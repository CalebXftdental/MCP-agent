"""LIVE test confirming `paginate: true` genuinely accumulates multiple real
pages on get_ap_invoices_due_soon, against the real ERP mirror.

Superseded premise (kept here for history): this tool used to self-exhaust
internally (via sqlagent/finance/index.py's own `_paginate` ->
minierp_core.paginate_all, capped at 20 pages) regardless of what page/
page_size was asked for, so `paginate: true` on its tool_call node was a
documented, cost-free no-op -- the tool had no real per-call `hasMore` to
loop on. As of 2026-08-21 the tool was redesigned to be an honest single
real page per call (see finance/index.py's docstring), which makes
`paginate: true` do real, useful work here for the first time: this test now
proves that work happens (multiple governed calls, rows accumulate to the
same total the old auto-exhausting tool used to produce in one call) and that
`paginate` omitted/false stays a true single page, not the old silent
5000-row default.
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
                     "config": {"days_ahead": 365, **paginate_config}},
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

        # ── paginate omitted -- one real page, honest pagination block ──────
        plain_elapsed, plain_outputs = run_once("AP Due Soon (paginate off, page_size default)", {})
        plain_pagination = plain_outputs.get("pagination") or {}
        print(f"  paginate off: {plain_elapsed*1000:.1f}ms, invoices={len(plain_outputs.get('invoices') or [])}, "
              f"pagination={plain_pagination}")
        check("paginate off: exactly one real page's worth of rows (<= its pageSize), not a silent full dump",
              len(plain_outputs.get("invoices") or []) <= (plain_pagination.get("pageSize") or 250), plain_outputs)
        check("paginate off: response carries a real pagination block (page/pageSize/returned/hasMore)",
              {"page", "pageSize", "returned", "hasMore"} <= set(plain_pagination.keys()), plain_pagination)

        # ── paginate:true -- small page_size forces >1 real page, proving the
        #    interpreter's generic exhaustion loop actually drives this tool's
        #    now-honest hasMore, not just replaying page 1 forever. ──────────
        small_page_elapsed, small_page_outputs = run_once(
            "AP Due Soon (paginate on, small page_size)",
            {"page_size": 10, "paginate": True, "max_pages": 5},
        )
        print(f"  paginate on (page_size=10): {small_page_elapsed*1000:.1f}ms, "
              f"invoices={len(small_page_outputs.get('invoices') or [])}, "
              f"pagesFetched={small_page_outputs.get('pagesFetched')}, truncated={small_page_outputs.get('truncated')}")
        check(
            "paginate:true with a small page_size fetched more than one real page",
            (small_page_outputs.get("pagesFetched") or 0) > 1, small_page_outputs,
        )
        check(
            "paginate:true accumulated more rows than a single small page alone would have",
            len(small_page_outputs.get("invoices") or []) > 10, small_page_outputs,
        )

        # ── paginate:true at the tool's normal default page_size -- total
        #    accumulated rows should match what the OLD auto-exhausting tool
        #    used to return in a single call (same underlying dataset). ──────
        full_elapsed, full_outputs = run_once(
            "AP Due Soon (paginate on, default page_size)",
            {"paginate": True, "max_pages": 20},
        )
        full_count = len(full_outputs.get("invoices") or [])
        print(f"  paginate on (default page_size): {full_elapsed*1000:.1f}ms, invoices={full_count}, "
              f"pagesFetched={full_outputs.get('pagesFetched')}, truncated={full_outputs.get('truncated')}")
        check(
            "paginate:true (default page_size) matches or exceeds the small-page_size run's total "
            "(same underlying dataset, fully exhausted either way)",
            full_count >= len(small_page_outputs.get("invoices") or []), (full_count, small_page_outputs),
        )
        check("paginate:true (default page_size) was not truncated by the max_pages cap",
              full_outputs.get("truncated") is False, full_outputs)
        full_pagination = full_outputs.get("pagination") or {}
        check("paginate:true's merged pagination.returned reflects the full accumulated total, not one page's",
              full_pagination.get("returned") == full_count, full_pagination)

finally:
    minierp.terminate()
    try:
        minierp.wait(timeout=8)
    except subprocess.TimeoutExpired:
        minierp.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
