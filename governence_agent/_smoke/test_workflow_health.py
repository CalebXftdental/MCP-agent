"""Local HTTP smoke for workflow health and evaluation summary."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import time
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "workflow-health"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "health_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "health_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-health-smoke-secret",
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
workflows = gateway_app.workflows
workflows.reload_for_tests()

print("workflow health")
with TestClient(gateway_app.app, base_url="http://testserver") as client:
    login = client.post("/dashboard/login", json={"username": "health_admin", "password": "health_password"})
    admin_cookie = login.cookies.get("gov_session")
    admin_headers = {"cookie": f"gov_session={admin_cookie}"}
    client.cookies.clear()
    check("admin login succeeds", login.status_code == 200 and bool(admin_cookie))

    viewer_name = "health_viewer"
    created_user = client.post("/admin/consumers", headers=admin_headers, json={
        "consumer_id": viewer_name,
        "name": viewer_name,
        "role": "user",
        "type": "user",
        "status": "active",
        "password": "viewer_password",
        "categories": ["orders"],
    })
    check("viewer account created", created_user.status_code in (201, 409), created_user.text)
    viewer_login = client.post("/dashboard/login", json={"username": viewer_name, "password": "viewer_password"})
    viewer_cookie = viewer_login.cookies.get("gov_session")
    viewer_headers = {"cookie": f"gov_session={viewer_cookie}"}
    client.cookies.clear()
    check("viewer login succeeds", viewer_login.status_code == 200 and bool(viewer_cookie))

    completed = workflows.new_run("shipment_exception_report", "health_admin", {"sample": True})
    completed = workflows.update_run(completed, status="completed", artifact_ids=["art_one"], updated_at=completed.created_at + 12)
    failed = workflows.new_run("vendor_ap_summary", "health_admin", {"sample": True})
    failed = workflows.update_run(failed, status="failed", error="backend unavailable", updated_at=failed.created_at + 8)
    paused = workflows.new_run("weekly_executive_brief", "health_admin", {"sample": True})
    paused = workflows.mark_approval_required(paused.run_id, approval_id="appr_health", artifact_ids=["art_two", "art_three"])
    running = workflows.new_run("customer_360_report", "health_admin", {"sample": True})
    running = workflows.update_run(running, updated_at=time.time() - 4000)

    denied = client.get("/admin/workflow-health", headers=viewer_headers)
    check("workflow health is admin-only", denied.status_code == 403)

    health = client.get("/admin/workflow-health?stuck_after_sec=60", headers=admin_headers)
    body = health.json()
    check("workflow health loads", health.status_code == 200 and body.get("totalRuns") == 4, body)
    check("status counts include lifecycle states", body.get("statusCounts", {}).get("completed") == 1 and body.get("statusCounts", {}).get("failed") == 1 and body.get("statusCounts", {}).get("approval_required") == 1, body.get("statusCounts"))
    check("failure rate computed", body.get("failureRate") == 25.0, body)
    check("approval rate computed", body.get("approvalRate") == 25.0, body)
    check("stuck run detected", len(body.get("stuckRuns") or []) == 1 and body["stuckRuns"][0]["runId"] == running.run_id, body.get("stuckRuns"))
    templates = {t.get("templateId"): t for t in body.get("templates", [])}
    check("template rows include empty templates", "customer_email_draft" in templates and templates["customer_email_draft"].get("runs") == 0, templates)
    check("template artifact rate computed", templates["weekly_executive_brief"].get("artifactRate") == 2.0, templates["weekly_executive_brief"])

    workflows.reload_for_tests()
    persisted = client.get("/admin/workflow-health?stuck_after_sec=60", headers=admin_headers)
    check("health survives reload", persisted.status_code == 200 and persisted.json().get("totalRuns") == 4, persisted.text)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)