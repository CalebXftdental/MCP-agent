"""Local HTTP smoke for the Customer 360 workflow.

Starts mcp-office on localhost, imports the gateway app, logs in as a seeded
admin user, runs the sample Customer 360 workflow, and downloads its generated
artifacts through the session-gated artifact route.

Run:  .venv/Scripts/python.exe _smoke/test_workflow_customer360.py
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
TMP = ROOT / "_smoke" / ".tmp" / "workflow-customer360"
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "wf_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "wf_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18030/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
    "ONLYOFFICE_DOCUMENT_SERVER_URL": "http://onlyoffice.local",
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
    [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18030", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18030/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "wf_admin", "password": "wf_password"})
        check("login succeeds", login.status_code == 200)

        catalog = client.get("/workflows")
        check("workflow catalog includes customer_360_report", any(w.get("templateId") == "customer_360_report" for w in catalog.json().get("workflows", [])))

        run = client.post("/workflows/customer_360_report/run", json={"sample": True, "customer_id": "SMOKE100"})
        check("workflow run created", run.status_code == 201)
        body = run.json()
        if body.get("status") != "completed":
            print("workflow response", body)
        check("workflow completed", body.get("status") == "completed")
        artifacts = body.get("artifacts") or []
        check("two artifacts created", len(artifacts) == 2)

        listed = client.get("/artifacts")
        listed_ids = {a.get("artifactId") for a in listed.json().get("artifacts", [])}
        check("artifacts listed", all(a.get("artifactId") in listed_ids for a in artifacts))

        gateway_app.workflows.reload_for_tests()
        persisted = client.get(f"/workflow-runs/{body['runId']}")
        check("workflow run persists", persisted.status_code == 200 and persisted.json().get("status") == "completed")
        timeline = client.get(f"/workflow-runs/{body['runId']}?timeline=1")
        timeline_body = timeline.json()
        events = timeline_body.get("timeline") or []
        kinds = {e.get("kind") for e in events}
        tools = {e.get("tool") for e in events}
        check("workflow timeline loads", timeline.status_code == 200 and timeline_body.get("runId") == body["runId"] and events)
        check("timeline includes steps, audit, and artifacts", {"step", "audit", "artifact"}.issubset(kinds))
        check("timeline links office tool calls", {"office_create_excel_report", "office_create_powerpoint_deck"}.issubset(tools))
        check("timeline exposes artifact metadata", len([e for e in events if e.get("kind") == "artifact"]) == len(artifacts))
        evidence = client.post(f"/workflow-runs/{body['runId']}/export-evidence")
        evidence_body = evidence.json()
        evidence_artifact = evidence_body.get("artifact") or {}
        check("evidence export creates artifact", evidence.status_code == 201 and evidence_artifact.get("type") == "json" and evidence_body.get("timelineEvents") >= len(events))
        evidence_download = client.get(evidence_artifact.get("downloadUrl", ""))
        evidence_json = evidence_download.json()
        check("evidence artifact downloads", evidence_download.status_code == 200 and evidence_json.get("kind") == "workflow_evidence_packet")
        check("evidence packet links run and source artifacts", evidence_json.get("run", {}).get("runId") == body["runId"] and len(evidence_json.get("artifacts") or []) == len(artifacts))

        first = artifacts[0]
        workbench = client.get(f"/artifacts/{first['artifactId']}/workbench")
        wb = workbench.json()
        check("artifact workbench loads", workbench.status_code == 200 and wb.get("artifact", {}).get("artifactId") == first["artifactId"])
        check("workbench previews office package", (wb.get("preview") or {}).get("signals", {}).get("hasCoreProperties") is True)
        check("workbench exposes onlyoffice config", (wb.get("onlyoffice") or {}).get("documentServerUrl") == "http://onlyoffice.local")
        review = client.post(f"/artifacts/{first['artifactId']}/request-approval", json={"reason": "Review generated workbook"})
        check("artifact approval requested", review.status_code == 201 and first["artifactId"] in review.json().get("artifactIds", []))

        for artifact in artifacts:
            download = client.get(artifact["downloadUrl"])
            check(f"download {artifact['type']}", download.status_code == 200 and len(download.content) > 500)
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
