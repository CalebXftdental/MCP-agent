"""Local HTTP smoke for automatic broad sensitive export approval gates."""
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
TMP = ROOT / "_smoke" / ".tmp" / "workflow-broad-export"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "broad_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "broad_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-broad-export-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "GOVERNANCE_BROAD_EXPORT_APPROVAL_ROWS": "1",
    "OFFICE_MCP_URL": "http://127.0.0.1:18049/mcp",
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
        print(f"  FAIL {name}", detail if detail is not None else "")


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
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18049", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18049/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "broad_admin", "password": "broad_password"})
        check("login succeeds", login.status_code == 200, login.text)

        run = client.post("/workflows/customer_360_report/run", json={"sample": True, "customer_id": "BROAD100"})
        body = run.json()
        approval = body.get("approval") or {}
        approval_id = approval.get("approvalId")
        check("broad sensitive export pauses", run.status_code == 201 and body.get("status") == "approval_required" and approval_id, body)
        check("approval is high risk", approval.get("riskLevel") == "high" and "broad sensitive export" in approval.get("reason", ""), approval)
        check("generated artifacts attached", len(body.get("artifactIds") or []) == 2 and set(approval.get("artifactIds") or []) == set(body.get("artifactIds") or []), body)
        approval_step = next((s for s in body.get("steps", []) if s.get("stepId") == "request_broad_export_approval"), {})
        check("approval step records threshold", approval_step.get("status") == "pending" and approval_step.get("outputs", {}).get("threshold") == 1, approval_step)

        pending_resume = client.post(f"/workflow-runs/{body['runId']}/resume", json={"approval_id": approval_id})
        check("pending approval blocks resume", pending_resume.status_code == 409, pending_resume.text)
        approved = client.post(f"/approvals/{approval_id}/approve", json={"note": "broad export reviewed"})
        check("admin approves broad export", approved.status_code == 200 and approved.json().get("status") == "approved", approved.text)
        resumed = client.post(f"/workflow-runs/{body['runId']}/resume", json={"approval_id": approval_id})
        resumed_body = resumed.json()
        check("approved broad export resumes", resumed.status_code == 200 and resumed_body.get("status") == "completed", resumed.text)

        non_sensitive = client.post("/workflows/shipment_exception_report/run", json={"sample": True, "customer_id": "BROAD200", "classification": ["INTERNAL"]})
        check("non-sensitive export bypasses broad gate", non_sensitive.status_code == 201 and non_sensitive.json().get("status") == "completed", non_sensitive.text)
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)