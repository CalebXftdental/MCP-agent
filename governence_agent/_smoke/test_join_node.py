"""Smoke test for the generic `join` node kind ("My Workflow" node #6).

No backend subprocess needed (unlike test_filter_node.py) -- every node here binds
directly to trigger inputs, no tool_call involved, so this only exercises the join
node's own mechanics (merge-by-key, fields shapes, on_missing, non-list-input
failure) and validate_graph's join-specific rejections.

Built the way a human would build it on the My Workflow page: a trigger declaring
two array inputs (`left`, `right`) plus a scalar (`notAList`), and several `join`
nodes each bound directly to those trigger inputs, exercising one behavior each.
A run's `steps[].outputs` for a join node is exactly `{merged, unmatched,
matchedCount, unmatchedCount, totalCount, mergedTable}`
(gateway/workflow_graph_interpreter.py::_execute_join_node).
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "join-node"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "join_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "join_password",
    "GOVERNANCE_SESSION_SECRET": "join-node-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
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


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")
from store import get_store  # noqa: E402
from store.models import ConsumerRecord  # noqa: E402
from auth.passwords import hash_password  # noqa: E402

store = get_store()
store.upsert_consumer(ConsumerRecord(
    consumer_id="user:join_builder", name="join_builder", key_hash="", status="active",
    role="user", type="user", categories=[],
    login_password_hash=hash_password("builder_password"),
))

LEFT = [
    {"customerId": "C1", "name": "Alice", "status": "active"},
    {"customerId": "C2", "name": "Bob", "status": "active"},
    {"customerId": "C3", "name": "Carol", "status": "closed"},   # no match on the right
]
RIGHT = [
    {"customerId": "C1", "email": "alice@example.com", "status": "verified"},
    {"customerId": "C2", "email": "bob@example.com", "status": "verified"},
    {"customerId": "C2", "email": "bob-dup@example.com", "status": "unverified"},  # duplicate key
]

with TestClient(gateway_app.app, base_url="http://testserver") as client:
    client.post("/dashboard/login", json={"username": "join_builder", "password": "builder_password"})

    graph = client.post("/workflow-graphs", json={
        "displayName": "Join Node Smoke Test",
        "nodes": [
            {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                {"name": "left", "label": "Left"}, {"name": "right", "label": "Right"},
                {"name": "notAList", "label": "Not a list"},
            ]}},
            # plain field list -- kept under the same name
            {"nodeId": "n_basic", "kind": "join", "title": "basic",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "customerId", "right_key": "customerId", "fields": ["email"]}},
            # "*" -- bring over everything except the join key, INCLUDING a colliding
            # field name ("status") -- proves last-write-wins is what "*" means, and
            # is exactly why the rename form below exists.
            {"nodeId": "n_star", "kind": "join", "title": "star",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "customerId", "right_key": "customerId", "fields": "*"}},
            # {"from","as"} rename -- avoids the collision "*" doesn't
            {"nodeId": "n_rename", "kind": "join", "title": "rename",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "customerId", "right_key": "customerId",
                        "fields": [{"from": "email", "as": "email"}, {"from": "status", "as": "rightStatus"}]}},
            # on_missing default ("keep") -- C3 (no right match) still appears in merged, unenriched
            {"nodeId": "n_keep", "kind": "join", "title": "keep",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "customerId", "right_key": "customerId", "fields": ["email"]}},
            # on_missing="drop" -- C3 excluded from merged, but still reported in unmatched
            {"nodeId": "n_drop", "kind": "join", "title": "drop",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "customerId", "right_key": "customerId", "fields": ["email"], "on_missing": "drop",
                        "table_name": "Enriched Customers"}},
            # non-list input -- must fail loudly, not silently coerce to []
            {"nodeId": "n_bad_left", "kind": "join", "title": "bad_left",
             "inputBindings": {"left": {"source": "trigger", "path": "notAList"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "customerId", "right_key": "customerId", "fields": ["email"]}},
        ],
        "edges": [
            {"edgeId": f"e_{n}", "sourceNodeId": "trigger", "targetNodeId": n}
            for n in ("n_basic", "n_star", "n_rename", "n_keep", "n_drop", "n_bad_left")
        ],
    })
    check("join graph created", graph.status_code == 201, graph.text)
    gid = graph.json()["graphId"]
    pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
    check("join graph published", pub.status_code == 200, pub.text)

    run = client.post(f"/workflows/{gid}/run", json={"left": LEFT, "right": RIGHT, "notAList": "not-a-list"})
    run_body = run.json()
    # n_bad_left is deliberately wired to a non-list input -- it fails, which fails
    # the whole run, so _workflow_run_start reports 409 (an "error" result), not
    # 201 -- confirmed against backend/workflow_api.py's _workflow_run_start. The
    # completed steps that ran BEFORE the failure are still present in the body.
    check("run reports 409 (one node deliberately fails, per _workflow_run_start's error-result contract)",
          run.status_code == 409, run_body)
    steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

    basic = steps_by_id.get("n_basic", {}).get("outputs", {})
    check("basic: matchedCount/unmatchedCount/totalCount", (basic.get("matchedCount"), basic.get("unmatchedCount"), basic.get("totalCount")) == (2, 1, 3), basic)
    by_id = {r["customerId"]: r for r in basic.get("merged", [])}
    check("basic: C1 got email attached, name preserved", by_id.get("C1") == {"customerId": "C1", "name": "Alice", "status": "active", "email": "alice@example.com"}, by_id)
    check("basic: duplicate right key -- first match wins (bob@example.com, not the dup)", by_id.get("C2", {}).get("email") == "bob@example.com", by_id)
    check("basic: C3 (no match) present in merged, unenriched (default on_missing=keep)", by_id.get("C3") == {"customerId": "C3", "name": "Carol", "status": "closed"}, by_id)
    check("basic: unmatched lists exactly the unmatched left row", [r["customerId"] for r in basic.get("unmatched", [])] == ["C3"], basic)
    check("basic: mergedTable is [{name, rows}] shaped for create_pdf_packet/create_excel_report",
          basic.get("mergedTable") == [{"name": "basic", "rows": basic.get("merged")}], basic)

    star = steps_by_id.get("n_star", {}).get("outputs", {})
    star_c1 = next(r for r in star.get("merged", []) if r["customerId"] == "C1")
    check("star: brings over every right field except the join key, including the colliding one",
          star_c1 == {"customerId": "C1", "name": "Alice", "status": "verified", "email": "alice@example.com"}, star_c1)

    rename = steps_by_id.get("n_rename", {}).get("outputs", {})
    rename_c1 = next(r for r in rename.get("merged", []) if r["customerId"] == "C1")
    check("rename: left's own status survives untouched, right's status renamed to rightStatus",
          rename_c1 == {"customerId": "C1", "name": "Alice", "status": "active", "email": "alice@example.com", "rightStatus": "verified"}, rename_c1)

    drop = steps_by_id.get("n_drop", {}).get("outputs", {})
    check("drop: C3 excluded from merged", "C3" not in {r["customerId"] for r in drop.get("merged", [])}, drop)
    check("drop: C3 STILL reported in unmatched -- never silently invisible", [r["customerId"] for r in drop.get("unmatched", [])] == ["C3"], drop)
    check("drop: matchedCount/unmatchedCount unaffected by on_missing (still describes the true match state)",
          (drop.get("matchedCount"), drop.get("unmatchedCount")) == (2, 1), drop)
    check("drop: mergedTable uses the configured table_name", (drop.get("mergedTable") or [{}])[0].get("name") == "Enriched Customers", drop)

    bad_left = steps_by_id.get("n_bad_left", {})
    check("bad_left: a non-list left input fails the step loudly", bad_left.get("status") == "failed", bad_left)
    check("bad_left: error names the problem", "did not resolve to a list" in (bad_left.get("error") or ""), bad_left)

    # ── validate_graph rejections at CREATE time ──
    missing_key = client.post("/workflow-graphs", json={
        "displayName": "Join Missing Key",
        "nodes": [
            {"nodeId": "trigger", "kind": "trigger"},
            {"nodeId": "n1", "kind": "join",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
    })
    check("missing left_key/right_key is rejected at create time", missing_key.status_code == 400 and "left_key is required" in missing_key.text, missing_key.text)

    bad_on_missing = client.post("/workflow-graphs", json={
        "displayName": "Join Bad on_missing",
        "nodes": [
            {"nodeId": "trigger", "kind": "trigger"},
            {"nodeId": "n1", "kind": "join",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "id", "right_key": "id", "on_missing": "explode"}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
    })
    check("invalid on_missing is rejected at create time", bad_on_missing.status_code == 400 and "on_missing must be one of" in bad_on_missing.text, bad_on_missing.text)

    bad_fields = client.post("/workflow-graphs", json={
        "displayName": "Join Bad Fields",
        "nodes": [
            {"nodeId": "trigger", "kind": "trigger"},
            {"nodeId": "n1", "kind": "join",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "id", "right_key": "id", "fields": [{"as": "noFromField"}]}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
    })
    check("a fields entry missing 'from' is rejected at create time", bad_fields.status_code == 400 and "non-empty 'from' name" in bad_fields.text, bad_fields.text)

    missing_fields = client.post("/workflow-graphs", json={
        "displayName": "Join Missing Fields",
        "nodes": [
            {"nodeId": "trigger", "kind": "trigger"},
            {"nodeId": "n1", "kind": "join",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "id", "right_key": "id"}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
    })
    check("omitting fields entirely is rejected at create time (would silently merge nothing)",
          missing_fields.status_code == 400 and "fields is required" in missing_fields.text, missing_fields.text)

    empty_fields = client.post("/workflow-graphs", json={
        "displayName": "Join Empty Fields List",
        "nodes": [
            {"nodeId": "trigger", "kind": "trigger"},
            {"nodeId": "n1", "kind": "join",
             "inputBindings": {"left": {"source": "trigger", "path": "left"}, "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "id", "right_key": "id", "fields": []}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
    })
    check("an empty fields list is rejected the same way as omitting it", empty_fields.status_code == 400 and "fields is required" in empty_fields.text, empty_fields.text)

    dangling = client.post("/workflow-graphs", json={
        "displayName": "Join Dangling Input",
        "nodes": [
            {"nodeId": "trigger", "kind": "trigger"},
            {"nodeId": "n1", "kind": "join",
             "inputBindings": {"left": {"source": "node", "node_id": "does_not_exist", "path": "rows"},
                                "right": {"source": "trigger", "path": "right"}},
             "config": {"left_key": "id", "right_key": "id"}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"}],
    })
    check("dangling join input reference is rejected at create time", dangling.status_code == 400 and "unknown node" in dangling.text, dangling.text)

    # ── registry/prompt consistency (2026-08-19 refactor) ──────────────────────
    import workflow_graph_store as wgs  # noqa: E402
    import orchestrator  # noqa: E402

    check("join is derivable from the schema registry, not just a hand-maintained VALID_NODE_KINDS entry",
          "join" in wgs.NODE_KIND_SCHEMAS and "join" in wgs.VALID_NODE_KINDS)
    rendered = wgs.render_node_kind_prompt("join")
    join_field_names = {f.name for f in wgs.NODE_KIND_SCHEMAS["join"].fields}
    check("every join schema field name appears in its generated prompt text",
          all(f"'{name}'" in rendered for name in join_field_names), (join_field_names, rendered))
    check("every join output key appears in its generated prompt text",
          all(f"`{k}`" in rendered for k in wgs.NODE_KIND_SCHEMAS["join"].outputs), rendered)
    check("the copilot's actual system prompt embeds the generated join section (not a stale hand-written copy)",
          rendered in orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT, "join section missing or diverged from the registry")
    check("the loop node -- shipped with zero prompt docs before this refactor -- now has a generated section too",
          wgs.render_node_kind_prompt("loop") in orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT)
    check("the stale 'no for-each node exists yet' claim is gone now that loop is documented",
          "no for-each" not in orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
