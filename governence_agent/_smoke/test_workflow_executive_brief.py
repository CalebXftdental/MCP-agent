"""Local HTTP smoke for the Weekly Executive Brief workflow."""
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
TMP = ROOT / "_smoke" / ".tmp" / "workflow-executive-brief"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "wf_exec_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "wf_exec_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-exec-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18038/mcp",
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
print("start office backend")
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18038", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18038/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "wf_exec_admin", "password": "wf_exec_password"})
        check("login succeeds", login.status_code == 200, login.text)
        catalog = client.get("/workflows").json().get("workflows", [])
        check("executive brief active", any(w.get("templateId") == "weekly_executive_brief" and w.get("status") == "active" for w in catalog), catalog)

        run = client.post("/workflows/weekly_executive_brief/run", json={
            "sample": True,
            "start_date": "2026-07-15",
            "end_date": "2026-07-22",
            "request_approval": True,
        })
        body = run.json()
        if body.get("status") != "approval_required":
            print("workflow response", body)
        check("workflow run created", run.status_code == 201, run.text)
        check("workflow paused for approval", body.get("status") == "approval_required", body)
        artifacts = body.get("artifacts") or []
        check("xlsx pptx pdf created", sorted(a.get("type") for a in artifacts) == ["pdf", "pptx", "xlsx"], artifacts)
        check("review approval created", body.get("approval", {}).get("status") == "pending" and len(body.get("approval", {}).get("artifactIds", [])) == 3, body.get("approval"))

        for artifact in artifacts:
            download = client.get(artifact["downloadUrl"])
            if artifact["type"] == "pdf":
                check("download pdf", download.status_code == 200 and download.content.startswith(b"%PDF-1.4"), download.status_code)
            else:
                check(f"download {artifact['type']}", download.status_code == 200 and len(download.content) > 500, download.status_code)

        gateway_app.workflows.reload_for_tests()
        persisted = client.get(f"/workflow-runs/{body['runId']}")
        check("workflow persists", persisted.status_code == 200 and persisted.json().get("status") == "approval_required", persisted.text)
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
