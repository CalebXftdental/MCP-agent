"""Local HTTP smoke for autonomous agent profiles and agent-owned schedules."""
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
TMP = ROOT / "_smoke" / ".tmp" / "agent-profiles"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "agent_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "agent_password",
    "GOVERNANCE_SESSION_SECRET": "agent-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18042/mcp",
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
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18042", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18042/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "agent_admin", "password": "agent_password"})
        check("admin login succeeds", login.status_code == 200)

        created = client.post("/admin/agents", json={
            "display_name": "Shipment exception agent",
            "consumer_id": "agent_shipments",
            "categories": ["office", "files"],
            "allowed_template_ids": ["shipment_exception_report"],
            "description": "Runs the shipment exception report on a schedule.",
        })
        body = created.json()
        agent = body.get("agent") or {}
        agent_id = agent.get("agentId")
        check("agent created", created.status_code == 201 and agent_id and body.get("api_key"))
        check("agent constraints persisted", agent.get("consumerId") == "agent_shipments" and agent.get("allowedTemplateIds") == ["shipment_exception_report"])

        listed = client.get("/admin/agents")
        check("agent listed", any(a.get("agentId") == agent_id for a in listed.json().get("agents", [])))

        blocked = client.post("/automations", json={
            "agent_id": agent_id,
            "template_id": "vendor_ap_summary",
            "display_name": "Blocked vendor run",
            "interval_sec": 3600,
            "next_run_at": int(time.time()) - 1,
            "inputs": {"sample": True, "vendor_code": "VEND100"},
        })
        check("disallowed agent template blocked", blocked.status_code == 403)

        now = int(time.time())
        scheduled = client.post("/automations", json={
            "agent_id": agent_id,
            "template_id": "shipment_exception_report",
            "display_name": "Agent shipment exceptions",
            "interval_sec": 3600,
            "next_run_at": now - 1,
            "inputs": {"sample": True, "customer_id": "AGENT100"},
        })
        sched = scheduled.json()
        auto_id = sched.get("automationId")
        check("agent automation created", scheduled.status_code == 201 and auto_id and sched.get("actorType") == "agent" and sched.get("owner") == "agent_shipments")

        ran = client.post("/automations/run-due", json={"now": now})
        run_body = ran.json()
        first = (run_body.get("results") or [{}])[0]
        run = first.get("run") or {}
        check("agent due run executes", ran.status_code == 200 and first.get("status") == "completed" and run.get("runId"))
        check("agent workflow created artifact", len(run.get("artifactIds") or []) == 1)

        gateway_app.agent_store.reload_for_tests()
        profile = client.get(f"/admin/agents/{agent_id}").json()
        check("agent run stats updated", profile.get("runCount") == 1 and profile.get("lastRunId") == run.get("runId"))

        disabled = client.patch(f"/admin/agents/{agent_id}", json={"status": "disabled"})
        check("agent disabled", disabled.status_code == 200 and disabled.json().get("status") == "disabled")
        denied_schedule = client.post("/automations", json={
            "agent_id": agent_id,
            "template_id": "shipment_exception_report",
            "display_name": "Disabled agent schedule",
            "interval_sec": 3600,
            "next_run_at": now - 1,
            "inputs": {"sample": True, "customer_id": "AGENT200"},
        })
        check("disabled agent cannot create schedule", denied_schedule.status_code == 403)

        gateway_app.automation_store.update_automation(auto_id, next_run_at=now - 1)
        blocked_run = client.post("/automations/run-due", json={"now": now})
        blocked_first = (blocked_run.json().get("results") or [{}])[0]
        check("disabled agent due run blocked", blocked_run.status_code == 200 and blocked_first.get("status") == "agent_not_allowed")
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
