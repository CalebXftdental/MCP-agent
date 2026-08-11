"""Smoke test for the generic `filter` node kind ("My Workflow" node #5).

Network-independent by design (only starts mcp-office/mcp-email, no mcp-minierp,
no live ERP call) -- this is deliberately isolated from any live-network
dependency so it proves the filter node's own mechanics (condition evaluation,
matched/unmatched split, matchedTable/unmatchedTable shape, validate_graph
rejections) regardless of whether db-api.frontierdental.com is reachable from
this environment. The end-to-end proof against the real ERP (get_customer_order_recency
+ this same filter node + create_pdf_packet/create_email_draft) is
_smoke/test_winback_radar_graph.py.

Built the same way a human would build it by hand on the My Workflow page: a
trigger declaring one input (`rows`, a JSON array), and four `filter` nodes each
bound directly to that trigger input, each exercising one condition op --
`eq`, `gt`, `older_than_days`, `not_in`. A run's `steps[].outputs` for a filter
node is exactly `{matched, unmatched, matchedCount, totalCount, matchedTable,
unmatchedTable}` (gateway/workflow_graph_interpreter.py::_execute_filter_node).

Also proves two validate_graph rejections that must fire at CREATE time, before
a broken filter node can ever be published or run: an unrecognized op, and an
`input` binding pointing at a node id that doesn't exist.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "filter-node"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "filter_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "filter_password",
    "GOVERNANCE_SESSION_SECRET": "filter-node-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18441/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18442/mcp",
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


def days_since(date_str: str) -> int:
    dt = datetime.fromisoformat(date_str)
    now = datetime.now(timezone.utc) if dt.tzinfo is not None else datetime.now()
    return (now - dt).days


print("start office + email backends")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18441", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18442", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18441/health")
    wait_health("http://127.0.0.1:18442/health")
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
        consumer_id="user:filter_builder", name="filter_builder", key_hash="", status="active",
        role="user", type="user", categories=["office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    rows = [
        {"id": "A", "amount": 150, "tags": ["vip", "east"], "lastOrder": "2020-01-01"},
        {"id": "B", "amount": 50, "tags": ["west"], "lastOrder": "2026-08-10"},
        {"id": "C", "amount": 200, "tags": ["vip"], "lastOrder": None},
        {"id": "D", "amount": 100, "tags": ["vip", "west"], "lastOrder": "2026-08-01"},
    ]
    # Expected sets computed the same way the interpreter would, not hardcoded
    # against an assumed "today" -- older_than_days depends on wall-clock time.
    expect_eq = {"A"}
    expect_gt = {r["id"] for r in rows if r["amount"] > 100}
    expect_older_30 = {r["id"] for r in rows if r["lastOrder"] and days_since(r["lastOrder"]) > 30}
    expect_not_in_ab = {r["id"] for r in rows if r["id"] not in ("A", "B")}

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "filter_builder", "password": "builder_password"})

        # ── Graph: one trigger input (`rows`), four filter nodes each testing
        #    one op, all bound directly to the trigger's `rows` -- exactly the
        #    shape a human would build by hand on the My Workflow page (Add a
        #    step -> Filter -> bind "List to filter" -> "From this workflow's
        #    input" -> add condition rows). ──
        graph = client.post("/workflow-graphs", json={
            "displayName": "Filter Node Smoke Test",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                    {"name": "rows", "label": "Rows"}, {"name": "limit", "label": "Limit"},
                ]}},
                {"nodeId": "n_eq", "kind": "filter", "title": "eq",
                 "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": [{"field": "id", "op": "eq", "value": "A"}]}}},
                {"nodeId": "n_gt", "kind": "filter", "title": "gt",
                 "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": [{"field": "amount", "op": "gt", "value": 100}]}}},
                {"nodeId": "n_older", "kind": "filter", "title": "older_than_days",
                 "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": [{"field": "lastOrder", "op": "older_than_days", "value": 30}]},
                            "table_name": "Stale Rows"}},
                {"nodeId": "n_not_in", "kind": "filter", "title": "not_in",
                 "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": [{"field": "id", "op": "not_in", "value": ["A", "B"]}]}}},
                # match_limit bound to a trigger input (the "collect N matched" case) --
                # condition matches everything (empty "all"), so the full match set is
                # all 4 rows and match_limit=2 (from the trigger) should cap it to the
                # first 2 in original row order, with matchLimitReached=True.
                {"nodeId": "n_limit", "kind": "filter", "title": "limit",
                 "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": []}, "match_limit": {"source": "trigger", "path": "limit"}}},
                # match_limit as a literal, set HIGHER than the true match count (only
                # "A" matches eq) -- matchLimitReached should come back False, proving
                # it means "we found that many," not "we were asked for that many."
                {"nodeId": "n_limit_short", "kind": "filter", "title": "limit_short",
                 "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": [{"field": "id", "op": "eq", "value": "A"}]}, "match_limit": 100}},
            ],
            "edges": [
                {"edgeId": f"e_{n}", "sourceNodeId": "trigger", "targetNodeId": n}
                for n in ("n_eq", "n_gt", "n_older", "n_not_in", "n_limit", "n_limit_short")
            ],
        })
        check("filter graph created", graph.status_code == 201, graph.text)
        gid = graph.json()["graphId"]
        pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("filter graph published", pub.status_code == 200, pub.text)

        run = client.post(f"/workflows/{gid}/run", json={"rows": rows, "limit": 2})
        run_body = run.json()
        check("filter graph run completes", run.status_code == 201 and run_body.get("status") == "completed", run_body)
        steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

        cases = [
            # table_name defaults to the node's own title when config.table_name
            # isn't set (n_older is the one node that overrides it explicitly).
            ("n_eq", expect_eq, "eq"),
            ("n_gt", expect_gt, "gt"),
            ("n_older", expect_older_30, "Stale Rows"),
            ("n_not_in", expect_not_in_ab, "not_in"),
        ]
        for step_id, expected_matched_ids, table_name in cases:
            step = steps_by_id.get(step_id, {})
            outputs = step.get("outputs", {})
            matched_ids = {r["id"] for r in outputs.get("matched", [])}
            unmatched_ids = {r["id"] for r in outputs.get("unmatched", [])}
            check(f"{step_id}: matched rows are exactly {sorted(expected_matched_ids)}", matched_ids == expected_matched_ids, outputs)
            check(f"{step_id}: unmatched rows are the complement", unmatched_ids == {r["id"] for r in rows} - expected_matched_ids, outputs)
            check(f"{step_id}: matchedCount/totalCount are correct", outputs.get("matchedCount") == len(expected_matched_ids) and outputs.get("totalCount") == len(rows), outputs)
            table = outputs.get("matchedTable") or []
            check(f"{step_id}: matchedTable is [{{name, rows}}] shaped for create_pdf_packet/create_excel_report",
                  isinstance(table, list) and len(table) == 1 and table[0].get("name") == table_name
                  and {r["id"] for r in table[0].get("rows", [])} == expected_matched_ids, table)

        # ── match_limit: "collect up to N matches" is a different question than
        #    page_size on a bulk tool (how many raw rows get fetched) -- these
        #    prove the filter node's own answer to it. ──
        limit_out = steps_by_id.get("n_limit", {}).get("outputs", {})
        check("n_limit: match_limit bound to a trigger input caps matchedCount to 2", limit_out.get("matchedCount") == 2, limit_out)
        check("n_limit: totalMatchCount reports the TRUE match count before capping (all 4 rows)", limit_out.get("totalMatchCount") == len(rows), limit_out)
        check("n_limit: matchLimitReached is True (found at least as many as asked for)", limit_out.get("matchLimitReached") is True, limit_out)
        check("n_limit: matched keeps the first N in original row order, not an arbitrary subset",
              [r["id"] for r in limit_out.get("matched", [])] == [r["id"] for r in rows[:2]], limit_out)
        check("n_limit: matchedTable rows count reflects the LIMITED set, not the full match set",
              len((limit_out.get("matchedTable") or [{}])[0].get("rows", [])) == 2, limit_out)
        check("n_limit: unmatched is unaffected by match_limit (still exactly the rows that failed the condition)",
              len(limit_out.get("unmatched", [])) == 0, limit_out)

        short_out = steps_by_id.get("n_limit_short", {}).get("outputs", {})
        check("n_limit_short: a literal match_limit higher than the true match count keeps all real matches", short_out.get("matchedCount") == 1, short_out)
        check("n_limit_short: matchLimitReached is False -- asked for 100, only 1 exists, that's a real shortfall to surface",
              short_out.get("matchLimitReached") is False, short_out)

        # ── Empty conditions (no rows added yet) matches everything -- the
        #    UI's default for a brand-new filter step, not an accidental
        #    "match nothing" trap for someone who hasn't configured it yet. ──
        empty_graph = client.post("/workflow-graphs", json={
            "displayName": "Filter Node Empty Conditions",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [{"name": "rows", "label": "Rows"}]}},
                {"nodeId": "n1", "kind": "filter", "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": []}}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
        })
        check("empty-conditions graph created", empty_graph.status_code == 201, empty_graph.text)
        egid = empty_graph.json()["graphId"]
        client.post(f"/workflow-graphs/{egid}/publish", json={})
        erun = client.post(f"/workflows/{egid}/run", json={"rows": rows}).json()
        en1 = next((s for s in erun.get("steps", []) if s.get("stepId") == "n1"), {})
        check("empty conditions match every row", en1.get("outputs", {}).get("matchedCount") == len(rows), en1)

        # ── validate_graph rejections at CREATE time ──
        bad_op = client.post("/workflow-graphs", json={
            "displayName": "Filter Node Bad Op",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger"},
                {"nodeId": "n1", "kind": "filter", "inputBindings": {"input": {"source": "trigger", "path": "rows"}},
                 "config": {"conditions": {"all": [{"field": "id", "op": "bogus_op", "value": "A"}]}}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
        })
        check("unknown filter op is rejected at create time", bad_op.status_code == 400 and "unknown filter condition op" in bad_op.text, bad_op.text)

        dangling = client.post("/workflow-graphs", json={
            "displayName": "Filter Node Dangling Input",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger"},
                {"nodeId": "n1", "kind": "filter", "inputBindings": {"input": {"source": "node", "node_id": "does_not_exist", "path": "rows"}},
                 "config": {"conditions": {"all": []}}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
        })
        check("dangling filter input reference is rejected at create time", dangling.status_code == 400 and "unknown node" in dangling.text, dangling.text)

finally:
    office.terminate()
    email.terminate()
    for proc in (office, email):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
