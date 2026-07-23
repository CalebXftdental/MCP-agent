"""Local HTTP smoke for the Customer Email Draft workflow."""
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

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "workflow-email-draft"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "wf_email_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "wf_email_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-email-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18037/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18047/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
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


python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
procs = []
try:
    for name, folder, port in [("office", "mcp-office", "18037"), ("email", "mcp-email", "18047")]:
        print(f"start {name} backend")
        proc = subprocess.Popen(
            [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", port, "--no-access-log"],
            cwd=str(ROOT / folder),
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        wait_health(f"http://127.0.0.1:{port}/health")
        check(f"{name} backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "wf_email_admin", "password": "wf_email_password"})
        check("login succeeds", login.status_code == 200, login.text)

        catalog = client.get("/workflows")
        check("workflow catalog includes customer_email_draft", any(w.get("templateId") == "customer_email_draft" and w.get("status") == "active" for w in catalog.json().get("workflows", [])), catalog.text)

        run = client.post("/workflows/customer_email_draft/run", json={
            "sample": True,
            "customer_id": "EMAIL100",
            "recipient": "manager@example.com",
            "topic": "renewal review follow-up",
            "include_packet": True,
            "request_send_approval": True,
        })
        body = run.json()
        if body.get("status") != "approval_required":
            print("workflow response", json.dumps(body, indent=2))
        check("workflow run created", run.status_code == 201, run.text)
        check("workflow paused for approval", body.get("status") == "approval_required", body)
        artifacts = body.get("artifacts") or []
        check("packet and draft created", len(artifacts) == 2 and {a.get("type") for a in artifacts} == {"pdf", "email_draft"}, artifacts)
        check("approval created", body.get("approval", {}).get("status") == "pending" and body.get("approval", {}).get("workflowRunId") == body.get("runId"), body.get("approval"))

        draft = next(a for a in artifacts if a.get("type") == "email_draft")
        packet = next(a for a in artifacts if a.get("type") == "pdf")
        draft_download = client.get(draft["downloadUrl"])
        draft_json = json.loads(draft_download.content.decode("utf-8"))
        check("draft downloads with recipient", draft_download.status_code == 200 and draft_json.get("to") == ["manager@example.com"], draft_json)
        check("draft references packet", packet.get("artifactId") in draft_json.get("attachmentArtifactIds", []), draft_json)

        pdf_download = client.get(packet["downloadUrl"])
        check("packet downloads as pdf", pdf_download.status_code == 200 and pdf_download.content.startswith(b"%PDF-1.4"), pdf_download.status_code)

        gateway_app.workflows.reload_for_tests()
        persisted = client.get(f"/workflow-runs/{body['runId']}")
        check("workflow persists", persisted.status_code == 200 and persisted.json().get("status") == "approval_required", persisted.text)

        blocked = client.post("/dashboard/try-tool", json={"tool": "send_email_draft", "args": {"draft_id": draft["artifactId"], "approval_id": body["approval"]["approvalId"]}})
        blocked_result = blocked.json().get("result") or {}
        check("pending approval blocks send", blocked.status_code == 200 and blocked_result.get("status") == "approval_required", blocked.text)
finally:
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
