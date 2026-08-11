"""Live-data verification for the two new bulk finance tools
(get_ap_invoices_due_soon, get_ar_invoices_past_due) -- proves each returns
real rows with the expected shape from the live ERP mirror, through the real
governed /dashboard/try-tool path (_govern: PDP + redaction + audit), before
either feeds a workflow graph.

Thresholds here are deliberately generous (days_ahead=365, min_invoice_age_days=0)
-- the point is proving the mechanism against real data whose actual date
distribution we don't know ahead of time, not testing a specific business
threshold (that's what the filter node inside each workflow does).
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
TMP = ROOT / "_smoke" / ".tmp" / "finance-bulk-tools"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "finance_bulk_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "finance_bulk_password",
    "GOVERNANCE_SESSION_SECRET": "finance-bulk-tools-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18461/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "30",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18461", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18461/health")
    check("mcp-minierp (real ERP mirror) healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:finance_bulk_tester", name="finance_bulk_tester", key_hash="", status="active",
        role="user", type="user", categories=["finance"],
        login_password_hash=hash_password("tester_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "finance_bulk_tester", "password": "tester_password"})

        def try_tool(tool, args):
            return client.post("/dashboard/try-tool", json={"tool": tool, "args": args, "customer_id": ""})

        # ── get_ap_invoices_due_soon ──
        ap_resp = try_tool("get_ap_invoices_due_soon", {"days_ahead": 365, "page": 1, "page_size": 25})
        check("get_ap_invoices_due_soon call succeeds", ap_resp.status_code == 200, ap_resp.text)
        ap_result = (ap_resp.json() or {}).get("result") or {}
        invoices = ap_result.get("invoices") or []
        check("get_ap_invoices_due_soon returned at least one real invoice", len(invoices) > 0, ap_result)
        if invoices:
            row = invoices[0]
            for key in ("invoiceNumber", "dueDate", "lineTotal", "paid", "vendorCode"):
                check(f"AP invoice row has {key!r}", key in row, row)
            check("at least one AP row resolved a real vendorName via the baccount join",
                  any(r.get("vendorName") for r in invoices), invoices[:3])

        # ── get_ar_invoices_past_due ──
        ar_resp = try_tool("get_ar_invoices_past_due", {"min_invoice_age_days": 0, "page": 1})
        check("get_ar_invoices_past_due call succeeds", ar_resp.status_code == 200, ar_resp.text)
        ar_result = (ar_resp.json() or {}).get("result") or {}
        ar_invoices = ar_result.get("invoices") or []
        check("get_ar_invoices_past_due returned at least one real invoice", len(ar_invoices) > 0, ar_result)
        if ar_invoices:
            row = ar_invoices[0]
            for key in ("invoiceNumber", "unpaidBalance", "lineTotal", "invoiceDate"):
                check(f"AR invoice row has {key!r}", key in row, row)
            check("no customerId/customerName field leaked onto an AR row (schema has no such link)",
                  not any("customer" in k.lower() for k in row.keys()), row)

finally:
    minierp.terminate()
    try:
        minierp.wait(timeout=8)
    except subprocess.TimeoutExpired:
        minierp.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
