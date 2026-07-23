"""Local HTTP smoke for assistant workflow suggestions and launch."""
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
TMP = ROOT / "_smoke" / ".tmp" / "assistant-workflow-suggestions"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "suggest_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "suggest_password",
    "GOVERNANCE_SESSION_SECRET": "assistant-workflow-suggest-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18046/mcp",
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
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18046", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18046/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "suggest_admin", "password": "suggest_password"})
        check("admin login succeeds", login.status_code == 200)

        suggest = client.post("/workflow-suggestions", json={"message": "Prepare a customer account review deck for customer CUST777 with shipment notes", "limit": 3})
        body = suggest.json()
        suggestions = body.get("suggestions", [])
        ids = [s.get("templateId") for s in suggestions]
        check("suggestions returned", suggest.status_code == 200 and suggestions, body)
        check("customer review suggested first", ids[0] == "customer_360_report", ids)
        check("suggestion includes launch inputs", suggestions[0].get("inputs", {}).get("customer_id"), suggestions[0])

        launch = client.post("/workflow-suggestions/launch", json={"template_id": "shipment_exception_report", "inputs": {"sample": True, "customer_id": "SUG100"}})
        launch_body = launch.json()
        check("suggested workflow launches", launch.status_code == 201 and launch_body.get("status") == "completed" and launch_body.get("runId"), launch.text)
        check("launch creates artifact", len(launch_body.get("artifactIds") or []) == 1 and launch_body.get("artifacts", [{}])[0].get("downloadUrl"), launch_body)

        disabled = client.post("/admin/workflows/shipment_exception_report/disable", json={"reason": "suggest smoke"})
        check("template disabled", disabled.status_code == 200 and disabled.json().get("status") == "disabled", disabled.text)
        hidden = client.post("/workflow-suggestions", json={"message": "shipment delay exception report for customer SUG100", "limit": 3})
        hidden_ids = [s.get("templateId") for s in hidden.json().get("suggestions", [])]
        check("disabled workflow hidden from suggestions", "shipment_exception_report" not in hidden_ids, hidden.json())
        blocked = client.post("/workflow-suggestions/launch", json={"template_id": "shipment_exception_report", "inputs": {"sample": True}})
        check("disabled launch blocked", blocked.status_code == 403 and blocked.json().get("error") == "workflow template is disabled", blocked.text)
        client.post("/admin/workflows/shipment_exception_report/enable", json={})
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)