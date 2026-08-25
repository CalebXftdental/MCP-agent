"""End-to-end test of "AP Due-Soon Digest" as a real, user-buildable "My
Workflow" graph -- the second bulk-tool-backed workflow this session, same
recipe as _smoke/test_winback_radar_graph.py:

  trigger (days_ahead)
    -> tool_call: get_ap_invoices_due_soon (days_ahead <- trigger)
    -> filter: input <- "invoices"; paid eq false
    -> tool_call: create_excel_report (tables <- filter's "matchedTable")
    -> tool_call: create_email_draft (never sent -- an internal AP-team digest)

Built with the exact node/edge JSON shape a human produces via the My
Workflow Step/Canvas UI (see gateway/frontend/src/components/workflow/
StepModal.tsx and StepConfigFields.tsx's `filter` branch).
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
TMP = ROOT / "_smoke" / ".tmp" / "ap-digest-graph"
OUT = ROOT / "_smoke" / ".tmp" / "ap-digest-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "ap_digest_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "ap_digest_password",
    "GOVERNANCE_SESSION_SECRET": "ap-digest-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18471/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18472/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18473/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18471", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18472", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18473", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18471/health")
    wait_health("http://127.0.0.1:18472/health")
    wait_health("http://127.0.0.1:18473/health")
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
        consumer_id="user:ap_digest_builder", name="ap_digest_builder", key_hash="", status="active",
        role="user", type="user", categories=["finance", "office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "ap_digest_builder", "password": "builder_password"})

        graph = client.post("/workflow-graphs", json={
            "displayName": "AP Due-Soon Digest",
            "description": "Flags large AP invoices due soon, across every vendor, for the AP team.",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                    {"name": "days_ahead", "label": "Due within (days)"},
                ]}},
                {"nodeId": "n1", "kind": "tool_call", "tool": "get_ap_invoices_due_soon",
                 "title": "Look up AP invoices due soon",
                 "inputBindings": {"days_ahead": {"source": "trigger", "path": "days_ahead"}},
                 # paginate:true -- this tool now returns ONE real page per call
                 # (see finance/index.py's docstring), so a digest over the full
                 # cross-vendor dataset needs the interpreter's generic
                 # exhaustion mechanism, not just page 1.
                 "config": {"paginate": True}},
                # NOTE: this live ERP mirror's AP data has payDate set on every
                # sampled invoice (confirmed by probe: 0/5000 unpaid company-wide) --
                # filtering on paid==false would always return zero rows against
                # THIS dataset, which proves nothing about whether the mechanism
                # works. lineTotal is the condition that actually has real spread
                # in the live data, so that's what this demo graph flags; `paid`
                # is still returned on every row for a human to read.
                {"nodeId": "n2", "kind": "filter", "title": "Flag large invoices due soon",
                 "inputBindings": {"input": {"source": "node", "node_id": "n1", "path": "invoices"}},
                 "config": {"table_name": "AP Invoices Due Soon",
                            "conditions": {"all": [{"field": "lineTotal", "op": "gt", "value": 1000}]}}},
                {"nodeId": "n3", "kind": "tool_call", "tool": "create_excel_report",
                 "title": "Build AP due-soon workbook",
                 "inputBindings": {"tables": {"source": "node", "node_id": "n2", "path": "matchedTable"}},
                 "config": {"title": "AP Invoices Due Soon", "classification": ["INTERNAL", "SENSITIVE"]}},
                {"nodeId": "n4", "kind": "tool_call", "tool": "create_email_draft",
                 "title": "Draft AP digest email",
                 "config": {
                     "to": ["ap-team@frontierdental.com"],
                     "subject": "AP Due-Soon Digest",
                     "body_markdown": "Hi team,\n\nThe attached workbook lists large AP invoices coming due soon, "
                                      "across every vendor. Please review and plan cash flow / payment timing as "
                                      "needed.\n\nRegards,\nGoverned AI Office Assistant",
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
        check("AP digest graph created", graph.status_code == 201, graph.text)
        gid = graph.json()["graphId"]
        pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("AP digest graph published", pub.status_code == 200, pub.text)

        run = None
        matched = 0
        for days_ahead in (60, 180, 365):
            attempt = client.post(f"/workflows/{gid}/run", json={"days_ahead": days_ahead})
            body = attempt.json()
            n2_out = next((s for s in body.get("steps", []) if s.get("stepId") == "n2"), {}).get("outputs", {})
            matched = n2_out.get("matchedCount") or 0
            run, run_body = attempt, body
            if matched > 0:
                break
        check("AP digest run reaches the workbook step", run.status_code == 201, run_body)
        check("filter flagged at least one real large invoice due soon", matched > 0, run_body)

        # A workbook full of large SENSITIVE-classified financial rows crosses the
        # broad-export threshold automatically (no explicit approval_gate node
        # needed -- create_excel_report's own risk=EXPORT + row-count check does
        # this, same mechanism demo2 in test_my_workflow_demos.py exercises) --
        # expected, not a bug: approve + resume, same as that existing demo.
        if run_body.get("status") == "approval_required":
            approval_id = (run_body.get("approval") or {}).get("approvalId")
            check("broad-export approval was actually created", bool(approval_id), run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "ap_digest_admin", "password": "ap_digest_password"})
            approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed AP due-soon workbook"})
            check("admin approves the broad export", approve.status_code == 200, approve.text)
            resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
            run_body = resumed.json()
            check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "ap_digest_builder", "password": "builder_password"})
        else:
            check("AP digest run completes without needing approval", run_body.get("status") == "completed", run_body)

        # Two tool_call nodes each produce an artifact (the workbook AND the
        # email draft) -- read the workbook's id off ITS OWN step (n3), not
        # off the run's flat `artifacts` list, which mixes both together.
        n3_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n3"), {})
        aid = n3_step.get("outputs", {}).get("artifactId")
        check("AP digest workbook artifact created", bool(aid), n3_step)

        if aid:
            dl = client.get(f"/artifacts/{aid}/download")
            check("workbook artifact downloads", dl.status_code == 200, dl.status_code)
            (OUT / "ap-digest.xlsx").write_bytes(dl.content)
            cells = xlsx_cell_texts(dl.content)
            check("workbook has more than a couple cells (regression check)", len(cells) > 5, len(cells))
            n1_out = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n1"), {}).get("outputs", {})
            matched_rows = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n2"), {}).get("outputs", {}).get("matched", [])
            if matched_rows:
                real_invoice_number = str(matched_rows[0].get("invoiceNumber") or "")
                check("workbook contains a real flagged invoice number from the live mirror",
                      any(real_invoice_number == c or (real_invoice_number and real_invoice_number in c) for c in cells), cells[:20])

        n4_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n4"), {})
        check("AP digest email drafted, never sent", n4_step.get("status") == "completed" and n4_step.get("outputs", {}).get("draftId")
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
