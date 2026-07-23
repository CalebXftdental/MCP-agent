"""Local HTTP smoke for workflow template enable/disable controls."""
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
TMP = ROOT / "_smoke" / ".tmp" / "workflow-template-controls"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "wf_control_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "wf_control_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-template-control-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18045/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
})

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def wait_health(url: str, timeout: float = 10.0) -> None:
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


print("start office backend")
office = subprocess.Popen(
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18045", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18045/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    template_id = "shipment_exception_report"
    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "wf_control_admin", "password": "wf_control_password"})
        check("admin login succeeds", login.status_code == 200)

        before = client.get(f"/workflows/{template_id}")
        check("template starts active", before.status_code == 200 and before.json().get("status") == "active")

        now = int(time.time())
        scheduled = client.post("/automations", json={
            "template_id": template_id,
            "display_name": "Controlled shipment exceptions",
            "interval_sec": 3600,
            "next_run_at": now - 1,
            "inputs": {"sample": True, "customer_id": "CTRL100"},
        })
        auto_id = scheduled.json().get("automationId")
        check("automation can be created while active", scheduled.status_code == 201 and auto_id)

        disabled = client.post(f"/admin/workflows/{template_id}/disable", json={"reason": "maintenance window"})
        disabled_body = disabled.json()
        check("admin disables template", disabled.status_code == 200 and disabled_body.get("status") == "disabled")
        check("disable reason persisted", disabled_body.get("disabledReason") == "maintenance window")

        gateway_app.workflows.reload_for_tests()
        listed = client.get("/workflows").json().get("workflows", [])
        listed_one = next((w for w in listed if w.get("templateId") == template_id), {})
        check("disabled status reloads from disk", listed_one.get("status") == "disabled" and listed_one.get("disabledReason") == "maintenance window")

        manual = client.post(f"/workflows/{template_id}/run", json={"sample": True, "customer_id": "CTRL200"})
        check("disabled template blocks manual run", manual.status_code == 403 and manual.json().get("error") == "workflow template is disabled")

        create_disabled = client.post("/automations", json={
            "template_id": template_id,
            "display_name": "Blocked disabled workflow",
            "interval_sec": 3600,
            "inputs": {"sample": True},
        })
        check("disabled template blocks new automation", create_disabled.status_code == 403)

        ran_disabled = client.post("/automations/run-due", json={"now": now})
        first_disabled = (ran_disabled.json().get("results") or [{}])[0]
        check("due automation records disabled status", ran_disabled.status_code == 200 and first_disabled.get("status") == "template_disabled")
        after_disabled = client.get(f"/automations/{auto_id}").json()
        check("automation last status persisted", after_disabled.get("lastStatus") == "template_disabled")

        enabled = client.post(f"/admin/workflows/{template_id}/enable", json={})
        check("admin enables template", enabled.status_code == 200 and enabled.json().get("status") == "active")

        gateway_app.automation_store.update_automation(auto_id, next_run_at=now - 1)
        ran_enabled = client.post("/automations/run-due", json={"now": now + 1})
        first_enabled = (ran_enabled.json().get("results") or [{}])[0]
        check("enabled template resumes scheduled execution", ran_enabled.status_code == 200 and first_enabled.get("status") == "completed")
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
