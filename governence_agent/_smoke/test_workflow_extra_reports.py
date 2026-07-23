"""Local HTTP smoke for the additional office workflow runners."""
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
TMP = ROOT / "_smoke" / ".tmp" / "workflow-extra-reports"
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "wf_extra_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "wf_extra_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-extra-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18031/mcp",
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
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18031", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18031/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "wf_extra_admin", "password": "wf_extra_password"})
        check("login succeeds", login.status_code == 200)
        catalog = client.get("/workflows").json().get("workflows", [])
        statuses = {w.get("templateId"): w.get("status") for w in catalog}
        check("shipment workflow active", statuses.get("shipment_exception_report") == "active")
        check("vendor workflow active", statuses.get("vendor_ap_summary") == "active")

        shipment = client.post("/workflows/shipment_exception_report/run", json={"sample": True, "customer_id": "SHIP100"})
        ship_body = shipment.json()
        check("shipment workflow created", shipment.status_code == 201)
        check("shipment workflow completed", ship_body.get("status") == "completed")
        check("shipment creates one xlsx", [a.get("type") for a in ship_body.get("artifacts", [])] == ["xlsx"])

        vendor = client.post("/workflows/vendor_ap_summary/run", json={"sample": True, "vendor_code": "VENDX"})
        vendor_body = vendor.json()
        check("vendor workflow created", vendor.status_code == 201)
        check("vendor workflow completed", vendor_body.get("status") == "completed")
        check("vendor creates xlsx and docx", sorted(a.get("type") for a in vendor_body.get("artifacts", [])) == ["docx", "xlsx"])

        for artifact in (ship_body.get("artifacts") or []) + (vendor_body.get("artifacts") or []):
            wb = client.get(f"/artifacts/{artifact['artifactId']}/workbench")
            check(f"workbench {artifact['type']}", wb.status_code == 200 and wb.json().get("artifact", {}).get("artifactId") == artifact["artifactId"])
            dl = client.get(artifact["downloadUrl"])
            check(f"download {artifact['type']}", dl.status_code == 200 and len(dl.content) > 500)
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
