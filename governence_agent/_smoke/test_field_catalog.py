"""Smoke test for the workflow-copilot's `get_field_catalog` meta tool: a
static, cost-free field-shape lookup (schema, not a live data call) for tools
that have declared an outputSchema (today: mcp-minierp's finance domain --
see finalize_stage_1.md's outputSchema rollout notes), and a clean
`status="unavailable"` fallback for everything else.

Live: needs a real mcp-minierp instance so get_field_catalog can fetch its
outputSchema over the real MCP transport (mcp_clients.get_tool_schema) --
same reasoning as test_finance_bulk_tools.py/test_paginate_noop_live.py.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "field-catalog"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "field_catalog_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "field_catalog_password",
    "GOVERNANCE_SESSION_SECRET": "field-catalog-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18571/mcp",
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
        print(f"  FAIL {name}", str(detail)[:600] if detail is not None else "")


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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18571", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

try:
    wait_health("http://127.0.0.1:18571/health")
    check("mcp-minierp (real ERP mirror) healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    check("gateway/app.py imports cleanly with get_field_catalog registered", True)

    import orchestrator  # noqa: E402
    import request_context as ctx  # noqa: E402
    from mcp_server import mcp  # noqa: E402
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402

    record = ConsumerRecord(
        consumer_id="user:field_catalog_tester", name="field_catalog_tester", key_hash="",
        status="active", role="user", type="user", categories=["finance"],
    )
    get_store().upsert_consumer(record)
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set("test-harness")

    check("get_field_catalog is workflow-copilot-only", "get_field_catalog" in orchestrator.WORKFLOW_ONLY_TOOLS)

    async def call_tool(name, args):
        result = await mcp.call_tool(name, {"session_id": "field-catalog-test", **args})
        content = result[0] if isinstance(result, tuple) else result
        for part in content or []:
            if getattr(part, "text", None) is not None:
                return json.loads(part.text)
        return {}

    import asyncio

    async def main():
        # ── A typed tool: real schema, real fields ──────────────────────────
        r1 = await call_tool("get_field_catalog", {"tool_name": "minierp_finance_get_vendor_details"})
        check("typed tool: status success", r1.get("status") == "success", r1)
        expected_top = {
            "source", "status", "intent", "message", "missingFields", "vendorCode",
            "vendorClassId", "termsId", "curyId", "paymentMethodId", "vendor1099", "retainageApply",
        }
        check("typed tool: exact top-level field set", set(r1.get("topLevelFields") or []) == expected_top,
              r1.get("topLevelFields"))
        check("typed tool: no array fields on a single-record tool", r1.get("arrayFields") == {}, r1)

        # ── A typed list tool: array-of-record flattening ───────────────────
        r2 = await call_tool("get_field_catalog", {"tool_name": "minierp_finance_get_ap_invoices_due_soon"})
        check("typed list tool: status success", r2.get("status") == "success", r2)
        expected_row = {
            "invoiceNumber", "docType", "invoiceDate", "dueDate", "lineTotal",
            "taxTotal", "paid", "vendorCode", "vendorName",
        }
        check("typed list tool: invoices array item fields match the real row shape",
              set((r2.get("arrayFields") or {}).get("invoices") or []) == expected_row,
              r2.get("arrayFields"))

        # ── An untyped tool: clean, honest fallback ──────────────────────────
        r3 = await call_tool("get_field_catalog", {"tool_name": "minierp_orders_get_order_details"})
        check("untyped tool: status unavailable (not a fabricated schema)", r3.get("status") == "unavailable", r3)

        # ── Unknown tool name: clean error, not a crash ──────────────────────
        r4 = await call_tool("get_field_catalog", {"tool_name": "not_a_real_tool_xyz"})
        check("unknown tool name: status error", r4.get("status") == "error", r4)

        # ── Called twice for the same tool -- cache path doesn't error ───────
        r5 = await call_tool("get_field_catalog", {"tool_name": "minierp_finance_get_vendor_details"})
        check("second call for the same tool still succeeds (cache hit path)",
              r5.get("status") == "success" and r5.get("topLevelFields") == r1.get("topLevelFields"), (r1, r5))

    asyncio.run(main())

finally:
    minierp.terminate()
    try:
        minierp.wait(timeout=8)
    except subprocess.TimeoutExpired:
        minierp.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
