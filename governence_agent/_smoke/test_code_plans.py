"""Local HTTP smoke for read-only governed code planning."""
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
TMP = ROOT / "_smoke" / ".tmp" / "code_plans"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "code_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "code_password",
    "GOVERNANCE_VIEWER_USER": "code_viewer",
    "GOVERNANCE_VIEWER_PASSWORD": "viewer_password",
    "GOVERNANCE_SESSION_SECRET": "code-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_CODE_PLAN_STORE_FILE": str(TMP / "state" / "code_plans.json"),
    "CODE_MCP_URL": "http://127.0.0.1:18070/mcp",
    "CODE_AUTOMATION_REPO_ROOT": str(ROOT),
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


print("start code backend")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
code = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18070", "--no-access-log"],
    cwd=str(ROOT / "mcp-code"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18070/health")
    check("code backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from policy import manifest
    from policy.categories import get_category

    check("code backend registered", "code" in manifest.backends())
    check("code planning category exists", get_category("code_planning") is not None)
    check("code plan tool is namespaced", manifest.namespaced("opencode_plan_change") == "code_opencode_plan_change")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "code_admin", "password": "code_password"})
        check("admin login succeeds", login.status_code == 200, login.text)

        plan = client.post("/dashboard/try-tool", json={
            "tool": "opencode_plan_change",
            "args": {
                "request": "Add monthly customer report workflow with Excel and PowerPoint outputs",
                "target_area": "workflow",
                "risk_level": "medium",
            },
        })
        body = plan.json().get("result", {}) if plan.headers.get("content-type", "").startswith("application/json") else {}
        check("plan call succeeds", plan.status_code == 200 and body.get("status") == "success", plan.text)
        pid = body.get("planId")
        check("plan requires approval", body.get("requiresApproval") is True and body.get("riskLevel") == "medium", body)
        check("plan proposes workflow files", any("gateway/workflows.py" in f or "gateway/static/app.html" in f for f in body.get("proposedFiles", [])), body.get("proposedFiles"))

        template = client.post("/dashboard/try-tool", json={
            "tool": "opencode_generate_template",
            "args": {"goal": "Create a customer renewal packet workflow", "template_type": "workflow"},
        })
        t_body = template.json().get("result", {})
        check("template plan succeeds", template.status_code == 200 and t_body.get("templateDraft", {}).get("goal"), template.text)

        listed = client.get("/code-plans?all=1")
        plans = listed.json().get("plans", [])
        check("admin lists plans", listed.status_code == 200 and len(plans) >= 2, listed.text)

        detail = client.get(f"/code-plans/{pid}")
        check("plan detail includes commands", detail.status_code == 200 and detail.json().get("proposedCommands"), detail.text)

        approval = client.post(f"/code-plans/{pid}/request-approval", json={"reason": "Review before any opencode edit run"})
        check("approval requested for plan", approval.status_code == 201 and approval.json().get("approval", {}).get("status") == "pending", approval.text)

        gateway_app.code_plan_store.reload_for_tests()
        persisted = client.get(f"/code-plans/{pid}")
        check("plan persists after reload", persisted.status_code == 200 and persisted.json().get("approvalId"), persisted.text)

        client.post("/dashboard/logout")
        viewer_login = client.post("/dashboard/login", json={"username": "code_viewer", "password": "viewer_password"})
        check("viewer login succeeds", viewer_login.status_code == 200, viewer_login.text)
        viewer_detail = client.get(f"/code-plans/{pid}")
        check("viewer cannot read admin plan", viewer_detail.status_code == 403, viewer_detail.text)
finally:
    code.terminate()
    try:
        code.wait(timeout=5)
    except subprocess.TimeoutExpired:
        code.kill()

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
