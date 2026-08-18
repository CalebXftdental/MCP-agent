"""LIVE test of the `paginate` tool_call node config against the real mirrored
ERP (db-api.frontierdental.com via mcp-minierp), through the full governed
path -- workflow-graph create/publish/run, same as test_winback_radar_live.py.

Proves the interpreter's `_exhaust_tool_call` loop actually works end-to-end
against real data (not just stubbed govern/parse, see
test_paginate_tool_call_node.py's Part 1), and reports real wall-clock latency
so there's an honest number for "what does turning this on actually cost."

Politeness: get_customers_by_region, page_size left at its tool default (25),
max_pages capped at 5 (<=125 rows, <=5 governed calls) -- same bounded-request
budget discipline as the earlier raw-GraphQL benchmarking in this session.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "paginate-node-live"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "paginate_live_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "paginate_live_password",
    "GOVERNANCE_SESSION_SECRET": "paginate-node-live-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18481/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "60",
})

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


def wait_health(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"service did not become healthy: {last}")


print("start mcp-minierp (real ERP mirror)")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
minierp = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18481", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18481/health")
    check("mcp-minierp (real ERP mirror) healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:paginate_live_tester", name="paginate_live_tester", key_hash="", status="active",
        role="user", type="user", categories=["accounts"],
        login_password_hash=hash_password("tester_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "paginate_live_tester", "password": "tester_password"})

        # ── Baseline: ONE page, paginate off -- same call the interpreter has
        #    always made, to compare against the paginated version below. ──
        graph_baseline = client.post("/workflow-graphs", json={
            "displayName": "Paginate Live Baseline (one page)",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": []}},
                {"nodeId": "n_region", "kind": "tool_call", "tool": "get_customers_by_region",
                 "config": {"country": "US"}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n_region"}],
        })
        check("baseline graph created", graph_baseline.status_code == 201, graph_baseline.text)
        gid_baseline = graph_baseline.json()["graphId"]
        client.post(f"/workflow-graphs/{gid_baseline}/publish", json={})

        t0 = time.perf_counter()
        run_baseline = client.post(f"/workflows/{gid_baseline}/run", json={})
        baseline_elapsed = time.perf_counter() - t0
        baseline_body = run_baseline.json()
        check("baseline run completes", run_baseline.status_code == 201 and baseline_body.get("status") == "completed", baseline_body)
        baseline_step = next((s for s in baseline_body.get("steps", []) if s.get("stepId") == "n_region"), {})
        baseline_outputs = baseline_step.get("outputs", {})
        print(f"  baseline (paginate off): {baseline_elapsed*1000:.1f}ms wall-clock, "
              f"count={baseline_outputs.get('count')}, hasMore={baseline_outputs.get('hasMore')}")

        # ── Paginated: same tool, same filter, paginate:true, max_pages=5. ──
        graph_paginated = client.post("/workflow-graphs", json={
            "displayName": "Paginate Live Test (exhaust up to 5 pages)",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": []}},
                {"nodeId": "n_region", "kind": "tool_call", "tool": "get_customers_by_region",
                 "config": {"country": "US", "paginate": True, "max_pages": 5, "max_duration_sec": 45}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n_region"}],
        })
        check("paginated graph created", graph_paginated.status_code == 201, graph_paginated.text)
        gid_paginated = graph_paginated.json()["graphId"]
        client.post(f"/workflow-graphs/{gid_paginated}/publish", json={})

        t0 = time.perf_counter()
        run_paginated = client.post(f"/workflows/{gid_paginated}/run", json={})
        paginated_elapsed = time.perf_counter() - t0
        paginated_body = run_paginated.json()
        check("paginated run completes", run_paginated.status_code == 201 and paginated_body.get("status") == "completed", paginated_body)
        paginated_step = next((s for s in paginated_body.get("steps", []) if s.get("stepId") == "n_region"), {})
        paginated_outputs = paginated_step.get("outputs", {})
        print(f"  paginated (max_pages=5): {paginated_elapsed*1000:.1f}ms wall-clock, "
              f"count={paginated_outputs.get('count')}, pagesFetched={paginated_outputs.get('pagesFetched')}, "
              f"truncated={paginated_outputs.get('truncated')}, truncatedReason={paginated_outputs.get('truncatedReason')}")

        check(
            "paginated result has at least as many customers as the one-page baseline",
            (paginated_outputs.get("count") or 0) >= (baseline_outputs.get("count") or 0),
            (paginated_outputs.get("count"), baseline_outputs.get("count")),
        )
        check("paginated output carries pagesFetched", isinstance(paginated_outputs.get("pagesFetched"), int), paginated_outputs)
        if baseline_outputs.get("hasMore"):
            check(
                "when the baseline itself reports hasMore, paginate actually fetched more than 1 page",
                (paginated_outputs.get("pagesFetched") or 0) > 1, paginated_outputs,
            )
            per_page = paginated_elapsed / max(1, paginated_outputs.get("pagesFetched") or 1)
            print(f"  ~{per_page*1000:.1f}ms per page over {paginated_outputs.get('pagesFetched')} pages "
                  f"(vs {baseline_elapsed*1000:.1f}ms for the single baseline page)")
        else:
            print("  (baseline's own single page already had hasMore=False -- this territory has <=25 "
                  "customers, so 1 page really is the whole answer; paginate correctly did nothing extra)")
            check("paginate is a correct no-op when there's genuinely only one page", paginated_outputs.get("pagesFetched") == 1, paginated_outputs)

finally:
    minierp.terminate()
    try:
        minierp.wait(timeout=8)
    except subprocess.TimeoutExpired:
        minierp.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
