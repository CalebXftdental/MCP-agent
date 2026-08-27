"""Home-chat bulk-result capping + export, end to end through the REAL
governed pipeline (_govern: PDP -> backend call -> redaction -> the new cap)
-- a synthetic mock backend stands in for mcp-minierp (no db-api call; this
is testing NEW, not-yet-deployed governance-layer code, which by definition
can't be tested against the live production gateway), but every other layer
(gateway/app.py's real tool wrapper, govern.py's real _govern, the real
mcp-office backend for the actual Excel artifact) is real.

Confirmed live (2026-08-26, see _smoke/test_kb_answer_quality.py's [prod]
cases): an ordinary Home-chat question against the REAL get_ap_invoices_due_soon
returned 1,115 rows / ~67K tokens in ONE call with no page_size given -- an
instant guaranteed context-length crash on the local Qwen backend (40,960
tokens) that serves Home chat by default. This test proves the fix: Home-chat
sessions ("chat:"-prefixed) never see more than GOVERNANCE_HOME_CHAT_MAX_RESULT_ROWS
rows from a single call, and the full data is still reachable afterward via
export_bulk_result_to_excel without ever re-entering an LLM's context.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "home-chat-bulk-cap"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

_MOCK_ROWS = 200  # comfortably over the 15-row cap, and small enough this stays a fast unit-ish test

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "bulkcap_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "bulkcap_password",
    "GOVERNANCE_SESSION_SECRET": "bulkcap-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18521/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18522/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "20",
    "GOVERNANCE_HOME_CHAT_MAX_RESULT_ROWS": "15",
})

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def wait_health(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"service did not become healthy: {last}")


# Minimal mock mcp-minierp -- ONE tool, matching get_ap_invoices_due_soon's
# real bare canonical name/signature, returning a large synthetic (never
# real-customer) row set. Same shape/purpose as the existing mock_minierp.py
# but for the current tool surface, not the pre-consolidation one.
MOCK_MINIERP = TMP / "mock_minierp_bulk.py"
MOCK_MINIERP.write_text(f"""
import json
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

mcp = FastMCP(
    "mock-minierp-bulk", stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)

@mcp.tool()
async def get_ap_invoices_due_soon(days_ahead: int = 14, company_id: int | None = None, page: int = 1, page_size: int = 250) -> str:
    # A "badly-behaved" backend, deliberately IGNORING page_size -- mirrors
    # the real, currently-deployed-to-production get_ar_invoices_past_due
    # confirmed live (2026-08-26) to return the same ~5,000 rows regardless
    # of requested page_size. Exercises the response-side backstop cap.
    rows = [
        {{"invoiceNumber": f"AP{{i:07d}}", "vendorCode": f"VEND{{i % 7}}", "lineTotal": 100.0 + i, "paid": False}}
        for i in range(1, {_MOCK_ROWS} + 1)
    ]
    return json.dumps({{
        "source": "miniERP-finance", "status": "ok", "intent": "ap_invoices_due_soon",
        "invoices": rows, "receivedPageSize": page_size,
    }})

@mcp.tool()
async def get_ar_invoices_past_due(min_invoice_age_days: int = 30, company_id: int | None = None, page: int = 1, page_size: int = 250) -> str:
    # A "well-behaved" backend, HONORING page_size with a real slice -- mirrors
    # what the ALREADY-FIXED local code (mcp-minierp/sqlagent/finance/index.py,
    # a real DB-level LIMIT via find_with_offset_pagination) does once it's
    # actually deployed. Proves the request-side clamp itself: if Home chat's
    # outbound page_size is really being clamped, this returns few rows
    # directly, not {_MOCK_ROWS} rows truncated afterward.
    all_rows = [
        {{"invoiceNumber": f"AR{{i:07d}}", "unpaidBalance": 100.0 + i}}
        for i in range(1, {_MOCK_ROWS} + 1)
    ]
    rows = all_rows[: max(1, int(page_size or 250))]
    return json.dumps({{
        "source": "miniERP-finance", "status": "ok", "intent": "ar_invoices_past_due",
        "invoices": rows, "receivedPageSize": page_size,
    }})

async def _health(_request):
    return JSONResponse({{"ok": True}})

