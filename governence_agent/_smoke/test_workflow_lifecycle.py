"""Local HTTP smoke for workflow approval pause, resume, and cancel lifecycle."""
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
TMP = ROOT / "_smoke" / ".tmp" / "workflow-lifecycle"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "life_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "life_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-life-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18043/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
})

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


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
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18043", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18043/health")
    check("office backend healthy", True)
    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "life_admin", "password": "life_password"})
        check("login succeeds", login.status_code == 200, login.text)

        run = client.post("/workflows/weekly_executive_brief/run", json={
            "sample": True,
            "start_date": "2026-07-15",
            "end_date": "2026-07-22",
            "request_approval": True,
        })
        body = run.json()
        rid = body.get("runId")
        approval = body.get("approval") or {}
        approval_id = approval.get("approvalId")
        check("approval workflow pauses", run.status_code == 201 and body.get("status") == "approval_required" and rid and approval_id, body)
        check("approval id attached to run", approval_id in body.get("approvalIds", []) and len(body.get("artifactIds", [])) == 3, body)
        approval_step = next((s for s in body.get("steps", []) if s.get("type") == "approval"), {})
        check("approval step pending", approval_step.get("status") == "pending", approval_step)

        pending_resume = client.post(f"/workflow-runs/{rid}/resume", json={"approval_id": approval_id})
        check("pending approval blocks resume", pending_resume.status_code == 409, pending_resume.text)

        approved = client.post(f"/approvals/{approval_id}/approve", json={"note": "Reviewed"})
        check("approval approved", approved.status_code == 200 and approved.json().get("status") == "approved", approved.text)
        still_paused = client.get(f"/workflow-runs/{rid}")
        check("approved run still awaits resume", still_paused.status_code == 200 and still_paused.json().get("status") == "approval_required", still_paused.text)

        resumed = client.post(f"/workflow-runs/{rid}/resume", json={"approval_id": approval_id})
        resumed_body = resumed.json()
        check("approved workflow resumes", resumed.status_code == 200 and resumed_body.get("status") == "completed" and resumed_body.get("resumedAt"), resumed.text)
        check("approval step completed after resume", any(s.get("type") == "approval" and s.get("status") == "completed" for s in resumed_body.get("steps", [])), resumed_body.get("steps"))

        cancel_run = client.post("/workflows/weekly_executive_brief/run", json={
            "sample": True,
            "start_date": "2026-07-15",
            "end_date": "2026-07-22",
            "request_approval": True,
        })
        cancel_body = cancel_run.json()
        cancel_id = cancel_body.get("runId")
        cancelled = client.post(f"/workflow-runs/{cancel_id}/cancel", json={"reason": "No longer needed"})
        check("approval-required workflow cancels", cancelled.status_code == 200 and cancelled.json().get("status") == "cancelled", cancelled.text)
        cancelled_step = next((s for s in cancelled.json().get("steps", []) if s.get("type") == "approval"), {})
        check("pending approval step cancelled", cancelled_step.get("status") == "cancelled", cancelled_step)
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
