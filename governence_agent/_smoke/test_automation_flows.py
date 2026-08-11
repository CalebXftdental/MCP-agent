"""End-to-end proof of 3 leadership-demo-ready SCHEDULED AUTOMATIONS.

Unlike test_my_workflow_demos.py (which ran graphs on-demand via
/workflows/{id}/run), this exercises the actual Automations panel path:
POST /workflow-graphs -> publish -> POST /automations (schedule it) ->
POST /automations/run-due (the scheduler tick an admin/cron would trigger) ->
approve any resulting gate -> resume -> download + verify the artifact.

Flow 1: Daily Shipment Exception Digest   -- fully autonomous, zero gate.
         Ops never touches it; it just produces the workbook every morning.
Flow 2: Weekly Executive Brief -> Leadership -- autonomous PDF + draft, but
         a human must approve before it's emailed to leadership.
Flow 3: Weekly Vendor AP Summary -> Finance sign-off -- autonomous workbook,
         gated on finance approval before being considered released.

This is the real governance story for a leadership demo: routine reporting
runs itself; anything that leaves the building or touches sensitive financial
data still stops for a human. Verifies actual PDF/XLSX byte content, not just
run status, so a "succeeded" result can't hide an empty artifact.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "automation-flows"
OUT = ROOT / "_smoke" / ".tmp" / "automation-flow-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "auto_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "auto_password",
    "GOVERNANCE_SESSION_SECRET": "automation-flows-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18391/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18392/mcp",
    "EMAIL_INTERNAL_DOMAINS": "frontierdental.com",
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


def pdf_text(payload: bytes) -> str:
    raw = payload.decode("latin-1", errors="replace")
    return "\n".join(m.group(1).replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
                      for m in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj", raw))


_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def xlsx_cell_texts(payload: bytes) -> list[str]:
    out: list[str] = []
    with zipfile.ZipFile(__import__("io").BytesIO(payload)) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("s:si", _NS):
                shared.append("".join(t.text or "" for t in si.findall(".//s:t", _NS)))
        for name in zf.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                root = ET.fromstring(zf.read(name))
                for c in root.findall(".//s:c", _NS):
                    inline = c.find("s:is/s:t", _NS)
                    if inline is not None:
                        out.append(inline.text or "")
                        continue
                    v = c.find("s:v", _NS)
                    text = v.text if v is not None else None
                    if text is None:
                        continue
                    if c.get("t") == "s":
                        try:
                            out.append(shared[int(text)])
                        except (ValueError, IndexError):
                            out.append(text)
                    else:
                        out.append(text)
    return out


print("start office + email backends")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18391", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18392", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18391/health")
    wait_health("http://127.0.0.1:18392/health")
    check("office backend healthy", True)
    check("email backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:ops_scheduler", name="ops_scheduler", key_hash="", status="active",
        role="user", type="user", categories=["office", "email_draft", "email_send_external"],
        login_password_hash=hash_password("scheduler_password"),
    ))

    def download(client, artifact_id):
        return client.get(f"/artifacts/{artifact_id}/download")

    def schedule_and_fire(client, gid, display_name):
        auto = client.post("/automations", json={
            "template_id": gid, "display_name": display_name,
            "interval_sec": 86400, "next_run_at": time.time() - 1, "inputs": {},
        })
        check(f"{display_name}: scheduled", auto.status_code == 201, auto.text)
        return auto.json().get("automationId")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "ops_scheduler", "password": "scheduler_password"})

        # ── Flow 1: Daily Shipment Exception Digest -- fully autonomous ────
        shipments = [
            {"customerName": "Sample Dental Group", "orderNumber": "SO-1005", "shipmentStatus": "Exception", "trackingNumber": "1Z-SAMPLE-5", "shipDate": "2026-08-08", "carrier": "FedEx"},
            {"customerName": "Sample Dental Group", "orderNumber": "SO-1006", "shipmentStatus": "Delayed", "trackingNumber": "1Z-SAMPLE-6", "shipDate": "2026-08-09", "carrier": "DHL"},
            {"customerName": "North Clinic Network", "orderNumber": "SO-2011", "shipmentStatus": "Delivered", "trackingNumber": "1Z-SAMPLE-7", "shipDate": "2026-08-07", "carrier": "UPS"},
            {"customerName": "Prairie Ortho", "orderNumber": "SO-3021", "shipmentStatus": "In Transit", "trackingNumber": "1Z-SAMPLE-8", "shipDate": "2026-08-09", "carrier": "UPS"},
        ]
        exceptions = [r for r in shipments if r["shipmentStatus"] not in ("Delivered",)]
        tables_1 = [
            {"name": "Summary", "rows": [
                {"Metric": "Shipments reviewed", "Value": len(shipments)},
                {"Metric": "Open exceptions", "Value": len(exceptions)},
            ]},
            {"name": "Exceptions", "rows": exceptions},
            {"name": "All Shipments", "rows": shipments},
        ]
        g1 = client.post("/workflow-graphs", json={
            "displayName": "Daily Shipment Exception Digest",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_excel_report", "title": "Build exception workbook",
                 "config": {"title": "Shipment Exception Digest - 2026-08-10", "tables": tables_1, "classification": ["INTERNAL"]}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        check("flow1 graph created", g1.status_code == 201, g1.text)
        gid1 = g1.json()["graphId"]
        client.post(f"/workflow-graphs/{gid1}/publish", json={})
        aut1 = schedule_and_fire(client, gid1, "Daily Shipment Exception Digest")

        # ── Flow 2: Weekly Executive Brief -> Leadership (gated send) ───────
        sections_2 = [
            {"heading": "Weekly Executive Summary", "bullets": [
                "Period: 2026-08-03 to 2026-08-10",
                "Revenue represented in top accounts: 142421.60",
                "Open executive risks: 3",
            ]},
            {"heading": "Top Account Signal", "bullets": [
                "Top account: Sample Dental Group",
                "Spend: 42150.75",
                "Watch item: Shipment delays",
            ]},
        ]
        tables_2 = [{"name": "Top Customers", "rows": [
            {"rank": 1, "customerId": "CUST-100", "name": "Sample Dental Group", "totalSpend": 42150.75},
            {"rank": 2, "customerId": "CUST-245", "name": "North Clinic Network", "totalSpend": 38200.00},
        ]}]
        g2 = client.post("/workflow-graphs", json={
            "displayName": "Weekly Executive Brief -> Leadership",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_pdf_packet", "title": "Build executive brief",
                 "config": {"title": "Weekly Executive Brief", "sections": sections_2, "tables": tables_2, "classification": ["INTERNAL"]}},
                {"nodeId": "n2", "kind": "tool_call", "tool": "create_email_draft", "title": "Draft brief email",
                 "config": {"to": ["leadership@frontierdental.com"], "subject": "Weekly Executive Brief - 2026-08-10",
                            "body_markdown": "Hi team,\n\nThis week's brief: top account Sample Dental Group (42150.75), 3 open risks.\n\nRegards,\nGoverned AI Office Assistant",
                            "classification": ["INTERNAL"]}},
                {"nodeId": "g1", "kind": "approval_gate", "config": {"reason": "Sign off before the brief reaches leadership's inbox", "risk_level": "medium"}},
                {"nodeId": "n3", "kind": "tool_call", "tool": "send_email_draft", "title": "Send brief email",
                 "inputBindings": {"draft_id": {"source": "node", "node_id": "n2", "path": "draftId"},
                                    "approval_id": {"source": "node", "node_id": "g1", "path": "approvalId"}}},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"},
                {"edgeId": "e3", "sourceNodeId": "n2", "targetNodeId": "g1"},
                {"edgeId": "e4", "sourceNodeId": "g1", "targetNodeId": "n3"},
            ],
        })
        check("flow2 graph created", g2.status_code == 201, g2.text)
        gid2 = g2.json()["graphId"]
        client.post(f"/workflow-graphs/{gid2}/publish", json={})
        aut2 = schedule_and_fire(client, gid2, "Weekly Executive Brief -> Leadership")

        # ── Flow 3: Weekly Vendor AP Summary -> Finance sign-off ────────────
        invoices = [
            {"invoiceNumber": "AP-2001", "docType": "Bill", "invoiceDate": "2026-08-01", "dueDate": "2026-08-31", "lineTotal": 3200.00, "taxTotal": 208.00, "paid": False},
            {"invoiceNumber": "AP-2002", "docType": "Bill", "invoiceDate": "2026-08-03", "dueDate": "2026-09-02", "lineTotal": 1180.50, "taxTotal": 76.73, "paid": False},
        ]
        tables_3 = [
            {"name": "Summary", "rows": [
                {"Metric": "Vendor Code", "Value": "VEND220"},
                {"Metric": "Vendor Name", "Value": "Northgate Dental Supply"},
                {"Metric": "Invoice Count", "Value": len(invoices)},
                {"Metric": "Unpaid Count", "Value": sum(1 for r in invoices if not r["paid"])},
            ]},
            {"name": "AP Invoices", "rows": invoices},
        ]
        g3 = client.post("/workflow-graphs", json={
            "displayName": "Weekly Vendor AP Summary -> Finance",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_excel_report", "title": "Build AP workbook",
                 "config": {"title": "Vendor AP Summary - VEND220", "tables": tables_3, "classification": ["PII", "SENSITIVE"]}},
                {"nodeId": "g1", "kind": "approval_gate", "config": {"reason": "Finance sign-off before distributing the vendor AP workbook", "risk_level": "medium"}},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "g1"},
            ],
        })
        check("flow3 graph created", g3.status_code == 201, g3.text)
        gid3 = g3.json()["graphId"]
        client.post(f"/workflow-graphs/{gid3}/publish", json={})
        aut3 = schedule_and_fire(client, gid3, "Weekly Vendor AP Summary -> Finance")

        # ── Fire the scheduler tick (what a cron/admin trigger does) ───────
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "auto_admin", "password": "auto_password"})
        due = client.post("/automations/run-due", json={"now": time.time()})
        check("run-due call succeeds", due.status_code == 200, due.text)
        due_body = due.json()
        check("all 3 due automations ran", due_body.get("ran") == 3, due_body)
        results = {r["automationId"]: r for r in due_body.get("results", [])}

        r1 = results.get(aut1, {})
        check("flow1 (no gate) completed fully autonomously", r1.get("status") == "completed", r1)
        run1 = r1.get("run") or {}
        aid1 = next((a for a in run1.get("artifactIds", [])), None)
        check("flow1 produced an artifact", bool(aid1), run1)

        r2 = results.get(aut2, {})
        check("flow2 paused at the leadership-send gate", r2.get("status") == "approval_required", r2)
        run2 = r2.get("run") or {}
        run2_id = run2.get("runId")
        approval2 = ((r2.get("run") or {}).get("approvalIds") or [None])[-1]

        r3 = results.get(aut3, {})
        check("flow3 paused at the finance sign-off gate", r3.get("status") == "approval_required", r3)
        run3 = r3.get("run") or {}
        run3_id = run3.get("runId")
        approval3 = ((r3.get("run") or {}).get("approvalIds") or [None])[-1]

        # ── Approve + resume flow 2 and flow 3 (finance/leadership sign-off) ─
        approve2 = client.post(f"/approvals/{approval2}/approve", json={"note": "brief looks good, ok to send"})
        check("flow2 gate approved", approve2.status_code == 200, approve2.text)
        resume2 = client.post(f"/workflow-runs/{run2_id}/resume", json={"approval_id": approval2})
        resume2_body = resume2.json()
        check("flow2 completes after approval", resume2.status_code == 200 and resume2_body.get("status") == "completed", resume2_body)
        n3_step = next((s for s in resume2_body.get("steps", []) if s.get("stepId") == "n3"), {})
        check("flow2 email actually sent (sendId present)", n3_step.get("status") == "completed" and n3_step.get("outputs", {}).get("sendId"), n3_step)
        aid2_pdf = next((a for a in resume2_body.get("artifactIds", [])), None)

        approve3 = client.post(f"/approvals/{approval3}/approve", json={"note": "financials confirmed"})
        check("flow3 gate approved", approve3.status_code == 200, approve3.text)
        resume3 = client.post(f"/workflow-runs/{run3_id}/resume", json={"approval_id": approval3})
        resume3_body = resume3.json()
        check("flow3 completes after approval", resume3.status_code == 200 and resume3_body.get("status") == "completed", resume3_body)
        aid3 = next((a for a in resume3_body.get("artifactIds", [])), None)

        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "ops_scheduler", "password": "scheduler_password"})

        # ── Download + verify real content in every artifact ────────────────
        dl1 = download(client, aid1)
        check("flow1 artifact downloads", dl1.status_code == 200, dl1.status_code)
        (OUT / "flow1-shipment-exception-digest.xlsx").write_bytes(dl1.content)
        cells1 = xlsx_cell_texts(dl1.content)
        for needle in ("Open exceptions", "SO-1005", "SO-1006", "Sample Dental Group", "Exception", "Delayed"):
            check(f"flow1 XLSX contains {needle!r}", any(needle == c or needle in c for c in cells1), cells1[:20])

        dl2 = download(client, aid2_pdf)
        check("flow2 PDF artifact downloads", dl2.status_code == 200, dl2.status_code)
        (OUT / "flow2-weekly-executive-brief.pdf").write_bytes(dl2.content)
        text2 = pdf_text(dl2.content)
        for needle in ("Weekly Executive Brief", "Top Account Signal", "Sample Dental Group", "42150.75"):
            check(f"flow2 PDF contains {needle!r}", needle in text2, text2[:400])

        dl3 = download(client, aid3)
        check("flow3 XLSX artifact downloads", dl3.status_code == 200, dl3.status_code)
        (OUT / "flow3-vendor-ap-summary.xlsx").write_bytes(dl3.content)
        cells3 = xlsx_cell_texts(dl3.content)
        for needle in ("Vendor Code", "VEND220", "Northgate Dental Supply", "AP-2001", "AP-2002"):
            check(f"flow3 XLSX contains {needle!r}", any(needle == c or needle in c for c in cells3), cells3[:20])

finally:
    office.terminate()
    email.terminate()
    for proc in (office, email):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

print(f"\n{PASS} passed, {FAIL} failed")
print(f"artifacts written to: {OUT}")
sys.exit(1 if FAIL else 0)
