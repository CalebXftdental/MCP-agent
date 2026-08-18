"""LIVE test confirming `paginate: true` is a correct, cost-free no-op on a
tool that does NOT expose a top-level `hasMore` key -- specifically
get_ap_invoices_due_soon, which already self-exhausts internally (via
sqlagent/finance/index.py's own `_paginate` -> minierp_core.paginate_all,
capped at 20 pages) and reports `truncated`, not `hasMore`.

This is the real-world case for "applicable to all tools, no allowlist
needed": the SAME `paginate: true` config that drives a real multi-page
exhaustion on get_customers_by_region (test_paginate_node_live.py) must cost
nothing extra here -- exactly one governed call either way -- because
_exhaust_tool_call's generic "no hasMore key -> return after one call"
fallback doesn't know or care which specific tool it's calling.
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
TMP = ROOT / "_smoke" / ".tmp" / "paginate-noop-live"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "paginate_noop_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "paginate_noop_password",
    "GOVERNANCE_SESSION_SECRET": "paginate-noop-live-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18491/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18491", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18491/health")
    check("mcp-minierp (real ERP mirror) healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:paginate_noop_tester", name="paginate_noop_tester", key_hash="", status="active",
        role="user", type="user", categories=["finance"],
        login_password_hash=hash_password("tester_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "paginate_noop_tester", "password": "tester_password"})

        def run_once(display_name: str, paginate_config: dict) -> tuple[float, dict]:
            graph = client.post("/workflow-graphs", json={
                "displayName": display_name,
                "nodes": [
                    {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": []}},
                    {"nodeId": "n_ap", "kind": "tool_call", "tool": "get_ap_invoices_due_soon",
                     "config": {"days_ahead": 30, **paginate_config}},
                ],
                "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n_ap"}],
            })
            check(f"{display_name}: graph created", graph.status_code == 201, graph.text)
            gid = graph.json()["graphId"]
            client.post(f"/workflow-graphs/{gid}/publish", json={})
            t0 = time.perf_counter()
            run = client.post(f"/workflows/{gid}/run", json={})
            elapsed = time.perf_counter() - t0
            body = run.json()
            check(f"{display_name}: run completes", run.status_code == 201 and body.get("status") == "completed", body)
            step = next((s for s in body.get("steps", []) if s.get("stepId") == "n_ap"), {})
            return elapsed, step.get("outputs", {})

        plain_elapsed, plain_outputs = run_once("AP Due Soon (paginate off)", {})
        print(f"  paginate off: {plain_elapsed*1000:.1f}ms, count={plain_outputs.get('count')}, truncated={plain_outputs.get('truncated')}")

        paginate_elapsed, paginate_outputs = run_once("AP Due Soon (paginate on, no-op expected)", {"paginate": True, "max_pages": 5})
        print(f"  paginate on:  {paginate_elapsed*1000:.1f}ms, count={paginate_outputs.get('count')}, "
              f"truncated={paginate_outputs.get('truncated')}, pagesFetched={paginate_outputs.get('pagesFetched')}")

        check(
            "same invoice count with paginate on vs off (no hasMore key -> no extra pages attempted)",
            paginate_outputs.get("count") == plain_outputs.get("count"), (paginate_outputs.get("count"), plain_outputs.get("count")),
        )
        check(
            "paginate:true costs roughly the same wall-clock as paginate off (no 5x multiplier from a phantom loop)",
            paginate_elapsed < plain_elapsed * 2.5, (paginate_elapsed, plain_elapsed),
        )
        check(
            "exactly one page fetched -- this tool has an invoices list but no hasMore key, so "
            "_exhaust_tool_call correctly stopped after the first (and only) governed call",
            paginate_outputs.get("pagesFetched") == 1 and paginate_outputs.get("truncated") is False, paginate_outputs,
        )

finally:
    minierp.terminate()
    try:
        minierp.wait(timeout=8)
    except subprocess.TimeoutExpired:
        minierp.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
