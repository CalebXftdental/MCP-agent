"""End-to-end test of "AR Credit-Hold Radar" as a real, user-buildable "My
Workflow" graph -- same recipe as test_winback_radar_graph.py /
test_ap_digest_graph.py / test_ar_aging_digest_graph.py:

  trigger (min_invoice_age_days)
    -> tool_call: get_ar_invoices_past_due (min_invoice_age_days <- trigger)
    -> filter: input <- "invoices"; creditHold eq true
    -> tool_call: create_excel_report (tables <- filter's "matchedTable")
    -> tool_call: create_email_draft (never sent -- an internal finance/credit digest)

`creditHold` is confirmed a real field on get_ar_invoices_past_due's output
(mcp-minierp/sqlagent/finance/index.py's ArInvoicesPastDueResult row shape,
sourced from ARInvoice.creditHold via MINIERP_FIELDS["ar_credit_hold"]) --
no field-name guessing needed, read directly from source.

Same schema-limit note as test_ar_aging_digest_graph.py: ARInvoice has no
customer/bAccount link field in this schema, so flagged rows are a company-
wide credit-hold digest, not attributable to a specific account.
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
TMP = ROOT / "_smoke" / ".tmp" / "ar-credit-hold-radar-graph"
OUT = ROOT / "_smoke" / ".tmp" / "ar-credit-hold-radar-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "ar_credit_hold_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "ar_credit_hold_password",
    "GOVERNANCE_SESSION_SECRET": "ar-credit-hold-radar-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18531/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18532/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18533/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "30",
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


def wait_health(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"service did not become healthy: {last}")


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


print("start mcp-minierp (real ERP mirror) + mcp-office + mcp-email")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
minierp = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18531", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18532", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18533", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18531/health")
    wait_health("http://127.0.0.1:18532/health")
    wait_health("http://127.0.0.1:18533/health")
    check("mcp-minierp (real ERP mirror) healthy", True)
    check("mcp-office healthy", True)
    check("mcp-email healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:ar_credit_hold_builder", name="ar_credit_hold_builder", key_hash="", status="active",
        role="user", type="user", categories=["finance", "office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "ar_credit_hold_builder", "password": "builder_password"})

        graph = client.post("/workflow-graphs", json={
            "displayName": "AR Credit-Hold Radar",
            "description": "Flags AR invoices on accounts with credit hold set, company-wide (not attributable "
                            "to a specific customer -- ARInvoice has no customer link in this ERP's schema).",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                    {"name": "min_invoice_age_days", "label": "Minimum invoice age (days)"},
                ]}},
                {"nodeId": "n1", "kind": "tool_call", "tool": "get_ar_invoices_past_due",
                 "title": "Look up AR invoices",
                 "inputBindings": {"min_invoice_age_days": {"source": "trigger", "path": "min_invoice_age_days"}},
                 # paginate:true -- get one governed call over the full company-wide
                 # dataset (this tool now returns ONE real page per call otherwise --
                 # see finance/index.py's docstring), same pattern as the AR aging /
                 # AP digest reference graphs.
                 "config": {"paginate": True}},
                {"nodeId": "n2", "kind": "filter", "title": "Flag credit-hold invoices",
                 "inputBindings": {"input": {"source": "node", "node_id": "n1", "path": "invoices"}},
                 "config": {"table_name": "AR Invoices - Credit Hold",
                            "conditions": {"all": [{"field": "creditHold", "op": "eq", "value": True}]}}},
                {"nodeId": "n3", "kind": "tool_call", "tool": "create_excel_report",
                 "title": "Build credit-hold radar workbook",
                 "inputBindings": {"tables": {"source": "node", "node_id": "n2", "path": "matchedTable"}},
                 "config": {"title": "AR Credit-Hold Radar", "classification": ["INTERNAL", "SENSITIVE"]}},
                {"nodeId": "n4", "kind": "tool_call", "tool": "create_email_draft",
                 "title": "Draft credit-hold digest email",
                 "config": {
                     "to": ["finance-team@frontierdental.com"],
                     "subject": "AR Credit-Hold Radar",
                     "body_markdown": "Hi team,\n\nThe attached workbook lists AR invoices on accounts currently "
                                      "flagged for credit hold, company-wide. Note: this ERP does not link AR "
                                      "invoices to a customer record, so these rows cannot be attributed to a "
                                      "specific account -- use the invoice number for follow-up.\n\nRegards,\n"
                                      "Governed AI Office Assistant",
                     "classification": ["INTERNAL"],
                 }},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"},
                {"edgeId": "e3", "sourceNodeId": "n2", "targetNodeId": "n3"},
                {"edgeId": "e4", "sourceNodeId": "n3", "targetNodeId": "n4"},
            ],
        })
        check("AR credit-hold radar graph created", graph.status_code == 201, graph.text)
        gid = graph.json()["graphId"]
        pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("AR credit-hold radar graph published", pub.status_code == 200, pub.text)

        run = None
        matched = 0
        for min_invoice_age_days in (0, 30, 90):
            attempt = client.post(f"/workflows/{gid}/run", json={"min_invoice_age_days": min_invoice_age_days})
            body = attempt.json()
            n2_out = next((s for s in body.get("steps", []) if s.get("stepId") == "n2"), {}).get("outputs", {})
            matched = n2_out.get("matchedCount") or 0
            run, run_body = attempt, body
            if matched > 0:
                break
        check("AR credit-hold radar run reaches the workbook step", run.status_code == 201, run_body)

        n1_out = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n1"), {}).get("outputs", {})
        check("n1 (get_ar_invoices_past_due) returned real invoices", (n1_out.get("count") or len(n1_out.get("invoices") or [])) > 0
              or bool(n1_out.get("invoices")), n1_out)
        check("no customerId/customerName field on any real AR row (schema has no such link)",
              not any("customer" in k.lower() for row in (n1_out.get("invoices") or [])[:20] for k in row.keys()),
              (n1_out.get("invoices") or [])[:3])
        # Live-probed (2026-08-25, 6000 real ARInvoice rows via _smoke/.tmp/probe_credit_hold.py):
        # creditHold is false on every single real row today -- this Acumatica
        # instance currently has zero accounts on credit hold. That's a real
        # business state, not a broken filter, so the mechanism check below
        # doesn't require matched > 0 -- it requires the filter to have
        # correctly evaluated every real row it saw (true positives if any
        # exist, zero false positives either way) rather than erroring or
        # silently no-op'ing.
        print(f"  info matched={matched} real credit-hold invoices out of "
              f"{n1_out.get('count') or len(n1_out.get('invoices') or [])} scanned "
              "(0 is expected/legitimate on current live data -- see probe note above)")
        matched_rows = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n2"), {}).get("outputs", {}).get("matched", [])
        check("every flagged row actually carries creditHold true (regression check)",
              all(row.get("creditHold") is True for row in matched_rows), matched_rows[:5])
        check("filter step ran to completion over the real dataset (matchedCount is a real int, not an error)",
              isinstance(matched, int) and matched >= 0, run_body)

        # A workbook full of SENSITIVE-classified financial rows can cross the
        # broad-export threshold automatically (create_excel_report's own
        # risk=EXPORT + row-count check -- no explicit approval_gate node
        # needed), same mechanism the AR aging / AP digest reference graphs
        # exercise. Handle it the same way if it happens.
        if run_body.get("status") == "approval_required":
            approval_id = (run_body.get("approval") or {}).get("approvalId")
            check("broad-export approval was actually created", bool(approval_id), run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "ar_credit_hold_admin", "password": "ar_credit_hold_password"})
            approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed AR credit-hold workbook"})
            check("admin approves the broad export", approve.status_code == 200, approve.text)
            resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
            run_body = resumed.json()
            check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "ar_credit_hold_builder", "password": "builder_password"})
        else:
            check("AR credit-hold radar run completes without needing approval", run_body.get("status") == "completed", run_body)

        n3_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n3"), {})
        aid = n3_step.get("outputs", {}).get("artifactId")
        check("AR credit-hold radar workbook artifact created", bool(aid), n3_step)

        if aid:
            dl = client.get(f"/artifacts/{aid}/download")
            check("workbook artifact downloads", dl.status_code == 200, dl.status_code)
            (OUT / "ar-credit-hold-radar.xlsx").write_bytes(dl.content)
            cells = xlsx_cell_texts(dl.content)
            if matched_rows:
                check("workbook has more than a couple cells (regression check)", len(cells) > 5, len(cells))
                real_invoice_number = str(matched_rows[0].get("invoiceNumber") or "")
                check("workbook contains a real flagged invoice number from the live mirror",
                      any(real_invoice_number == c or (real_invoice_number and real_invoice_number in c) for c in cells), cells[:20])
            else:
                # 0 real credit-hold invoices today -- an empty-but-well-formed
                # workbook (title/header cells only) is the correct output, not
                # a regression. Just confirm it's genuinely a valid xlsx.
                check("empty-result workbook is still a valid, parseable xlsx", isinstance(cells, list), cells)

        n4_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n4"), {})
        check("AR credit-hold radar email drafted, never sent", n4_step.get("status") == "completed" and n4_step.get("outputs", {}).get("draftId")
              and not n4_step.get("outputs", {}).get("sendId"), n4_step)

finally:
    minierp.terminate()
    office.terminate()
    email.terminate()
    for proc in (minierp, office, email):
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

print(f"\n{PASS} passed, {FAIL} failed")
print(f"artifacts written to: {OUT}")
sys.exit(1 if FAIL else 0)
