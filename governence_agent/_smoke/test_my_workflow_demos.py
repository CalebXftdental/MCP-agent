"""End-to-end smoke test for 3 demo "My Workflow" graphs, built the same way the
fixed canvas UI now builds them (literal JSON config for array/object tool args).

Exercises the real gateway + mcp-office + mcp-email backends over HTTP, exactly
like _smoke/test_workflow_graphs.py, then downloads the produced PDF/XLSX
artifacts and asserts they actually contain the authored content (not just a
title) -- this is the regression check for the "PDF only has a title" bug.

Demo 1: Customer 360 PDF Packet            -- rich sections + tables, no gate
Demo 2: Vendor AP Summary Excel + approval  -- rich tables, manual approval gate
Demo 3: Weekly Executive Brief PDF -> email -- PDF + draft + approval + governed send
"""
from __future__ import annotations

import importlib
import os
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
TMP = ROOT / "_smoke" / ".tmp" / "my-workflow-demos"
OUT = ROOT / "_smoke" / ".tmp" / "my-workflow-demo-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "demo_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "demo_password",
    "GOVERNANCE_SESSION_SECRET": "my-workflow-demos-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18191/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18192/mcp",
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
    """The hand-rolled PDF writer never compresses streams -- every line is a
    literal `(...) Tj` op -- so pulling the text back out is just regex, no
    PDF parser needed."""
    import re
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18191", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18192", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18191/health")
    wait_health("http://127.0.0.1:18192/health")
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
        consumer_id="user:demo_builder", name="demo_builder", key_hash="", status="active",
        role="user", type="user", categories=["office", "email_draft", "email_send_external"],
        login_password_hash=hash_password("builder_password"),
    ))

    def download(client, artifact_id):
        resp = client.get(f"/artifacts/{artifact_id}/download")
        return resp

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "demo_builder", "password": "builder_password"})

        # ── Demo 1: Customer 360 PDF Packet ─────────────────────────────────
        sections_1 = [
            {"heading": "Executive Summary", "bullets": [
                "Customer: Sample Dental Group",
                "Orders reviewed: 4",
                "Total spend in scope: 12345.67",
            ]},
            {"heading": "Shipment Status", "bullets": [
                "Shipment records reviewed: 3",
                "Open or in-transit shipments: 1",
            ]},
            {"heading": "Recommended Next Steps", "bullets": [
                "Review open orders and shipment exceptions.",
                "Use the spreadsheet appendix for line-level follow-up.",
                "Route externally-facing communication through approval before sending.",
            ]},
        ]
        tables_1 = [
            {"name": "Orders", "rows": [
                {"orderNumber": "SO-1001", "status": "Completed", "date": "2026-06-01", "total": 4200.00},
                {"orderNumber": "SO-1002", "status": "Completed", "date": "2026-06-14", "total": 3050.50},
                {"orderNumber": "SO-1003", "status": "Open", "date": "2026-07-02", "total": 1995.17},
            ]},
        ]
        demo1 = client.post("/workflow-graphs", json={
            "displayName": "Customer 360 PDF Packet",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_pdf_packet",
                 "title": "Build customer 360 PDF",
                 "config": {"title": "Customer 360 - Sample Dental Group", "sections": sections_1, "tables": tables_1, "classification": ["INTERNAL"]}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        check("demo1 graph created", demo1.status_code == 201, demo1.text)
        gid1 = demo1.json()["graphId"]
        pub1 = client.post(f"/workflow-graphs/{gid1}/publish", json={})
        check("demo1 published", pub1.status_code == 200, pub1.text)
        run1 = client.post(f"/workflows/{gid1}/run", json={})
        run1_body = run1.json()
        check("demo1 run completes", run1.status_code == 201 and run1_body.get("status") == "completed", run1_body)
        artifact1 = (run1_body.get("artifacts") or [{}])[0]
        aid1 = artifact1.get("artifactId")
        check("demo1 produced an artifact id", bool(aid1), run1_body)
        dl1 = download(client, aid1)
        check("demo1 artifact downloads", dl1.status_code == 200, dl1.status_code)
        (OUT / "demo1-customer-360.pdf").write_bytes(dl1.content)
        text1 = pdf_text(dl1.content)
        for needle in ("Customer 360 - Sample Dental Group", "Executive Summary", "Customer: Sample Dental Group",
                       "Recommended Next Steps", "SO-1001", "4200"):
            check(f"demo1 PDF contains {needle!r}", needle in text1, text1[:400])
        check("demo1 PDF has more than just a title (regression check)", len(text1.splitlines()) > 5, text1)

        # ── Demo 2: Vendor AP Summary Excel + approval gate ─────────────────
        invoices = [
            {"invoiceNumber": "AP-1001", "docType": "Bill", "invoiceDate": "2026-06-20", "dueDate": "2026-07-20", "lineTotal": 1250.00, "taxTotal": 81.25, "paid": True},
            {"invoiceNumber": "AP-1002", "docType": "Bill", "invoiceDate": "2026-07-01", "dueDate": "2026-07-31", "lineTotal": 2480.40, "taxTotal": 161.23, "paid": False},
            {"invoiceNumber": "AP-1003", "docType": "Bill", "invoiceDate": "2026-07-10", "dueDate": "2026-08-09", "lineTotal": 775.50, "taxTotal": 50.41, "paid": False},
        ]
        tables_2 = [
            {"name": "Summary", "rows": [
                {"Metric": "Vendor Code", "Value": "VEND100"},
                {"Metric": "Vendor Name", "Value": "Sample Supply Co."},
                {"Metric": "Invoice Count", "Value": len(invoices)},
                {"Metric": "Unpaid Count", "Value": sum(1 for r in invoices if not r["paid"])},
            ]},
            {"name": "AP Invoices", "rows": invoices},
        ]
        demo2 = client.post("/workflow-graphs", json={
            "displayName": "Vendor AP Summary Excel",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_excel_report",
                 "title": "Build vendor AP workbook",
                 "config": {"title": "Vendor AP Summary - VEND100", "tables": tables_2, "classification": ["PII", "SENSITIVE"]}},
                {"nodeId": "g1", "kind": "approval_gate",
                 "config": {"reason": "Finance sign-off before distributing vendor AP workbook", "risk_level": "medium"}},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "g1"},
            ],
        })
        check("demo2 graph created", demo2.status_code == 201, demo2.text)
        gid2 = demo2.json()["graphId"]
        client.post(f"/workflow-graphs/{gid2}/publish", json={})
        run2 = client.post(f"/workflows/{gid2}/run", json={})
        run2_body = run2.json()
        check("demo2 run pauses at the manual gate", run2.status_code == 201 and run2_body.get("status") == "approval_required", run2_body)
        artifact2 = (run2_body.get("artifacts") or [{}])[0]
        aid2 = artifact2.get("artifactId")
        check("demo2 excel artifact already exists before approval", bool(aid2), run2_body)
        appr2 = run2_body.get("approval", {}).get("approvalId")

        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "demo_admin", "password": "demo_password"})
        approve2 = client.post(f"/approvals/{appr2}/approve", json={"note": "financials look right"})
        check("demo2 admin approves the gate", approve2.status_code == 200, approve2.text)
        resumed2 = client.post(f"/workflow-runs/{run2_body['runId']}/resume", json={"approval_id": appr2})
        check("demo2 resume completes the run", resumed2.status_code == 200 and resumed2.json().get("status") == "completed", resumed2.text)
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "demo_builder", "password": "builder_password"})

        dl2 = download(client, aid2)
        check("demo2 artifact downloads", dl2.status_code == 200, dl2.status_code)
        (OUT / "demo2-vendor-ap-summary.xlsx").write_bytes(dl2.content)
        cells2 = xlsx_cell_texts(dl2.content)
        for needle in ("Vendor Code", "VEND100", "Sample Supply Co.", "AP-1001", "AP-1002", "Unpaid Count"):
            check(f"demo2 XLSX contains {needle!r}", any(needle == c or needle in c for c in cells2), cells2[:20])
        check("demo2 XLSX has more than a couple cells (regression check)", len(cells2) > 10, len(cells2))

        # ── Demo 3: Weekly Executive Brief PDF -> draft -> approval -> send ──
        sections_3 = [
            {"heading": "Weekly Executive Summary", "bullets": [
                "Period: 2026-07-15 to 2026-07-22",
                "Revenue represented in top accounts: 142421.60",
                "Open executive risks: 3",
            ]},
            {"heading": "Top Account Signal", "bullets": [
                "Top account: Sample Dental Group",
                "Spend: 42150.75",
                "Watch item: Shipment delays",
            ]},
            {"heading": "Priority Actions", "bullets": [
                "High: Shipments - 3 open exceptions (Operations)",
                "Medium: Finance - 2 unpaid vendor invoices above threshold (Finance)",
            ]},
        ]
        tables_3 = [
            {"name": "Top Customers", "rows": [
                {"rank": 1, "customerId": "CUST-100", "name": "Sample Dental Group", "totalSpend": 42150.75},
                {"rank": 2, "customerId": "CUST-245", "name": "North Clinic Network", "totalSpend": 38200.00},
            ]},
        ]
        demo3 = client.post("/workflow-graphs", json={
            "displayName": "Weekly Executive Brief -> Email",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_pdf_packet",
                 "title": "Build executive brief PDF",
                 "config": {"title": "Weekly Executive Brief", "sections": sections_3, "tables": tables_3, "classification": ["INTERNAL"]}},
                {"nodeId": "n2", "kind": "tool_call", "tool": "create_email_draft",
                 "title": "Draft brief email to leadership",
                 "config": {
                     "to": ["leadership@example.com"], "subject": "Weekly Executive Brief - 2026-07-22",
                     "body_markdown": "Hi team,\n\nThis week's executive brief is attached/summarized below.\n\n- Top account: Sample Dental Group (42150.75)\n- Open executive risks: 3\n\nRegards,\nGoverned AI Office Assistant",
                     "classification": ["INTERNAL"],
                 }},
                {"nodeId": "g1", "kind": "approval_gate",
                 "config": {"reason": "Sign off before sending the exec brief externally-addressed email", "risk_level": "medium"}},
                {"nodeId": "n3", "kind": "tool_call", "tool": "send_email_draft",
                 "title": "Send brief email",
                 "inputBindings": {
                     "draft_id": {"source": "node", "node_id": "n2", "path": "draftId"},
                     "approval_id": {"source": "node", "node_id": "g1", "path": "approvalId"},
                 }},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"},
                {"edgeId": "e3", "sourceNodeId": "n2", "targetNodeId": "g1"},
                {"edgeId": "e4", "sourceNodeId": "g1", "targetNodeId": "n3"},
            ],
        })
        check("demo3 graph created", demo3.status_code == 201, demo3.text)
        gid3 = demo3.json()["graphId"]
        pub3 = client.post(f"/workflow-graphs/{gid3}/publish", json={})
        check("demo3 published", pub3.status_code == 200, pub3.text)
        run3 = client.post(f"/workflows/{gid3}/run", json={})
        run3_body = run3.json()
        check("demo3 run pauses at gate (after PDF + draft both ran)", run3.status_code == 201 and run3_body.get("status") == "approval_required", run3_body)
        n1_step = next((s for s in run3_body.get("steps", []) if s.get("stepId") == "n1"), {})
        n2_step = next((s for s in run3_body.get("steps", []) if s.get("stepId") == "n2"), {})
        check("demo3 PDF step completed before the gate", n1_step.get("status") == "completed", n1_step)
        check("demo3 draft step completed before the gate", n2_step.get("status") == "completed", n2_step)
        pdf_artifact3 = next((a for a in (run3_body.get("artifacts") or []) if a.get("artifactId")), {})
        aid3_pdf = pdf_artifact3.get("artifactId")

        appr3 = run3_body.get("approval", {}).get("approvalId")
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "demo_admin", "password": "demo_password"})
        approve3 = client.post(f"/approvals/{appr3}/approve", json={"note": "brief looks good, ok to send"})
        check("demo3 admin approves the gate", approve3.status_code == 200, approve3.text)
        resumed3 = client.post(f"/workflow-runs/{run3_body['runId']}/resume", json={"approval_id": appr3})
        resumed3_body = resumed3.json()
        check("demo3 resume completes the run", resumed3.status_code == 200 and resumed3_body.get("status") == "completed", resumed3_body)
        n3_step = next((s for s in resumed3_body.get("steps", []) if s.get("stepId") == "n3"), {})
        check("demo3 send step actually ran with a sendId", n3_step.get("status") == "completed" and n3_step.get("outputs", {}).get("sendId"), n3_step)
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "demo_builder", "password": "builder_password"})

        check("demo3 PDF artifact id present", bool(aid3_pdf), run3_body)
        dl3 = download(client, aid3_pdf)
        check("demo3 PDF artifact downloads", dl3.status_code == 200, dl3.status_code)
        (OUT / "demo3-weekly-executive-brief.pdf").write_bytes(dl3.content)
        text3 = pdf_text(dl3.content)
        for needle in ("Weekly Executive Brief", "Top Account Signal", "Sample Dental Group", "42150.75", "Priority Actions"):
            check(f"demo3 PDF contains {needle!r}", needle in text3, text3[:400])

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
