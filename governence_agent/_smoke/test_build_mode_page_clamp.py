"""LIVE test for _govern's build-mode data-volume guards (gateway/govern.py),
refined 2026-08-21 after a real benchmark showed the original design (clamp
every build-mode page_size + silently truncate every build-mode list result)
actively broke the "My Workflow" copilot's actual job: a tool with a real
outputSchema (get_field_catalog already covers it) doesn't need probing, so
a real call to it during authoring is presumably deliberate and should get
real data -- clamping it just makes the report impossible to build, and the
model resorted to paging through a self-exhausting tool trying to reconstruct
the truncated data (see STAGE2_PLAN.md SS11.2/11.3).

Current design, verified here against two real tool shapes:
  1. A TYPED bulk tool (get_ap_invoices_due_soon, has a real outputSchema) --
     build-mode does NOT clamp its page_size (no probing needed), but if the
     real deliberate fetch is too big for a build-mode turn, _govern returns
     an honest status="too_large_for_context" instead of silently truncating
     or crashing. The identical call from Home chat is completely unaffected.
  2. An UNTYPED tool (get_customers_by_region, still `-> str`, no schema) --
     build-mode DOES force a small page_size, since this one may still need
     a real sample to learn shape (get_field_catalog would say unavailable).

Live: needs a real mcp-minierp instance so this is visible on real row
counts and real schema fetches, not a mock.
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
TMP = ROOT / "_smoke" / ".tmp" / "build-mode-page-clamp"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "clamp_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "clamp_password",
    "GOVERNANCE_SESSION_SECRET": "build-mode-page-clamp-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18581/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18581", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

try:
    wait_health("http://127.0.0.1:18581/health")
    check("mcp-minierp (real ERP mirror) healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    import request_context as ctx  # noqa: E402
    from mcp_server import mcp  # noqa: E402
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    import schema_catalog  # noqa: E402

    record = ConsumerRecord(
        consumer_id="user:clamp_tester", name="clamp_tester", key_hash="",
        status="active", role="user", type="user", categories=["finance", "accounts"],
    )
    get_store().upsert_consumer(record)
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set("test-harness")

    async def call_tool(session_id, name, args):
        result = await mcp.call_tool(name, {"session_id": session_id, **args})
        content = result[0] if isinstance(result, tuple) else result
        for part in content or []:
            if getattr(part, "text", None) is not None:
                return json.loads(part.text)
        return {}

    import asyncio

    async def main():
        # ── Sanity: confirm schema_catalog's own typed/untyped split first,
        # since both halves of this test depend on it. ──────────────────────
        typed = await schema_catalog.get_output_schema("minierp_finance", "get_ap_invoices_due_soon")
        untyped = await schema_catalog.get_output_schema("minierp_accounts", "get_customers_by_region")
        check("get_ap_invoices_due_soon has a real schema", typed is not None, typed)
        check("get_customers_by_region does NOT have a real schema yet", untyped is None, untyped)

        # ── 1. Typed bulk tool: build-mode has no REQUEST-side clamp for a
        # known-schema tool (a deliberate fetch is presumably intentional),
        # so it relies entirely on the response-side too-large signal. Home
        # chat, added 2026-08-26 (see _smoke/test_home_chat_bulk_cap.py),
        # clamps the OUTBOUND page_size unconditionally instead -- there's no
        # "deliberate full fetch" concept in a normal chat question. Against
        # this specific tool, which already does a real DB-level LIMIT
        # (mcp-minierp/sqlagent/finance/index.py, fixed 2026-08-21), that
        # means Home chat's request comes back small directly -- nothing left
        # for the response-side truncation to even need to do. The response
        # cap (truncated=true/totalMatched) is still the necessary BACKSTOP
        # for any tool that, like the currently-deployed-to-production
        # get_ar_invoices_past_due, does NOT honor page_size -- confirmed
        # separately in test_home_chat_bulk_cap.py against a mock backend
        # built to reproduce exactly that. ──────────────────────────────────
        build_result = await call_tool(
            "workflow-chat:clamp_tester", "minierp_finance_get_ap_invoices_due_soon",
            {"days_ahead": 365, "page_size": 250},
        )
        home_result = await call_tool(
            "chat:clamp_tester", "minierp_finance_get_ap_invoices_due_soon",
            {"days_ahead": 365, "page_size": 250},
        )
        home_count = len(home_result.get("invoices") or [])
        print(f"  workflow-chat (typed, oversized real match) -> status={build_result.get('status')!r}; "
              f"chat (Home) returned {home_count} rows (real pagination.pageSize={((home_result.get('pagination') or {}).get('pageSize'))})")
        check("build-mode call to a typed tool with an oversized real match returns too_large_for_context",
              build_result.get("status") == "too_large_for_context", build_result)
        check("the too_large_for_context message tells the model not to page for more",
              "page" in (build_result.get("message") or "").lower(), build_result)
        check("Home chat call is NOT a hard reject, and returns a small preview either way "
              "(request-clamped directly, or response-truncated as the backstop)",
              home_result.get("status") != "too_large_for_context" and home_count <= 15, home_count)

        # ── 2. Untyped tool: build-mode DOES force a small page_size (still
        # needs probing for shape, per schema_catalog above). ───────────────
        build_untyped = await call_tool(
            "workflow-chat:clamp_tester", "minierp_accounts_get_customers_by_region",
            {"state": "CA", "page_size": 25},
        )
        untyped_count = len(build_untyped.get("customers") or build_untyped.get("records") or [])
        print(f"  workflow-chat (untyped, page_size=25 requested) -> {untyped_count} rows back")
        check("build-mode call to an untyped tool is forced to a small sample despite page_size=25",
              0 < untyped_count <= 3, build_untyped)

    asyncio.run(main())

finally:
    minierp.terminate()
    try:
        minierp.wait(timeout=8)
    except subprocess.TimeoutExpired:
        minierp.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