app = mcp.streamable_http_app()
app.add_route("/health", _health)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=18521)
""", encoding="utf-8")

print("start mock mcp-minierp (synthetic bulk data, no db-api) + real mcp-office")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
minierp = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "mock_minierp_bulk:app", "--host", "127.0.0.1", "--port", "18521", "--no-access-log"],
    cwd=str(TMP), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18522", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18521/health")
    wait_health("http://127.0.0.1:18522/health")
    check("mock mcp-minierp + real mcp-office healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    import bulk_result_cache  # noqa: E402
    import request_context as ctx  # noqa: E402
    from govern import _govern  # noqa: E402
    import app as gateway_app  # noqa: E402,F401 -- registers @mcp.tool defs (incl. export_bulk_result_to_excel)
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    record = ConsumerRecord(
        consumer_id="user:bulkcap_tester", name="bulkcap_tester", key_hash="", status="active",
        role="user", type="user", categories=["finance", "office"],
        login_password_hash=hash_password("tester_password"),
    )
    store.upsert_consumer(record)
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set("127.0.0.1")

    SESSION = "chat:bulkcap_test_1"

    async def run():
        # 1) A real Home-chat session calling a bulk tool gets capped.
        raw = await _govern("get_ap_invoices_due_soon", SESSION, "", {"days_ahead": 30, "company_id": None, "page": 1, "page_size": 250})
        parsed = json.loads(raw)
        check("response is capped (truncated=true)", parsed.get("truncated") is True, parsed.get("truncated"))
        check(f"only {os.environ['GOVERNANCE_HOME_CHAT_MAX_RESULT_ROWS']} rows shown, not all {_MOCK_ROWS}",
              len(parsed.get("invoices") or []) == 15, len(parsed.get("invoices") or []))
        check(f"totalMatched reports the REAL count ({_MOCK_ROWS})", parsed.get("totalMatched") == _MOCK_ROWS, parsed.get("totalMatched"))
        check("carries an exportHint the model can act on", "export_bulk_result_to_excel" in (parsed.get("exportHint") or ""))

        # 2) A DIFFERENT session (a real workflow run, not home-chat) is NOT capped --
        #    this cap must be Home-chat-specific, never touch real execution.
        raw2 = await _govern("get_ap_invoices_due_soon", "workflow:run_1", "", {"days_ahead": 30, "company_id": None, "page": 1, "page_size": 250})
        parsed2 = json.loads(raw2)
        check("a real workflow-execution session is NOT capped", not parsed2.get("truncated"), parsed2)
        check("workflow-execution session gets the FULL row set", len(parsed2.get("invoices") or []) == _MOCK_ROWS, len(parsed2.get("invoices") or []))

        # 2b) The REQUEST-side clamp itself, against a well-behaved backend
        # that actually honors page_size: a Home-chat session asking for
        # page_size=250 should have that clamped down BEFORE the call is even
        # made, so the backend only ever sees a small request -- not just a
        # big real fetch trimmed on the way back out.
        raw3 = await _govern("get_ar_invoices_past_due", SESSION, "", {"min_invoice_age_days": 0, "company_id": None, "page": 1, "page_size": 250})
        parsed3 = json.loads(raw3)
        check("Home chat's OUTBOUND page_size was clamped to 15 before the call, not left at 250",
              parsed3.get("receivedPageSize") == 15, parsed3.get("receivedPageSize"))
        check("...so the well-behaved backend already returned <=15 rows directly (no truncation needed)",
              len(parsed3.get("invoices") or []) <= 15 and not parsed3.get("truncated"),
              (len(parsed3.get("invoices") or []), parsed3.get("truncated")))

        raw4 = await _govern("get_ar_invoices_past_due", "workflow:run_2", "", {"min_invoice_age_days": 0, "company_id": None, "page": 1, "page_size": 250})
        parsed4 = json.loads(raw4)
        check("a real workflow-execution session's page_size is NOT clamped",
              parsed4.get("receivedPageSize") == 250, parsed4.get("receivedPageSize"))

        # 3) export_bulk_result_to_excel hands back the COMPLETE data, not just the capped 15.
        export_raw = await gateway_app.export_bulk_result_to_excel(SESSION, "Bulk cap test export")
        export_result = json.loads(export_raw)
        check("export succeeded", export_result.get("status") == "success", export_result)
        check("export produced a real artifact with a download URL",
              bool(export_result.get("artifactId")) and bool(export_result.get("downloadUrl")), export_result)

        # The assertion that actually matters: does the exported FILE contain
        # all 200 real rows, or just the 15 that were shown in chat? A zip/XML
        # row count (no openpyxl in this env) -- coarse, but proves it's the
        # full set, not the capped preview re-exported by mistake.
        import zipfile
        import re as _re
        import artifact_store

        art = artifact_store.get_artifact(export_result["artifactId"])
        xlsx_bytes = Path(art.storage_path).read_bytes()
        with zipfile.ZipFile(__import__("io").BytesIO(xlsx_bytes)) as z:
            sheet_xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
        row_count = len(_re.findall(r"<row[ >]", sheet_xml))
        check(f"exported file contains ~{_MOCK_ROWS} real rows, not just the capped 15 (found {row_count})",
              row_count >= _MOCK_ROWS, row_count)

        # 4) The cache is cleared after a successful export -- a second export
        #    attempt with nothing new cached should fail cleanly, not silently
        #    re-serve stale data.
        export_again_raw = await gateway_app.export_bulk_result_to_excel(SESSION, "second export, nothing cached")
        export_again = json.loads(export_again_raw)
        check("re-exporting with nothing freshly cached fails cleanly", export_again.get("status") == "error", export_again)

        # 5) export_bulk_result_to_excel with NO prior capped call in this
        #    session at all also fails cleanly (never silently no-ops).
        empty_raw = await gateway_app.export_bulk_result_to_excel("chat:never_capped_anything", "")
        empty_result = json.loads(empty_raw)
        check("export with nothing ever cached for the session fails cleanly", empty_result.get("status") == "error", empty_result)

    asyncio.run(run())
finally:
    minierp.terminate()
    office.terminate()
    for p in (minierp, office):
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
