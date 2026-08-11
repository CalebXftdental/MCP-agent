"""Smoke test for the new "workflow request" feature: a non-technical user's
ask that exceeds today's capability gets logged (via the chat assistant's
submit_workflow_request tool AND the manual /dashboard/request-workflow box)
into the SAME admin queue as access requests, under kind="workflow", and an
admin can acknowledge/dismiss it without it trying to grant any access.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "workflow-request"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "wr_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "wr_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-request-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
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


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")
import asyncio  # noqa: E402
import json  # noqa: E402
import request_context as ctx  # noqa: E402
from mcp_server import mcp  # noqa: E402
from store import get_store  # noqa: E402
from store.models import ConsumerRecord  # noqa: E402
from auth.passwords import hash_password  # noqa: E402

store = get_store()
store.upsert_consumer(ConsumerRecord(
    consumer_id="user:wr_tester", name="wr_tester", key_hash="", status="active",
    role="user", type="user", categories=[],
    login_password_hash=hash_password("tester_password"),
))

with TestClient(gateway_app.app, base_url="http://testserver") as client:
    client.post("/dashboard/login", json={"username": "wr_tester", "password": "tester_password"})

    # ── Manual request box path ─────────────────────────────────────────────
    resp = client.post("/dashboard/request-workflow", json={
        "description": "Win-back radar: flag customers whose last order is far past "
                        "their normal reorder cadence AND who are not closed.",
    })
    check("manual workflow request created", resp.status_code == 201, resp.text)
    wf_id_manual = resp.json().get("id")

    # ── Chat-tool path (submit_workflow_request) -- called the way the REAL
    #    orchestrator.execute_tool does: mcp.call_tool directly, not via the
    #    Playground's /dashboard/try-tool (which requires a manifest entry and
    #    this tool deliberately has none -- it's a meta action, not governed
    #    business data, same reasoning as backend/session.py's _request_access). ─
    tester_record = store.get_consumer("user:wr_tester")
    ctx.consumer_ctx.set(tester_record.name)
    ctx.consumer_record_ctx.set(tester_record)

    async def call_tool_directly(args):
        result = await mcp.call_tool("submit_workflow_request", {**args, "session_id": "wf-request-test"})
        content = result[0] if isinstance(result, tuple) else result
        for part in content or []:
            if getattr(part, "text", None) is not None:
                return json.loads(part.text)
        return {}

    tool_result = asyncio.run(call_tool_directly({"description": "Vendor scorecard: rank vendors by on-time delivery and AP past-due."}))
    check("submit_workflow_request tool call succeeds", tool_result.get("status") == "success", tool_result)
    check("tool call returned a requestId", bool(tool_result.get("requestId")), tool_result)

    empty_result = asyncio.run(call_tool_directly({"description": "  "}))
    check("empty description tool call reports an error, not a fake success",
          empty_result.get("status") == "error", empty_result)

    client.post("/dashboard/logout")
    client.post("/dashboard/login", json={"username": "wr_admin", "password": "wr_password"})

    # ── Admin sees both requests, separable by kind == "workflow" ──────────
    pending = client.get("/admin/requests?status=pending")
    check("admin requests call succeeds", pending.status_code == 200, pending.text)
    reqs = pending.json().get("requests", [])
    workflow_reqs = [r for r in reqs if r.get("kind") == "workflow"]
    check("both workflow requests show up for admin review", len(workflow_reqs) == 2, workflow_reqs)
    check("manual request's free text is preserved in justification",
          any("win-back" in (r.get("justification") or "").lower() for r in workflow_reqs), workflow_reqs)
    check("tool-submitted request's free text is preserved in justification",
          any("vendor scorecard" in (r.get("justification") or "").lower() for r in workflow_reqs), workflow_reqs)

    # ── Acknowledge one, dismiss the other -- neither should touch any grant ─
    ack = client.post(f"/admin/requests/{wf_id_manual}/approve")
    check("acknowledging a workflow request succeeds (no grant/backend needed)", ack.status_code == 200, ack.text)

    other_id = tool_result.get("requestId")
    dismiss = client.post(f"/admin/requests/{other_id}/deny")
    check("dismissing a workflow request succeeds", dismiss.status_code == 200, dismiss.text)

    after = client.get("/admin/requests?status=pending").json().get("requests", [])
    check("both workflow requests left the pending queue", not any(r.get("kind") == "workflow" for r in after), after)

    acked = client.get("/admin/requests?status=acknowledged").json().get("requests", [])
    check("acknowledged request is queryable by its new status", any(r.get("id") == wf_id_manual for r in acked), acked)

    dismissed = client.get("/admin/requests?status=dismissed").json().get("requests", [])
    check("dismissed request is queryable by its new status", any(r.get("id") == other_id for r in dismissed), dismissed)

    # ── The consumer who filed the request was NOT granted anything ────────
    tester = store.get_consumer("user:wr_tester")
    check("filing a workflow request granted no categories/overrides",
          tester.categories == [] and not tester.overrides, (tester.categories, tester.overrides))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
