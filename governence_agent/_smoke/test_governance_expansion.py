"""Local HTTP smoke for the expansion.md §7 governance additions:
  - files category gating raw artifact upload (POST /artifacts)
  - email_send_internal domain-based approval bypass
  - workflow_admin / agent_admin additive (non-admin) route access
  - workflow_runner opt-in blanket gate on running any workflow template
"""
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
TMP = ROOT / "_smoke" / ".tmp" / "governance-expansion"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "gov_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "gov_password",
    "GOVERNANCE_SESSION_SECRET": "governance-expansion-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "EMAIL_MCP_URL": "http://127.0.0.1:18041/mcp",
    "EMAIL_INTERNAL_DOMAINS": "example.com",
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


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
from policy.categories import get_category  # noqa: E402

print("start email backend")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18041", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18041/health")
    check("email backend healthy", True)

    gateway_app = importlib.import_module("app")
    from store.models import ConsumerRecord  # noqa: E402

    check("files category exists", get_category("files") is not None)
    check("email_send_internal category exists", get_category("email_send_internal") is not None)
    check("workflow_runner category exists", get_category("workflow_runner") is not None)
    check("workflow_admin category exists", get_category("workflow_admin") is not None)
    check("agent_admin category exists", get_category("agent_admin") is not None)

    store = gateway_app.get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:no_files", name="no_files_user", key_hash="", status="active",
        role="user", type="user", categories=["orders"],
        login_password_hash=gateway_app.hash_password("no_files_password"),
    ))
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:has_files", name="has_files_user", key_hash="", status="active",
        role="user", type="user", categories=["orders", "files"],
        login_password_hash=gateway_app.hash_password("has_files_password"),
    ))
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:wf_admin", name="wf_admin_user", key_hash="", status="active",
        role="user", type="user", categories=["workflow_admin"],
        login_password_hash=gateway_app.hash_password("wf_admin_password"),
    ))
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:agent_admin", name="agent_admin_user", key_hash="", status="active",
        role="user", type="user", categories=["agent_admin"],
        login_password_hash=gateway_app.hash_password("agent_admin_password"),
    ))
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:no_admin_cats", name="no_admin_cats_user", key_hash="", status="active",
        role="user", type="user", categories=["orders"],
        login_password_hash=gateway_app.hash_password("no_admin_cats_password"),
    ))
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:no_wf_runner", name="no_wf_runner_user", key_hash="", status="active",
        role="user", type="user", categories=["office", "shipments"],
        login_password_hash=gateway_app.hash_password("no_wf_runner_password"),
    ))
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:wf_runner", name="wf_runner_user", key_hash="", status="active",
        role="user", type="user", categories=["office", "shipments", "workflow_runner"],
        login_password_hash=gateway_app.hash_password("wf_runner_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        admin_login = client.post("/dashboard/login", json={"username": "gov_admin", "password": "gov_password"})
        check("admin login succeeds", admin_login.status_code == 200, admin_login.text)

        # ── files category gates raw artifact upload ──────────────────────
        no_files_login = client.post("/dashboard/login", json={"username": "no_files_user", "password": "no_files_password"})
        check("no_files login succeeds", no_files_login.status_code == 200, no_files_login.text)
        blocked_upload = client.post("/artifacts", json={"title": "note", "filename": "note.txt", "text": "hello"})
        check("upload blocked without files category", blocked_upload.status_code == 403, blocked_upload.text)
        client.post("/dashboard/logout")

        has_files_login = client.post("/dashboard/login", json={"username": "has_files_user", "password": "has_files_password"})
        check("has_files login succeeds", has_files_login.status_code == 200, has_files_login.text)
        allowed_upload = client.post("/artifacts", json={"title": "note", "filename": "note.txt", "text": "hello"})
        check("upload allowed with files category", allowed_upload.status_code == 201, allowed_upload.text)
        client.post("/dashboard/logout")

        admin_login2 = client.post("/dashboard/login", json={"username": "gov_admin", "password": "gov_password"})
        admin_upload = client.post("/artifacts", json={"title": "admin note", "filename": "admin-note.txt", "text": "hi"})
        check("admin upload bypasses files category", admin_upload.status_code == 201, admin_upload.text)

        # ── email_send_internal: internal-only recipients skip approval ───
        internal_draft = client.post("/dashboard/try-tool", json={
            "tool": "create_email_draft",
            "args": {"to": ["teammate@example.com"], "subject": "FYI", "body_markdown": "internal note", "classification": ["INTERNAL"]},
        })
        internal_draft_id = (internal_draft.json().get("result") or {}).get("draftId")
        check("internal draft created", internal_draft.status_code == 200 and internal_draft_id, internal_draft.text)
        internal_send = client.post("/dashboard/try-tool", json={"tool": "send_email_draft", "args": {"draft_id": internal_draft_id, "approval_id": ""}})
        internal_send_result = internal_send.json().get("result") or {}
        check("all-internal-domain send bypasses approval", internal_send_result.get("status") == "success", internal_send_result)

        mixed_draft = client.post("/dashboard/try-tool", json={
            "tool": "create_email_draft",
            "args": {"to": ["teammate@example.com", "customer@outside.com"], "subject": "Update", "body_markdown": "mixed note", "classification": ["INTERNAL"]},
        })
        mixed_draft_id = (mixed_draft.json().get("result") or {}).get("draftId")
        mixed_send = client.post("/dashboard/try-tool", json={"tool": "send_email_draft", "args": {"draft_id": mixed_draft_id, "approval_id": ""}})
        mixed_send_result = mixed_send.json().get("result") or {}
        check("mixed-domain send still requires approval", mixed_send_result.get("status") == "approval_required", mixed_send_result)
        client.post("/dashboard/logout")

        # ── workflow_admin / agent_admin: additive non-admin access ───────
        no_cats_login = client.post("/dashboard/login", json={"username": "no_admin_cats_user", "password": "no_admin_cats_password"})
        blocked_health = client.get("/admin/workflow-health")
        check("workflow-health blocked without workflow_admin", blocked_health.status_code == 403, blocked_health.text)
        blocked_agents = client.get("/admin/agents")
        check("admin/agents blocked without agent_admin", blocked_agents.status_code == 403, blocked_agents.text)
        client.post("/dashboard/logout")

        wf_admin_login = client.post("/dashboard/login", json={"username": "wf_admin_user", "password": "wf_admin_password"})
        check("wf_admin login succeeds", wf_admin_login.status_code == 200, wf_admin_login.text)
        allowed_health = client.get("/admin/workflow-health")
        check("workflow-health allowed with workflow_admin category (non-admin)", allowed_health.status_code == 200, allowed_health.text)
        blocked_agents2 = client.get("/admin/agents")
        check("workflow_admin alone does not grant agent_admin routes", blocked_agents2.status_code == 403, blocked_agents2.text)
        client.post("/dashboard/logout")

        agent_admin_login = client.post("/dashboard/login", json={"username": "agent_admin_user", "password": "agent_admin_password"})
        check("agent_admin login succeeds", agent_admin_login.status_code == 200, agent_admin_login.text)
        allowed_agents = client.get("/admin/agents")
        check("admin/agents allowed with agent_admin category (non-admin)", allowed_agents.status_code == 200, allowed_agents.text)
        client.post("/dashboard/logout")

        # ── workflow_runner: opt-in blanket gate ──────────────────────────
        os.environ["GOVERNANCE_REQUIRE_WORKFLOW_RUNNER_CATEGORY"] = "true"
        no_runner_login = client.post("/dashboard/login", json={"username": "no_wf_runner_user", "password": "no_wf_runner_password"})
        blocked_preflight = client.post("/workflows/shipment_exception_report/preflight", json={"sample": True})
        blocked_preflight_body = blocked_preflight.json()
        check("workflow_runner gate blocks run when opted in", "workflow_runner" in blocked_preflight_body.get("missingCategories", []), blocked_preflight_body)
        client.post("/dashboard/logout")

        runner_login = client.post("/dashboard/login", json={"username": "wf_runner_user", "password": "wf_runner_password"})
        allowed_preflight = client.post("/workflows/shipment_exception_report/preflight", json={"sample": True})
        allowed_preflight_body = allowed_preflight.json()
        check("workflow_runner category clears the opt-in gate", "workflow_runner" not in allowed_preflight_body.get("missingCategories", []), allowed_preflight_body)
        os.environ.pop("GOVERNANCE_REQUIRE_WORKFLOW_RUNNER_CATEGORY", None)

        off_by_default_login = client.post("/dashboard/login", json={"username": "no_wf_runner_user", "password": "no_wf_runner_password"})
        off_preflight = client.post("/workflows/shipment_exception_report/preflight", json={"sample": True})
        off_preflight_body = off_preflight.json()
        check("workflow_runner gate is off by default", "workflow_runner" not in off_preflight_body.get("missingCategories", []), off_preflight_body)
finally:
    email.terminate()
    try:
        email.wait(timeout=5)
    except subprocess.TimeoutExpired:
        email.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
