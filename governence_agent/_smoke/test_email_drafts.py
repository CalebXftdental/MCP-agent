"""Local HTTP smoke for governed email draft creation.

Starts mcp-email, imports the gateway app, logs in as a seeded admin, creates an
email draft through /dashboard/try-tool, and downloads the resulting draft
artifact.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "email-drafts"
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "email_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "email_password",
    "GOVERNANCE_SESSION_SECRET": "email-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "EMAIL_MCP_URL": "http://127.0.0.1:18040/mcp",
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


print("start email backend")
email = subprocess.Popen(
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18040", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18040/health")
    check("email backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "email_admin", "password": "email_password"})
        check("login succeeds", login.status_code == 200)

        draft = client.post("/dashboard/try-tool", json={
            "tool": "create_email_draft",
            "args": {
                "to": ["manager@example.com"],
                "subject": "Customer 360 follow-up",
                "body_markdown": "Please review the generated account packet.",
                "classification": ["INTERNAL"],
            },
        })
        check("draft tool call succeeds", draft.status_code == 200)
        result = draft.json().get("result") or {}
        check("draft created", result.get("status") == "success" and result.get("draftId"))
        blocked_send = client.post("/dashboard/try-tool", json={"tool": "send_email_draft", "args": {"draft_id": result["draftId"], "approval_id": "missing"}})
        blocked_result = blocked_send.json().get("result") or {}
        check("send requires approval", blocked_send.status_code == 200 and blocked_result.get("status") == "approval_required")

        approval = client.post("/approvals", json={"reason": "Send draft externally", "artifact_ids": [result["artifactId"]], "risk_level": "medium"})
        check("approval requested", approval.status_code == 201 and approval.json().get("status") == "pending")
        approval_id = approval.json()["approvalId"]
        approved = client.post(f"/approvals/{approval_id}/approve", json={"note": "smoke ok"})
        check("approval approved", approved.status_code == 200 and approved.json().get("status") == "approved")
        gateway_app.approval_store.reload_for_tests()
        persisted = client.get("/approvals")
        check("approval persists", any(a.get("approvalId") == approval_id and a.get("status") == "approved" for a in persisted.json().get("approvals", [])))

        queued = client.post("/dashboard/try-tool", json={"tool": "send_email_draft", "args": {"draft_id": result["draftId"], "approval_id": approval_id}})
        queued_result = queued.json().get("result") or {}
        check("approved send queued", queued.status_code == 200 and queued_result.get("sendStatus") == "queued_for_connector" and queued_result.get("sendId"))
        gateway_app.email_send_store.reload_for_tests()
        sends = client.get("/email-sends")
        check("send queue lists record", any(s.get("sendId") == queued_result.get("sendId") for s in sends.json().get("sends", [])))
        send_item = client.get(f"/email-sends/{queued_result['sendId']}")
        check("send queue item persists", send_item.status_code == 200 and send_item.json().get("approvalId") == approval_id)

        download = client.get(result["downloadUrl"])
        check("draft artifact downloads", download.status_code == 200 and b"Customer 360 follow-up" in download.content)
finally:
    email.terminate()
    try:
        email.wait(timeout=5)
    except subprocess.TimeoutExpired:
        email.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
