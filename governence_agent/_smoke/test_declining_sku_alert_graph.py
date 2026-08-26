"""End-to-end test of "Declining-SKU Alert" as a real, user-buildable "My
Workflow" graph -- the loop-based one of the three new workflows this session
(built and confidence-checked AFTER test_unreachable_account_flag_graph.py's
join-free baseline and test_big_spender_drop_off_graph.py's join usage).

  trigger (inventory_ids: list[str], period_a_start/end, period_b_start/end)
    -> loop_a: for each inventory_id in inventory_ids ->
         tool_call: get_product_sales(inventory_id <- loop_item, start_date/end_date <- period A)
    -> loop_b: for each inventory_id in inventory_ids (independently, same input list) ->
         tool_call: get_product_sales(inventory_id <- loop_item, start_date/end_date <- period B)
    -> join: left <- loop_a's "results", right <- loop_b's "results",
             left_key=right_key="inventoryId" (the field both periods' get_product_sales
             calls echo back -- confirmed straight from
             mcp-minierp/sqlagent/analytics.py: `inventoryId=inv` where inv is exactly
             the inventory_id argument, so both loops' rows share it verbatim),
             fields renaming period B's totals so they don't collide with period A's
             own totalQuantity/totalRevenue/orderCount, on_missing="keep" (a SKU with
             zero period-B sales is exactly the "declined to nothing" case this
             workflow exists to surface, not a row to silently drop)
    -> tool_call: create_excel_report (tables <- join's "mergedTable")

DEVIATION from the original plan, done deliberately and noted up front (same
"prefer honest simplification over a fake pass" discipline
test_ar_credit_hold_radar_graph.py's fix demonstrated): the planned design
optionally chained a `percent_change` tool_call after the two get_product_sales
calls inside the loop body to compute a real percent-decline column.
`percent_change` (gateway/app.py) turns out to have NO entry in
governance_core/policy/manifest.py's TOOL_POLICIES -- confirmed by reading the
manifest directly -- and workflow_graph_store.validate_graph rejects ANY
tool_call node whose tool isn't in that table with blocker "unknown_tool" (see
validate_graph's `policy = manifest.get(n.tool); if policy is None: blocker(...)`).
So percent_change categorically cannot be used inside a "My Workflow" graph at
all, loop body or otherwise -- this is not a binding-complexity problem to work
around, it's a hard capability gap. Per the plan's own explicit fallback, this
test reports both periods' totals per SKU side by side in the Excel export
(the reader computes/see the decline directly from two real columns) rather
than a computed percent-change column, and skips the optional post-loop
decline-threshold filter for the same reason (no field exists to threshold on
without percent_change, and filter conditions can only compare a field against
a trigger/node/literal value, not against another field on the same row).

Real inventory_ids for the trigger's watch list were found by a one-off live
probe (get_top_customers_by_spend -> get_customer_orders ->
get_product_details_in_order for a few real top-spender customers' real
orders), same "probe before hardcoding" discipline as the other two new tests'
territory discovery -- the probe script was deleted after use; the ids are
hardcoded below as its output.
"""
from __future__ import annotations

import importlib
import io
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
TMP = ROOT / "_smoke" / ".tmp" / "declining-sku-alert-graph"
OUT = ROOT / "_smoke" / ".tmp" / "declining-sku-alert-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "declining_sku_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "declining_sku_password",
    "GOVERNANCE_SESSION_SECRET": "declining-sku-alert-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18591/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18592/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18593/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "30",
})

