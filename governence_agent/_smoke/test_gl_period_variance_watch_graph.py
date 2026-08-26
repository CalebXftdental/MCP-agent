"""End-to-end test of "GL Period Variance Watch" as a real, user-buildable "My
Workflow" graph -- same recipe as test_ar_credit_hold_radar_graph.py /
test_ar_aging_digest_graph.py, this time exercising the `loop` node against a
real, single-record tool (get_gl_period_summary) instead of a bulk tool:

  trigger (account_codes: list[str], threshold_pct)
    -> loop over account_codes
         body: tool_call get_gl_period_summary(account_cd <- loop_item, page_size=1)
    -> filter: input <- loop's "results"; status eq "success"
       (shapes the loop's raw per-account results into a table AND drops any
       watched account code that turned out to have no GL history on file)
    -> tool_call: create_excel_report (tables <- filter's "matchedTable")
    -> tool_call: create_email_draft (never sent -- an internal finance digest)

Deviation from the original two-period variance-calc ask (noted up front, per
the task brief's own escape hatch): get_gl_period_summary(account_cd,
page_size=1) returns only the MOST RECENT fiscal period per account (results
are orderBy finPeriodId DESC -- see mcp-minierp/sqlagent/finance/index.py).
Computing a period-over-period variance % against threshold_pct would need
arithmetic between two fields of two different records (this period vs a
prior period) -- the `filter` node this graph engine has can only compare ONE
field against a constant/threshold, not two fields of two different rows
against each other. So this graph reports each watched account's CURRENT
period GL summary (beginning balance, period debit/credit, YTD balance)
rather than a computed variance; threshold_pct is still accepted as a trigger
input (kept for forward compatibility / a future variance-capable node) but
is not mechanically enforced here.

Second, unavoidable nesting note: get_gl_period_summary's own output nests its
period row(s) under "records" (each loop iteration's raw result is
{status, accountCd, company, records: [...], pagination, truncated}). The
`filter` node used to shape the post-loop table for create_excel_report
therefore matches/report at the PER-ACCOUNT level (whole result objects, one
row per watched account) -- the nested `records` field appears as a
stringified list inside its own Excel cell (mcp-office/builders/excel.py's
_cell() calls str() on any non-numeric value), not flattened into its own
columns. Still real, inspectable live data, just one level nested.

Two real, pre-existing bugs were found and fixed in mcp-minierp/sqlagent/
finance/index.py + schemas.py while live-probing account codes for this graph
(2026-08-25), both of which made get_gl_period_summary/get_po_line_items/
get_sales_price 100% non-functional against real (non-empty) live data before
the fix:
  1. GlPeriodSummaryResult (schemas.py) was missing a `truncated` field even
     though get_gl_period_summary always passes truncated=... to it -- any
     account WITH real GL history crashed the tool with a pydantic
     "extra_forbidden" ValidationError. Fixed by adding `truncated: bool |
     None = None`, matching PoLineItemsResult/SalesPriceResult.
  2. POLine.inventoryId, ARSalesPrice.inventoryId/customerId, and
     GLHistory.ledgerId/finPeriodId/subId come back from the live GraphQL
     endpoint as JSON numbers, not strings, even though their Result records
     declare these fields `str | None` -- ANY real PO/sales-price/GL-history
     row crashed with a pydantic "string_type" ValidationError. Fixed with a
     new `_str_or_none()` helper applied at the 3 record-construction sites
     these graphs actually call.
Confirmed by direct probe: before the fix, get_po_line_items/get_sales_price
crashed on literally every real PO/inventory id tried (10,000+ POLine rows
scanned), and get_gl_period_summary crashed on every account WITH real
history (11350/21131/28111 all crashed; only genuinely-empty "not_found"
accounts survived, because that path never touches truncated/records at
all). These are narrowly-scoped type-coercion/missing-field fixes, not
business-logic changes.

Real, live-probed account codes as of 2026-08-25 (via a direct fetch_page_or_
all scan of the Account/GLHistory tables, admin credential profile -- see
this session's probe notes): "11350" (Goods in Transit), "21131" (a payable
account), "28111" (a shareholder-loan account) all have real GL period
history. "11140"/"11210"/"11300"/"11360"/"21120" (active accounts with no
GL history in this data) are included in the watch list too, to prove the
filter step's status=="success" condition genuinely excludes the accounts
that come back not_found rather than crashing or silently including them.
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
TMP = ROOT / "_smoke" / ".tmp" / "gl-period-variance-watch-graph"
OUT = ROOT / "_smoke" / ".tmp" / "gl-period-variance-watch-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "gl_variance_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "gl_variance_password",
    "GOVERNANCE_SESSION_SECRET": "gl-period-variance-watch-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18610/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18611/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18612/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18610", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18611", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18612", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18610/health")
    wait_health("http://127.0.0.1:18611/health")
    wait_health("http://127.0.0.1:18612/health")
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
        consumer_id="user:gl_variance_builder", name="gl_variance_builder", key_hash="", status="active",
        role="user", type="user", categories=["finance", "office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "gl_variance_builder", "password": "builder_password"})

        graph = client.post("/workflow-graphs", json={
            "displayName": "GL Period Variance Watch",
            "description": "Loops over a watched list of GL account codes and reports each account's current "
                            "fiscal period summary (beginning balance, period debit/credit, YTD balance). No "
                            "computed period-over-period variance -- see the graph notes.",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                    {"name": "account_codes", "label": "Account codes to watch"},
                    {"name": "threshold_pct", "label": "Variance threshold (%)", "optional": True},
                ]}},
                {"nodeId": "n_loop", "kind": "loop", "title": "For each watched account",
                 "inputBindings": {"input": {"source": "trigger", "path": "account_codes"}},
                 "config": {"body": ["n1"]}},
                {"nodeId": "n1", "kind": "tool_call", "tool": "get_gl_period_summary",
                 "title": "Look up current-period GL summary",
                 "inputBindings": {"account_cd": {"source": "loop_item"}},
                 "config": {"page_size": 1}},
                {"nodeId": "n2", "kind": "filter", "title": "Keep accounts with real GL history",
                 "inputBindings": {"input": {"source": "node", "node_id": "n_loop", "path": "results"}},
                 "config": {"table_name": "GL Period Variance Watch",
                            "conditions": {"all": [{"field": "status", "op": "eq", "value": "success"}]}}},
                {"nodeId": "n3", "kind": "tool_call", "tool": "create_excel_report",
                 "title": "Build GL period variance watch workbook",
                 "inputBindings": {"tables": {"source": "node", "node_id": "n2", "path": "matchedTable"}},
                 "config": {"title": "GL Period Variance Watch", "classification": ["INTERNAL", "SENSITIVE"]}},
                {"nodeId": "n4", "kind": "tool_call", "tool": "create_email_draft",
                 "title": "Draft GL variance watch digest email",
                 "config": {
                     "to": ["finance-team@frontierdental.com"],
                     "subject": "GL Period Variance Watch",
                     "body_markdown": "Hi team,\n\nThe attached workbook lists the current fiscal period GL "
                                      "summary for each watched account code. Accounts with no GL history on "
                                      "file are omitted.\n\nRegards,\nGoverned AI Office Assistant",
                     "classification": ["INTERNAL"],
                 }},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n_loop"},
                {"edgeId": "e2", "sourceNodeId": "n_loop", "targetNodeId": "n1"},
                {"edgeId": "e3", "sourceNodeId": "n_loop", "targetNodeId": "n2"},
                {"edgeId": "e4", "sourceNodeId": "n2", "targetNodeId": "n3"},
                {"edgeId": "e5", "sourceNodeId": "n3", "targetNodeId": "n4"},
            ],
        })
        check("GL period variance watch graph created", graph.status_code == 201, graph.text)
        gid = graph.json()["graphId"]
        pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("GL period variance watch graph published", pub.status_code == 200, pub.text)

        # 11350/21131/28111 confirmed to have real GL history by direct live probe
        # (2026-08-25); 11140/11210/11300 confirmed active accounts with NO GL
        # history, included to prove the filter genuinely excludes them.
        account_codes = ["11350", "21131", "28111", "11140", "11210", "11300"]
        run = client.post(f"/workflows/{gid}/run", json={"account_codes": account_codes, "threshold_pct": 10})
        run_body = run.json()
        check("GL period variance watch run reaches the workbook step", run.status_code == 201, run_body)
        steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

        loop_out = steps_by_id.get("n_loop", {}).get("outputs", {})
        check("loop ran once per watched account code", loop_out.get("itemCount") == len(account_codes)
              and loop_out.get("iterations") == len(account_codes), loop_out)
        check("loop completed without truncation", loop_out.get("truncated") is False, loop_out)
        loop_results = loop_out.get("results") or []
        succeeded_accounts = {r.get("accountCd") for r in loop_results if r.get("status") == "success"}
        check("at least one watched account came back with real GL history",
              any(a in succeeded_accounts for a in ("11350", "21131", "28111")), loop_results)
        not_found_accounts = {r.get("accountCd") for r in loop_results if r.get("status") == "not_found"}
        check("the accounts confirmed to have NO GL history really came back not_found (regression check)",
              {"11140", "11210", "11300"} <= not_found_accounts, loop_results)

        n2_out = steps_by_id.get("n2", {}).get("outputs", {})
        matched_rows = n2_out.get("matched") or []
        matched = n2_out.get("matchedCount") or 0
        check("filter kept exactly the accounts with real GL history, dropped the not_found ones",
              matched == len(succeeded_accounts) and all(r.get("status") == "success" for r in matched_rows),
              (matched, matched_rows))
        check("every kept row carries a real, non-empty GL period record",
              all(r.get("records") for r in matched_rows), matched_rows)

        if run_body.get("status") == "approval_required":
            approval_id = (run_body.get("approval") or {}).get("approvalId")
            check("broad-export approval was actually created", bool(approval_id), run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "gl_variance_admin", "password": "gl_variance_password"})
            approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed GL variance watch workbook"})
            check("admin approves the broad export", approve.status_code == 200, approve.text)
            resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
            run_body = resumed.json()
            check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "gl_variance_builder", "password": "builder_password"})
        else:
            check("GL period variance watch run completes without needing approval", run_body.get("status") == "completed", run_body)

        n3_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n3"), {})
        aid = n3_step.get("outputs", {}).get("artifactId")
        check("GL period variance watch workbook artifact created", bool(aid), n3_step)

        if aid:
            dl = client.get(f"/artifacts/{aid}/download")
            check("workbook artifact downloads", dl.status_code == 200, dl.status_code)
            (OUT / "gl-period-variance-watch.xlsx").write_bytes(dl.content)
            cells = xlsx_cell_texts(dl.content)
            check("workbook has more than a couple cells (regression check)", len(cells) > 5, len(cells))
            if matched_rows:
                real_acct = str(matched_rows[0].get("accountCd") or "")
                check("workbook contains a real watched account code from the live mirror",
                      any(real_acct == c or (real_acct and real_acct in c) for c in cells), cells[:20])

        n4_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n4"), {})
        check("GL variance watch email drafted, never sent", n4_step.get("status") == "completed" and n4_step.get("outputs", {}).get("draftId")
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
