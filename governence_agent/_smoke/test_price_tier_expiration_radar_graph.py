"""End-to-end test of "Price-Tier Expiration Radar" as a real, user-buildable
"My Workflow" graph -- workflow #10 of this batch's final 3, same recipe as
test_gl_period_variance_watch_graph.py's `loop` + downstream `filter`:

  trigger (inventory_ids: list[str], window_days)
    -> loop over inventory_ids
         body: tool_call get_sales_price(inventory_id <- loop_item, fetch_all: true)
    -> filter: input <- loop's "results"; status eq "success"
       (keeps watched items that have real ARSalesPrice data on file, drops
       any watched inventory id that turned out to have none at all)
    -> tool_call: create_excel_report (tables <- filter's "matchedTable")
    -> tool_call: create_email_draft (never sent -- an internal pricing digest)

Deviation from the original "flag tiers expiring within window_days" ask (the
task brief's own "confirm the real field name" hedge, and the SAME nested-
output limit test_gl_period_variance_watch_graph.py and this batch's sibling
test_po_margin_sanity_check_graph.py both hit and documented): `expirationDate`
IS a real, confirmed field (mcp-minierp/sqlagent/finance/schemas.py's
SalesPriceRecord, sourced from ARSalesPrice.expirationDate) -- but it lives
inside get_sales_price's `records` list, one entry per price tier, NOT as a
flat top-level field on the tool's own per-call result. `filter`'s condition
evaluator (gateway/workflow_graph_interpreter.py's `_evaluate_condition_tree`,
`row.get(tree.get("field"))`) can only read a literal top-level field off each
row of its input list -- it cannot reach inside a nested `records` array to
compare one tier's `expirationDate`, and a watched item can carry MULTIPLE
tiers (multiple price classes) with different expiration dates each, so there
isn't even a single unambiguous date to promote. So this graph cannot compute
"expires within window_days" as a boolean per tier; it instead radars for the
more basic, still-genuine gap this mechanism CAN prove end-to-end: which
watched items have zero real price-tier data on file at all (status ==
"success" vs "not_found", proven below by deliberately watching one id that
doesn't exist). Every kept row's real, un-flattened `records` list -- with its
real `expirationDate` values -- still reaches the exported workbook (as a
stringified nested value, same appearance as GL Period Variance Watch's own
`records` column) for a human reviewer to actually check the dates.
window_days is still accepted as a trigger input, kept for forward
compatibility with a future per-tier flattening/map node, but is not
mechanically enforced by any node today -- same honest scope-limit shape as
GL Period Variance Watch's threshold_pct.

Real, live-probed kill switch (2026-08-26): gateway/govern.py gates
get_sales_price OFF by default company-wide ("_GATED_TOOLS_ENV_FLAGS =
{'get_sales_price': 'ALLOW_PRICE_QUERY'}", regardless of what any
category/consumer grants) -- "the pricing surface was wired up ahead of a full
security review of that data." This test deliberately sets
ALLOW_PRICE_QUERY=true in its OWN subprocess environment (below) to prove the
graph mechanism end-to-end for when that review clears the switch -- it does
not touch, and has no effect on, any real deployment's actual (still gated)
default. Same finding this batch's sibling test_po_margin_sanity_check_graph.py
also documents.

Real, live-probed inventory ids (2026-08-26, direct ARSalesPrice probe -- see
_smoke/.tmp/probe_sales_price_for_po.py, since cleaned up): "153755",
"179032".."179037" all carry real price-tier records (some with real
expirationDate values already in the past relative to today, e.g.
2022-11-13/2024-02-06 -- confirming this IS live, not synthetic, data).
"999999999" is included in the watch list too, to prove the filter genuinely
excludes an item with no real price data rather than crashing or silently
including it.
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
TMP = ROOT / "_smoke" / ".tmp" / "price-tier-expiration-radar-graph"
OUT = ROOT / "_smoke" / ".tmp" / "price-tier-expiration-radar-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "price_radar_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "price_radar_password",
    "GOVERNANCE_SESSION_SECRET": "price-tier-expiration-radar-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    # get_sales_price is gated OFF company-wide by default (gateway/govern.py's
    # _GATED_TOOLS_ENV_FLAGS) pending a full security review -- opted in here,
    # for this test's own subprocess environment only, to prove the graph
    # mechanism end-to-end (see the module docstring).
    "ALLOW_PRICE_QUERY": "true",
    "MINIERP_MCP_URL": "http://127.0.0.1:18661/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18662/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18663/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18661", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18662", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18663", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18661/health")
    wait_health("http://127.0.0.1:18662/health")
    wait_health("http://127.0.0.1:18663/health")
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
        consumer_id="user:price_radar_builder", name="price_radar_builder", key_hash="", status="active",
        role="user", type="user", categories=["finance", "office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "price_radar_builder", "password": "builder_password"})

        graph = client.post("/workflow-graphs", json={
            "displayName": "Price-Tier Expiration Radar",
            "description": "Loops over a watched list of inventory ids and reports each item's real "
                            "ARSalesPrice tier records (including real expirationDate values) on file. No "
                            "computed 'expires within window_days' boolean -- see the graph notes.",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                    {"name": "inventory_ids", "label": "Inventory ids to watch"},
                    {"name": "window_days", "label": "Expiration window (days)", "optional": True},
                ]}},
                {"nodeId": "n_loop", "kind": "loop", "title": "For each watched inventory item",
                 "inputBindings": {"input": {"source": "trigger", "path": "inventory_ids"}},
                 "config": {"body": ["n1"]}},
                {"nodeId": "n1", "kind": "tool_call", "tool": "get_sales_price",
                 "title": "Look up real sales price tiers",
                 "inputBindings": {"inventory_id": {"source": "loop_item"}},
                 "config": {"fetch_all": True}},
                {"nodeId": "n2", "kind": "filter", "title": "Keep items with real price data on file",
                 "inputBindings": {"input": {"source": "node", "node_id": "n_loop", "path": "results"}},
                 "config": {"table_name": "Price-Tier Expiration Radar",
                            "conditions": {"all": [{"field": "status", "op": "eq", "value": "success"}]}}},
                {"nodeId": "n3", "kind": "tool_call", "tool": "create_excel_report",
                 "title": "Build price-tier expiration radar workbook",
                 "inputBindings": {"tables": {"source": "node", "node_id": "n2", "path": "matchedTable"}},
                 "config": {"title": "Price-Tier Expiration Radar", "classification": ["INTERNAL"]}},
                {"nodeId": "n4", "kind": "tool_call", "tool": "create_email_draft",
                 "title": "Draft price-tier radar digest email",
                 "config": {
                     "to": ["pricing-team@frontierdental.com"],
                     "subject": "Price-Tier Expiration Radar",
                     "body_markdown": "Hi team,\n\nThe attached workbook lists real ARSalesPrice tier records "
                                      "(including expiration dates) on file for each watched inventory item. "
                                      "Watched items with no price data on file are omitted -- review the "
                                      "attached expiration dates directly for anything due to lapse.\n\n"
                                      "Regards,\nGoverned AI Office Assistant",
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
        check("price-tier expiration radar graph created", graph.status_code == 201, graph.text)
        gid = graph.json()["graphId"]
        pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("price-tier expiration radar graph published", pub.status_code == 200, pub.text)

        # "153755"/"179032".."179037" confirmed to have real ARSalesPrice tier
        # records by direct live probe (2026-08-26); "999999999" is a
        # deliberately fake id, included to prove the filter genuinely
        # excludes it.
        inventory_ids = ["153755", "179032", "179033", "179034", "179035", "179036", "179037", "999999999"]
        run = client.post(f"/workflows/{gid}/run", json={"inventory_ids": inventory_ids, "window_days": 90})
        run_body = run.json()
        check("price-tier expiration radar run reaches the workbook step", run.status_code == 201, run_body)
        steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

        loop_out = steps_by_id.get("n_loop", {}).get("outputs", {})
        check("loop ran once per watched inventory id", loop_out.get("itemCount") == len(inventory_ids)
              and loop_out.get("iterations") == len(inventory_ids), loop_out)
        check("loop completed without truncation", loop_out.get("truncated") is False, loop_out)
        loop_results = loop_out.get("results") or []
        succeeded_ids = {r.get("inventoryId") for r in loop_results if r.get("status") == "success"}
        check("every real watched inventory id came back with real price-tier data",
              {"153755", "179032", "179033", "179034", "179035", "179036", "179037"} <= succeeded_ids, loop_results)
        not_found_ids = {r.get("inventoryId") for r in loop_results if r.get("status") == "not_found"}
        check("the fake watched id really came back not_found (regression check)",
              "999999999" in not_found_ids, loop_results)

        n2_out = steps_by_id.get("n2", {}).get("outputs", {})
        matched_rows = n2_out.get("matched") or []
        matched = n2_out.get("matchedCount") or 0
        check("filter kept exactly the items with real price data, dropped the fake one",
              matched == len(succeeded_ids) and all(r.get("status") == "success" for r in matched_rows),
              (matched, matched_rows))
        check("every kept row carries its real, unflattened price-tier records (with real expirationDate values)",
              all(r.get("records") for r in matched_rows)
              and any(any(t.get("expirationDate") for t in r.get("records") or []) for r in matched_rows),
              matched_rows)

        if run_body.get("status") == "approval_required":
            approval_id = (run_body.get("approval") or {}).get("approvalId")
            check("broad-export approval was actually created", bool(approval_id), run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "price_radar_admin", "password": "price_radar_password"})
            approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed price-tier expiration radar workbook"})
            check("admin approves the broad export", approve.status_code == 200, approve.text)
            resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
            run_body = resumed.json()
            check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
            client.post("/dashboard/logout")
            client.post("/dashboard/login", json={"username": "price_radar_builder", "password": "builder_password"})
        else:
            check("price-tier expiration radar run completes without needing approval", run_body.get("status") == "completed", run_body)

        n3_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n3"), {})
        aid = n3_step.get("outputs", {}).get("artifactId")
        check("price-tier expiration radar workbook artifact created", bool(aid), n3_step)

        if aid:
            dl = client.get(f"/artifacts/{aid}/download")
            check("workbook artifact downloads", dl.status_code == 200, dl.status_code)
            (OUT / "price-tier-expiration-radar.xlsx").write_bytes(dl.content)
            cells = xlsx_cell_texts(dl.content)
            check("workbook has more than a couple cells (regression check)", len(cells) > 5, len(cells))
            real_id = str(matched_rows[0].get("inventoryId") or "")
            check("workbook contains a real watched inventory id from the live mirror",
                  any(real_id == c or (real_id and real_id in c) for c in cells), cells[:20])

        n4_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n4"), {})
        check("price-tier radar email drafted, never sent", n4_step.get("status") == "completed" and n4_step.get("outputs", {}).get("draftId")
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