# Real inventoryIds on the live ERP mirror, found via a one-off probe (deleted
# after use) walking real top-spender customers' real orders' real line items.
REAL_INVENTORY_IDS = ["148304", "32225", "34160", "58718", "60973"]

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
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18591", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18592", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18593", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18591/health")
    wait_health("http://127.0.0.1:18592/health")
    wait_health("http://127.0.0.1:18593/health")
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
        consumer_id="user:declining_sku_builder", name="declining_sku_builder", key_hash="", status="active",
        role="user", type="user", categories=["orders", "office"],
        login_password_hash=hash_password("builder_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "declining_sku_builder", "password": "builder_password"})

        graph = client.post("/workflow-graphs", json={
            "displayName": "Declining-SKU Alert",
            "description": "For a watch list of products, reports total quantity/revenue/orders in two periods "
                            "side by side so a decline is visible at a glance (no percent_change column -- see "
                            "this test's module docstring for why the tool can't be used inside a graph at all).",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                    {"name": "inventory_ids", "label": "Inventory IDs to watch"},
                    {"name": "period_a_start", "label": "Period A start", "optional": True},
                    {"name": "period_a_end", "label": "Period A end", "optional": True},
                    {"name": "period_b_start", "label": "Period B start", "optional": True},
                    {"name": "period_b_end", "label": "Period B end", "optional": True},
                ]}},
                {"nodeId": "loop_a", "kind": "loop", "title": "For each SKU -- period A sales",
                 "inputBindings": {"input": {"source": "trigger", "path": "inventory_ids"}},
                 "config": {"body": ["na1"]}},
                {"nodeId": "na1", "kind": "tool_call", "tool": "get_product_sales",
                 "title": "Period A product sales",
                 "inputBindings": {
                     "inventory_id": {"source": "loop_item"},
                     "start_date": {"source": "trigger", "path": "period_a_start"},
                     "end_date": {"source": "trigger", "path": "period_a_end"},
                 }, "config": {}},
                {"nodeId": "loop_b", "kind": "loop", "title": "For each SKU -- period B sales",
                 "inputBindings": {"input": {"source": "trigger", "path": "inventory_ids"}},
                 "config": {"body": ["nb1"]}},
                {"nodeId": "nb1", "kind": "tool_call", "tool": "get_product_sales",
                 "title": "Period B product sales",
                 "inputBindings": {
                     "inventory_id": {"source": "loop_item"},
                     "start_date": {"source": "trigger", "path": "period_b_start"},
                     "end_date": {"source": "trigger", "path": "period_b_end"},
                 }, "config": {}},
                {"nodeId": "n_join", "kind": "join", "title": "Combine period A and period B totals per SKU",
                 "inputBindings": {
                     "left": {"source": "node", "node_id": "loop_a", "path": "results"},
                     "right": {"source": "node", "node_id": "loop_b", "path": "results"},
                 },
                 "config": {
                     "left_key": "inventoryId", "right_key": "inventoryId",
                     "fields": [
                         {"from": "totalQuantity", "as": "totalQuantityPeriodB"},
                         {"from": "totalRevenue", "as": "totalRevenuePeriodB"},
                         {"from": "orderCount", "as": "orderCountPeriodB"},
                     ],
                     "on_missing": "keep",
                     "table_name": "Declining-SKU Alert",
                 }},
                {"nodeId": "n_report", "kind": "tool_call", "tool": "create_excel_report",
                 "title": "Build declining-SKU alert workbook",
                 "inputBindings": {"tables": {"source": "node", "node_id": "n_join", "path": "mergedTable"}},
                 "config": {"title": "Declining-SKU Alert", "classification": ["INTERNAL"]}},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "loop_a"},
                {"edgeId": "e2", "sourceNodeId": "loop_a", "targetNodeId": "na1"},
                {"edgeId": "e3", "sourceNodeId": "trigger", "targetNodeId": "loop_b"},
                {"edgeId": "e4", "sourceNodeId": "loop_b", "targetNodeId": "nb1"},
                {"edgeId": "e5", "sourceNodeId": "loop_a", "targetNodeId": "n_join"},
                {"edgeId": "e6", "sourceNodeId": "loop_b", "targetNodeId": "n_join"},
                {"edgeId": "e7", "sourceNodeId": "n_join", "targetNodeId": "n_report"},
            ],
        })
        check("declining-SKU alert graph created", graph.status_code == 201, graph.text)
        gid = graph.json()["graphId"]
        pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("declining-SKU alert graph published", pub.status_code == 200, pub.text)

        run = client.post(f"/workflows/{gid}/run", json={
            "inventory_ids": REAL_INVENTORY_IDS,
            "period_a_start": "2018-01-01", "period_a_end": "2024-12-31",
            "period_b_start": "2025-01-01", "period_b_end": "2026-08-25",
        })
        run_body = run.json()
        check("declining-SKU alert run reaches the report step", run.status_code in (201, 409), run_body)
        steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

        loop_a_out = steps_by_id.get("loop_a", {}).get("outputs", {})
        loop_b_out = steps_by_id.get("loop_b", {}).get("outputs", {})
        check("loop_a ran one iteration per watched SKU", loop_a_out.get("iterations") == len(REAL_INVENTORY_IDS), loop_a_out)
        check("loop_b ran one iteration per watched SKU", loop_b_out.get("iterations") == len(REAL_INVENTORY_IDS), loop_b_out)
        check("loop_a completed without a truncation/failure", loop_a_out.get("truncated") is False and loop_a_out.get("failed") == 0, loop_a_out)
        check("loop_b completed without a truncation/failure", loop_b_out.get("truncated") is False and loop_b_out.get("failed") == 0, loop_b_out)

        results_a = loop_a_out.get("results") or []
        results_b = loop_b_out.get("results") or []
        # get_product_sales itself reports status="not_found" (not an error -- a
        # real, valid outcome) for a SKU with zero matching order lines in that
        # date range (mcp-minierp/sqlagent/analytics.py: `status="ok" if rows
        # else "not_found"`) -- exactly the kind of row this workflow exists to
        # surface (e.g. real SKU 148304 below: heavy period-A revenue, zero in
        # period B -- a genuine decline-to-nothing), so both statuses count as a
        # real completed lookup, only an actual "error" status would be a bug.
        check("loop_a's results carry one real get_product_sales lookup per SKU (ok or not_found, never error)",
              len(results_a) == len(REAL_INVENTORY_IDS) and all(r.get("status") in ("ok", "not_found") for r in results_a), results_a)
        check("loop_b's results carry one real get_product_sales lookup per SKU (ok or not_found, never error)",
              len(results_b) == len(REAL_INVENTORY_IDS) and all(r.get("status") in ("ok", "not_found") for r in results_b), results_b)
        check("every period-A row's inventoryId echoes back the real SKU it was asked about (regression check)",
              sorted(str(r.get("inventoryId")) for r in results_a) == sorted(REAL_INVENTORY_IDS), results_a)

        real_revenue_a = {r.get("inventoryId"): r.get("totalRevenue") for r in results_a}
        real_qty_a = {r.get("inventoryId"): r.get("totalQuantity") for r in results_a}
        any_real_sales_a = any(v for v in real_revenue_a.values())
        print(f"  info period A per-SKU totalRevenue from the live mirror: {real_revenue_a}")
        check("at least one watched SKU had real recorded sales in period A (2018-2024, where the probe found orders)",
              any_real_sales_a, real_revenue_a)

        n_join_out = steps_by_id.get("n_join", {}).get("outputs", {})
        check("join ran to completion over real per-SKU data (matchedCount/unmatchedCount/totalCount are real ints)",
              isinstance(n_join_out.get("matchedCount"), int) and isinstance(n_join_out.get("unmatchedCount"), int)
              and n_join_out.get("totalCount") == len(REAL_INVENTORY_IDS), n_join_out)
        merged = n_join_out.get("merged") or []
        check("merged carries every watched SKU (on_missing='keep' -- a SKU with zero period-B sales must still show up)",
              len(merged) == len(REAL_INVENTORY_IDS), merged)
        check("every merged row's own period-A totals survive untouched alongside period B's renamed totals",
              all("totalQuantity" in row and "totalQuantityPeriodB" in row and "totalRevenue" in row
                  and "totalRevenuePeriodB" in row for row in merged), merged[:5])
        by_sku = {row.get("inventoryId"): row for row in merged}
        check("period-A totals on the merged rows genuinely match loop_a's own real results (regression check)",
              all(by_sku.get(iid, {}).get("totalRevenue") == real_revenue_a.get(iid) for iid in real_revenue_a), (by_sku, real_revenue_a))

        if run_body.get("status") == "approval_required":
            approval_id = (run_body.get("approval") or {}).get("approvalId")
            check("broad-export approval was actually created", bool(approval_id), run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "declining_sku_admin", "password": "declining_sku_password"})
            approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed declining-SKU alert workbook"})
            check("admin approves the broad export", approve.status_code == 200, approve.text)
            resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
            run_body = resumed.json()
            check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "declining_sku_builder", "password": "builder_password"})
        else:
            check("declining-SKU alert run completes without needing approval", run_body.get("status") == "completed", run_body)

        n_report_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n_report"), {})
        aid = n_report_step.get("outputs", {}).get("artifactId")
        check("declining-SKU alert workbook artifact created", bool(aid), n_report_step)

        if aid:
            dl = client.get(f"/artifacts/{aid}/download")
            check("workbook artifact downloads", dl.status_code == 200, dl.status_code)
            (OUT / "declining-sku-alert.xlsx").write_bytes(dl.content)
            cells = xlsx_cell_texts(dl.content)
            check("workbook has more than a couple cells (regression check)", len(cells) > 5, len(cells))
            check("workbook contains a real watched SKU's inventoryId from the live mirror",
                  any(str(merged[0].get("inventoryId")) in c for c in cells), cells[:20])

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
