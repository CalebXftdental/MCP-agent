"""Local HTTP smoke for workflow readiness/preflight checks."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import time
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "workflow-preflight"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "preflight_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "preflight_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-preflight-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18121/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18130/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18140/mcp",
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
from auth.passwords import hash_password  # noqa: E402
from store.models import ConsumerRecord  # noqa: E402

store = gateway_app.get_store()
store.upsert_consumer(ConsumerRecord(
    consumer_id="user:ops_viewer",
    name="ops_viewer",
    key_hash="",
    status="active",
    role="user",
    type="user",
    categories=["orders"],
    login_password_hash=hash_password("ops_password"),
))

with TestClient(gateway_app.app, base_url="http://testserver") as client:
    login = client.post("/dashboard/login", json={"username": "preflight_admin", "password": "preflight_password"})
    check("admin login succeeds", login.status_code == 200, login.text)

    ready = client.get("/workflows/shipment_exception_report/preflight")
    ready_body = ready.json()
    check("admin preflight accepts sample defaults", ready.status_code == 200 and ready_body.get("ready") is True, ready_body)
    check("preflight reports connectors", {c.get("backend") for c in ready_body.get("connectors", [])} >= {"minierp_orders", "minierp_shipments", "office"}, ready_body)
    check("sample warning returned", any("sample mode" in w for w in ready_body.get("warnings", [])), ready_body)

    missing_input = client.post("/workflows/vendor_ap_summary/preflight", json={"sample": False})
    missing_body = missing_input.json()
    check("real vendor run needs vendor code", missing_input.status_code == 200 and missing_body.get("ready") is False and "vendor_code" in missing_body.get("missingInputs", []), missing_body)

    email = client.post("/workflows/customer_email_draft/preflight", json={"sample": True, "request_send_approval": True})
    email_body = email.json()
    check("email preflight exposes approval gate", email.status_code == 200 and email_body.get("approvalGates", [{}])[0].get("enabled") is True, email_body)
    check("email preflight warns about default recipient", any("recipient" in w for w in email_body.get("warnings", [])), email_body)

    disabled = client.post("/admin/workflows/shipment_exception_report/disable", json={"reason": "preflight smoke"})
    check("template disabled for preflight", disabled.status_code == 200 and disabled.json().get("status") == "disabled", disabled.text)
    disabled_preflight = client.post("/workflows/shipment_exception_report/preflight", json={"sample": True})
    disabled_body = disabled_preflight.json()
    check("disabled workflow is readiness blocker", disabled_preflight.status_code == 200 and "workflow template is disabled" in disabled_body.get("blockers", []), disabled_body)
    client.post("/admin/workflows/shipment_exception_report/enable", json={})

    unknown = client.get("/workflows/not_a_template/preflight")
    check("unknown template returns 404", unknown.status_code == 404 and unknown.json().get("error") == "unknown workflow template", unknown.text)

    paused = client.put("/admin/controls", json={"paused_agents": False, "paused_backends": ["office"]})
    check("admin pauses office backend", paused.status_code == 200 and "office" in paused.json().get("controls", {}).get("paused_backends", []), paused.text)
    paused_preflight = client.post("/workflows/shipment_exception_report/preflight", json={"sample": True, "customer_id": "PAUSE100"})
    paused_body = paused_preflight.json()
    check("paused backend blocks readiness", paused_preflight.status_code == 200 and "office" in paused_body.get("pausedBackends", []) and any("backend is paused: office" == b for b in paused_body.get("blockers", [])), paused_body)
    blocked_run = client.post("/workflows/shipment_exception_report/run", json={"sample": True, "customer_id": "PAUSE100"})
    check("paused backend blocks manual workflow launch", blocked_run.status_code == 403 and "backend is paused: office" in blocked_run.json().get("error", ""), blocked_run.text)
    run_count = len(client.get("/workflow-runs").json().get("runs", []))
    check("blocked launch does not create workflow run", run_count == 0, run_count)
    now = int(time.time())
    scheduled = client.post("/automations", json={"template_id": "shipment_exception_report", "display_name": "Pause protected workflow", "interval_sec": 3600, "next_run_at": now - 1, "inputs": {"sample": True, "customer_id": "PAUSE200"}})
    due = client.post("/automations/run-due", json={"now": now})
    first_due = (due.json().get("results") or [{}])[0]
    check("automation can be scheduled while connector paused", scheduled.status_code == 201, scheduled.text)
    check("paused backend prevents due workflow execution", due.status_code == 200 and first_due.get("status") == "failed" and "backend is paused: office" in (first_due.get("run") or {}).get("error", ""), due.text)
    resumed_controls = client.put("/admin/controls", json={"paused_agents": False, "paused_backends": []})
    check("admin resumes office backend", resumed_controls.status_code == 200 and resumed_controls.json().get("controls", {}).get("paused_backends") == [], resumed_controls.text)

    viewer_login = client.post("/dashboard/login", json={"username": "ops_viewer", "password": "ops_password"})
    check("viewer login succeeds", viewer_login.status_code == 200, viewer_login.text)
    viewer_preflight = client.post("/workflows/shipment_exception_report/preflight", json={"sample": True})
    viewer_body = viewer_preflight.json()
    check("viewer preflight detects missing access", viewer_preflight.status_code == 200 and viewer_body.get("ready") is False, viewer_body)
    check("viewer missing categories are explicit", set(viewer_body.get("missingCategories", [])) == {"office", "shipments"}, viewer_body)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)