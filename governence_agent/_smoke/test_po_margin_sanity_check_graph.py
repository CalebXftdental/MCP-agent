"""End-to-end test of "PO Margin Sanity Check" as a real, user-buildable "My
Workflow" graph -- workflow #9 of this batch's final 3, same recipe as
test_gl_period_variance_watch_graph.py (loop over a real per-item tool) but
adding a `join` node afterward to merge the loop's per-item results back onto
the original PO line items:

  trigger (po_number)
    -> tool_call: get_po_line_items (po_number <- trigger, fetch_all: true)
    -> loop over n1's "records" (one PO line item per iteration)
         body: tool_call get_sales_price(inventory_id <- loop_item.inventoryId,
                                          fetch_all: true)
    -> join: left <- n1's "records" (PO lines), right <- the loop's own
       "results" (one get_sales_price call's raw output per PO line), keyed on
       "inventoryId" both sides -- brings each line's real ARSalesPrice tier
       records + lookup status back onto its PO line row.
    -> tool_call: create_excel_report (tables <- join's "mergedTable")

Deviation from the original "compute a margin %" ask (the task brief's own
escape hatch, and the SAME nested-output limit test_gl_period_variance_watch_
graph.py already hit and documented): get_sales_price's real output nests
every price tier under a `records` list (`SalesPriceResult.records`, see
mcp-minierp/sqlagent/finance/schemas.py) -- there is no flat, top-level
`salesPrice` field on the tool's own result to join by, and neither `filter`
nor `join` (gateway/workflow_graph_interpreter.py's `_evaluate_condition_tree`
/ `_execute_join_node`) can reach INSIDE a nested list to read one, only a
literal top-level field (`row.get(field)`). So this graph cannot compute a
margin % (that needs subtracting `unitCost` from a specific nested tier's
`salesPrice`); it joins the PO line's real `unitCost` next to that item's real,
unflattened `salesPriceRecords` (a human/report reader can eyeball the two
side by side) plus a `priceLookupStatus` flag (did a real ARSalesPrice record
exist for this item at all). This is real, live-probed data, not the
"just report unit costs, no price at all" simplification the brief offered as
a fallback -- the loop+join mechanism IS exercised end-to-end, just without a
computed percentage.

Real, live-probed kill switch (2026-08-26): gateway/govern.py gates
get_sales_price OFF by default company-wide ("_GATED_TOOLS_ENV_FLAGS =
{'get_sales_price': 'ALLOW_PRICE_QUERY'}", regardless of what any
category/consumer grants) -- "the pricing surface was wired up ahead of a full
security review of that data." This test deliberately sets
ALLOW_PRICE_QUERY=true in its OWN subprocess environment (below) to prove the
graph mechanism end-to-end for when that review clears the switch -- it does
not touch, and has no effect on, any real deployment's actual (still gated)
default.

Real, live-probed PO (2026-08-26, direct find_with_offset_pagination scan of
POLine/ARSalesPrice via the same admin credential profile finance/index.py
uses -- see _smoke/.tmp/probe_po_lines.py, since cleaned up): "PO0336835" (an
on-hold PO from 2021-11-03, company 2/US) has 7 real open line items, every
one of which carries curyUnitCost=0.000000 (never received/invoiced -- a real,
honest data point, not a bug) and a real inventoryId that DOES have live
ARSalesPrice tier records on file (18.35, 11.24, 59.24, ... -- confirmed by
direct probe). That gap -- a real PO line costed at $0 next to inventory that
demonstrably has a real non-zero market price on file -- is exactly the kind
of "margin sanity" anomaly this workflow exists to surface, even without a
computed percentage.
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
TMP = ROOT / "_smoke" / ".tmp" / "po-margin-sanity-check-graph"
OUT = ROOT / "_smoke" / ".tmp" / "po-margin-sanity-check-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "po_margin_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "po_margin_password",
    "GOVERNANCE_SESSION_SECRET": "po-margin-sanity-check-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    # get_sales_price is gated OFF company-wide by default (gateway/govern.py's
    # _GATED_TOOLS_ENV_FLAGS) pending a full security review -- opted in here,
    # for this test's own subprocess environment only, to prove the graph
    # mechanism end-to-end (see the module docstring).
    "ALLOW_PRICE_QUERY": "true",
    "MINIERP_MCP_URL": "http://127.0.0.1:18651/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18652/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18653/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18651", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18652", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18653", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18651/health")
    wait_health("http://127.0.0.1:18652/health")
    wait_health("http://127.0.0.1:18653/health")
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
        consumer_id="user:po_margin_builder", name="po_margin_builder", key_hash="", status="active",
        role="user", type="user", categories=["finance", "office"],
        login_password_hash=hash_password("builder_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "po_margin_builder", "password": "builder_password"})

        graph = client.post("/workflow-graphs", json={
            "displayName": "PO Margin Sanity Check",
            "description": "For one PO's line items, looks up each item's real ARSalesPrice tier records "
                            "and reports them next to the PO's own unit cost -- no computed margin %% (the "
                            "price tiers are nested, not a flat field to join a percentage on to -- see the "
                            "graph notes), but a real cost-vs-price side-by-side for a human sanity check.",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                    {"name": "po_number", "label": "PO number"},
                ]}},
                {"nodeId": "n1", "kind": "tool_call", "tool": "get_po_line_items",
                 "title": "Look up PO line items",
                 "inputBindings": {"po_number": {"source": "trigger", "path": "po_number"}},
                 "config": {"fetch_all": True}},
                {"nodeId": "n_loop", "kind": "loop", "title": "For each PO line's inventory item",
                 "inputBindings": {"input": {"source": "node", "node_id": "n1", "path": "records"}},
                 "config": {"body": ["n2"]}},
                {"nodeId": "n2", "kind": "tool_call", "tool": "get_sales_price",
                 "title": "Look up real sales price tiers",
                 "inputBindings": {"inventory_id": {"source": "loop_item", "path": "inventoryId"}},
                 "config": {"fetch_all": True}},
                {"nodeId": "n3", "kind": "join", "title": "Merge PO line cost with real sales price tiers",
                 "inputBindings": {
                     "left": {"source": "node", "node_id": "n1", "path": "records"},
                     "right": {"source": "node", "node_id": "n_loop", "path": "results"},
                 },
                 "config": {
                     "left_key": "inventoryId", "right_key": "inventoryId",
                     "fields": [
                         {"from": "records", "as": "salesPriceRecords"},
                         {"from": "status", "as": "priceLookupStatus"},
                     ],
                     "table_name": "PO Margin Sanity Check",
                 }},
                {"nodeId": "n4", "kind": "tool_call", "tool": "create_excel_report",
                 "title": "Build PO margin sanity check workbook",
                 "inputBindings": {"tables": {"source": "node", "node_id": "n3", "path": "mergedTable"}},
                 "config": {"title": "PO Margin Sanity Check", "classification": ["INTERNAL", "SENSITIVE"]}},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n_loop"},
                {"edgeId": "e3", "sourceNodeId": "n_loop", "targetNodeId": "n2"},
                {"edgeId": "e4", "sourceNodeId": "n1", "targetNodeId": "n3"},
                {"edgeId": "e5", "sourceNodeId": "n_loop", "targetNodeId": "n3"},
                {"edgeId": "e6", "sourceNodeId": "n3", "targetNodeId": "n4"},
            ],
        })
        check("PO margin sanity check graph created", graph.status_code == 201, graph.text)
        gid = graph.json()["graphId"]
        pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("PO margin sanity check graph published", pub.status_code == 200, pub.text)

        # PO0336835: live-probed 2026-08-26, 7 real open line items, every
        # inventoryId confirmed to have real ARSalesPrice records on file.
        po_number = "PO0336835"
        run = client.post(f"/workflows/{gid}/run", json={"po_number": po_number})
        run_body = run.json()
        check("PO margin sanity check run reaches the workbook step", run.status_code == 201, run_body)
        steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

        n1_out = steps_by_id.get("n1", {}).get("outputs", {})
        n1_records = n1_out.get("records") or []
        check("n1 (get_po_line_items) returned real PO line items", len(n1_records) == 7, n1_out)
        check("every real line carries a real inventoryId", all(r.get("inventoryId") for r in n1_records), n1_records)

        loop_out = steps_by_id.get("n_loop", {}).get("outputs", {})
        check("loop ran once per real PO line item", loop_out.get("itemCount") == len(n1_records)
              and loop_out.get("iterations") == len(n1_records), loop_out)
        check("loop completed without truncation", loop_out.get("truncated") is False, loop_out)
        loop_results = loop_out.get("results") or []
        check("every loop iteration's get_sales_price call succeeded (real price data on file for every line)",
              all(r.get("status") == "success" for r in loop_results), loop_results)

        n3_out = steps_by_id.get("n3", {}).get("outputs", {})
        matched_rows = n3_out.get("merged") or []
        check("join matched every PO line to its real sales-price lookup (same inventoryId both sides)",
              n3_out.get("matchedCount") == 7 and n3_out.get("unmatchedCount") == 0, n3_out)
        check("every merged row carries the real, un-computed unitCost from the PO line",
              all("unitCost" in r for r in matched_rows), matched_rows[:3])
        check("every merged row carries its real, unflattened sales-price tier records",
              all(r.get("salesPriceRecords") for r in matched_rows), matched_rows[:3])
        check("every merged row's price lookup succeeded (regression check)",
              all(r.get("priceLookupStatus") == "success" for r in matched_rows), matched_rows)

        # The real, live-probed anomaly this PO actually demonstrates: every
        # line is costed at $0 (never received/invoiced -- PO0336835 has been
        # on hold since 2021) while its inventory item demonstrably has a real,
        # non-zero market price on file. Report it, don't fabricate a %.
        zero_cost_with_real_price = [
            r for r in matched_rows
            if (r.get("unitCost") or 0) == 0 and any((t.get("salesPrice") or 0) > 0 for t in (r.get("salesPriceRecords") or []))
        ]
        print(f"  info {len(zero_cost_with_real_price)}/{len(matched_rows)} lines: $0 unit cost next to a real "
              "non-zero sales price on file (a genuine margin-sanity anomaly on this live PO)")
        check("at least one real line shows this $0-cost-vs-real-price anomaly", len(zero_cost_with_real_price) > 0, matched_rows)

        if run_body.get("status") == "approval_required":
            approval_id = (run_body.get("approval") or {}).get("approvalId")
            check("broad-export approval was actually created", bool(approval_id), run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "po_margin_admin", "password": "po_margin_password"})
            approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed PO margin sanity check workbook"})
            check("admin approves the broad export", approve.status_code == 200, approve.text)
            resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
            run_body = resumed.json()
            check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "po_margin_builder", "password": "builder_password"})
        else:
            check("PO margin sanity check run completes without needing approval", run_body.get("status") == "completed", run_body)

        n4_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n4"), {})
        aid = n4_step.get("outputs", {}).get("artifactId")
        check("PO margin sanity check workbook artifact created", bool(aid), n4_step)

        if aid:
            dl = client.get(f"/artifacts/{aid}/download")
            check("workbook artifact downloads", dl.status_code == 200, dl.status_code)
            (OUT / "po-margin-sanity-check.xlsx").write_bytes(dl.content)
            cells = xlsx_cell_texts(dl.content)
            check("workbook has more than a couple cells (regression check)", len(cells) > 5, len(cells))
            real_inventory_id = str(matched_rows[0].get("inventoryId") or "")
            check("workbook contains a real PO line's inventoryId from the live mirror",
                  any(real_inventory_id == c or (real_inventory_id and real_inventory_id in c) for c in cells), cells[:20])

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
