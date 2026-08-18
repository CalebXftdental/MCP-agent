"""Local HTTP smoke for recurring workflow automations."""
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
TMP = ROOT / "_smoke" / ".tmp" / "automations"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "auto_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "auto_password",
    "GOVERNANCE_SESSION_SECRET": "automation-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18032/mcp",
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
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18032", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18032/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    import automation_store  # noqa: E402

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "auto_admin", "password": "auto_password"})
        check("login succeeds", login.status_code == 200)
        now = int(time.time())
        created = client.post("/automations", json={
            "template_id": "shipment_exception_report",
            "display_name": "Smoke shipment exceptions",
            "interval_sec": 3600,
            "next_run_at": now - 1,
            "inputs": {"sample": True, "customer_id": "AUTO100"},
        })
        check("automation created", created.status_code == 201 and created.json().get("automationId"))
        auto_id = created.json()["automationId"]
        listed = client.get("/automations")
        check("automation listed", any(a.get("automationId") == auto_id for a in listed.json().get("automations", [])))
        ran = client.post("/automations/run-due", json={"now": now})
        body = ran.json()
        check("run due executes one", ran.status_code == 200 and body.get("ran") == 1)
        result = (body.get("results") or [{}])[0]
        run = result.get("run") or {}
        check("automation workflow completed", result.get("status") == "completed" and run.get("runId"))
        check("automation created artifact", len(run.get("artifactIds") or []) == 1)
        automation_store.reload_for_tests()
        advanced = client.get(f"/automations/{auto_id}")
        adv = advanced.json()
        check("automation persists and advances", advanced.status_code == 200 and adv.get("lastRunId") == run.get("runId") and adv.get("nextRunAt", 0) > now)
        artifacts = client.get("/artifacts")
        check("automation artifact listed", run["artifactIds"][0] in {a.get("artifactId") for a in artifacts.json().get("artifacts", [])})
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
