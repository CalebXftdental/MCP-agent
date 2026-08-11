"""Smoke test for the workflow copilot's edit-an-existing-workflow capability:
  - list_my_workflows (scoped to the calling principal only)
  - get_my_workflow (ownership-enforced read of a graph's current version)
  - propose_graph(graph_id=...) editing an EXISTING graph via a new version,
    instead of creating a duplicate -- including the ownership guard and the
    "editing never touches what's already published" invariant.
Plus the exclude mechanism: both new tools must be WORKFLOW-ONLY, same as
propose_graph/update_workflow_plan (Home chat must never see them).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "workflow-copilot-edit"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "edit_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "edit_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-copilot-edit-secret",
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
        print(f"  FAIL {name}", str(detail)[:600] if detail is not None else "")


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")

import orchestrator  # noqa: E402
import request_context as ctx  # noqa: E402
import workflow_graph_store  # noqa: E402
from mcp_server import mcp  # noqa: E402
from policy.resolve import resolve as resolve_grant  # noqa: E402
from store import get_store  # noqa: E402
from store.models import ConsumerRecord  # noqa: E402
from auth.passwords import hash_password  # noqa: E402

store = get_store()
owner_record = ConsumerRecord(
    consumer_id="user:edit_owner", name="edit_owner", key_hash="", status="active",
    role="user", type="user", categories=["office"],
    login_password_hash=hash_password("owner_password"),
)
other_record = ConsumerRecord(
    consumer_id="user:edit_other", name="edit_other", key_hash="", status="active",
    role="user", type="user", categories=["office"],
    login_password_hash=hash_password("other_password"),
)
store.upsert_consumer(owner_record)
store.upsert_consumer(other_record)


def act_as(record):
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set("127.0.0.1")


async def call_tool(name, args):
    call_args = {"session_id": "copilot-edit-test", **args}
    result = await mcp.call_tool(name, call_args)
    content = result[0] if isinstance(result, tuple) else result
    for part in content or []:
        if getattr(part, "text", None) is not None:
            return json.loads(part.text)
    return {}


TRIGGER = {"nodeId": "t1", "kind": "trigger"}
STEP_1 = {"nodeId": "n1", "kind": "tool_call", "tool": "create_pdf_packet", "title": "Build packet",
          "config": {"title": "Test", "sections": [{"heading": "H", "bullets": ["a"]}], "classification": ["INTERNAL"]}}
EDGE_1 = {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}


async def main():
    # ── Exclude mechanism: both new tools are WORKFLOW-ONLY ────────────────────
    act_as(owner_record)
    grant = resolve_grant(owner_record, store.get_category, store.get_department)
    all_tools = await mcp.list_tools()
    home_names = {s["function"]["name"] for s in orchestrator.build_tool_specs(all_tools, grant, exclude=orchestrator.WORKFLOW_ONLY_TOOLS)}
    workflow_names = {s["function"]["name"] for s in orchestrator.build_tool_specs(all_tools, grant, exclude=frozenset())}
    check("Home's tool list excludes list_my_workflows", "list_my_workflows" not in home_names, home_names)
    check("Home's tool list excludes get_my_workflow", "get_my_workflow" not in home_names, home_names)
    check("Workflow copilot's tool list includes list_my_workflows", "list_my_workflows" in workflow_names)
    check("Workflow copilot's tool list includes get_my_workflow", "get_my_workflow" in workflow_names)

    # ── list_my_workflows starts empty for a fresh principal ───────────────────
    empty_list = await call_tool("list_my_workflows", {})
    check("list_my_workflows succeeds", empty_list.get("status") == "success", empty_list)
    check("no workflows yet for this fresh principal", empty_list.get("workflows") == [], empty_list)

    # ── Create the workflow to be edited (mirrors a normal propose_graph call) ─
    created = await call_tool("propose_graph", {
        "display_name": "Editable test workflow",
        "nodes": [TRIGGER, STEP_1],
        "edges": [EDGE_1],
    })
    check("initial create succeeds", created.get("status") == "success", created)
    check("initial create is NOT marked as an edit", created.get("edited") is False, created)
    gid = created.get("graphId")
    check("a real graphId came back", bool(gid), created)

    # ── list_my_workflows now finds it ──────────────────────────────────────────
    listed = await call_tool("list_my_workflows", {})
    check("the new graph now shows up in list_my_workflows", any(w.get("graphId") == gid for w in listed.get("workflows", [])), listed)
    entry = next((w for w in listed.get("workflows", []) if w.get("graphId") == gid), {})
    check("listed entry reports draft status and version 1", entry.get("status") == "draft" and entry.get("currentVersion") == 1, entry)

    # ── get_my_workflow reads back exactly what was proposed ───────────────────
    fetched = await call_tool("get_my_workflow", {"graph_id": gid})
    check("get_my_workflow succeeds", fetched.get("status") == "success", fetched)
    fetched_node_ids = {n.get("nodeId") for n in fetched.get("nodes", [])}
    check("get_my_workflow returns the exact nodes that were proposed", fetched_node_ids == {"t1", "n1"}, fetched)
    check("get_my_workflow reports the current version", fetched.get("currentVersion") == 1, fetched)

    # ── get_my_workflow: ownership enforced -- a DIFFERENT principal is refused ─
    act_as(other_record)
    forbidden_read = await call_tool("get_my_workflow", {"graph_id": gid})
    check("a different principal cannot read someone else's workflow via get_my_workflow",
          forbidden_read.get("status") == "error", forbidden_read)
    check("the ownership refusal does not leak any node data", "nodes" not in forbidden_read, forbidden_read)

    # ── list_my_workflows: scoped -- the other principal doesn't see it either ──
    other_list = await call_tool("list_my_workflows", {})
    check("a different principal's list_my_workflows does not include someone else's graph",
          not any(w.get("graphId") == gid for w in other_list.get("workflows", [])), other_list)

    # ── propose_graph(graph_id=...): a DIFFERENT principal cannot edit it either ─
    forbidden_edit = await call_tool("propose_graph", {
        "graph_id": gid, "nodes": [TRIGGER, STEP_1], "edges": [EDGE_1],
    })
    check("a different principal's edit attempt is refused, not silently accepted",
          forbidden_edit.get("status") == "error", forbidden_edit)
    still_v1 = workflow_graph_store.get_graph(gid)
    check("the graph's version did NOT advance after the refused edit attempt",
          still_v1 is not None and still_v1.current_version == 1, still_v1)

    # ── propose_graph(graph_id=...): the REAL owner edits it -- adds a step ─────
    act_as(owner_record)
    step_2 = {"nodeId": "n2", "kind": "tool_call", "tool": "create_excel_report", "title": "Also build a workbook",
              "config": {"title": "Extra report", "tables": [{"name": "T", "rows": [{"a": 1}]}], "classification": ["INTERNAL"]}}
    edge_2 = {"edgeId": "e2", "sourceNodeId": "t1", "targetNodeId": "n2"}
    edited = await call_tool("propose_graph", {
        "graph_id": gid, "nodes": [TRIGGER, STEP_1, step_2], "edges": [EDGE_1, edge_2],
    })
    check("owner's edit succeeds", edited.get("status") == "success", edited)
    check("edit is marked as an edit (not a fresh create)", edited.get("edited") is True, edited)
    check("edit targets the SAME graphId -- no duplicate created", edited.get("graphId") == gid, edited)
    check("edit bumped the version number", edited.get("newVersion") == 2, edited)

    all_graphs = workflow_graph_store.list_graphs(owner=owner_record.name)
    check("editing did NOT create a second graph -- still exactly one for this owner",
          len(all_graphs) == 1, [g.graph_id for g in all_graphs])

    reread = await call_tool("get_my_workflow", {"graph_id": gid})
    reread_node_ids = {n.get("nodeId") for n in reread.get("nodes", [])}
    check("re-reading after the edit shows the NEW node included", reread_node_ids == {"t1", "n1", "n2"}, reread)
    check("re-reading after the edit reports version 2", reread.get("currentVersion") == 2, reread)

    # ── THE #1 WAY TO BREAK AN EDIT: omitting an existing node must actually drop it,
    #    proving the "full replace, not a diff" semantics really are what they claim ─
    dropped = await call_tool("propose_graph", {"graph_id": gid, "nodes": [TRIGGER, STEP_1], "edges": [EDGE_1]})
    check("an edit that omits n2 succeeds (full-replace, not merge)", dropped.get("status") == "success", dropped)
    reread2 = await call_tool("get_my_workflow", {"graph_id": gid})
    reread2_node_ids = {n.get("nodeId") for n in reread2.get("nodes", [])}
    check("the omitted node is actually gone -- confirms full-replace semantics", reread2_node_ids == {"t1", "n1"}, reread2)

    # ── Editing an ALREADY-PUBLISHED workflow must not disturb what's live ─────
    publish_resp = workflow_graph_store.publish_graph(gid, actor=owner_record.name)
    check("test setup: graph published for the next check", publish_resp is not None and publish_resp.status == "active", publish_resp)
    republish_edit = await call_tool("propose_graph", {
        "graph_id": gid, "nodes": [TRIGGER, STEP_1, step_2], "edges": [EDGE_1, edge_2],
    })
    check("editing an active workflow still succeeds", republish_edit.get("status") == "success", republish_edit)
    post_edit_graph = workflow_graph_store.get_graph(gid)
    check("the workflow is still active after the edit (unaffected)", post_edit_graph.status == "active", post_edit_graph)
    check("the PUBLISHED version pointer did NOT move -- what's live is untouched by the edit",
          post_edit_graph.published_version == publish_resp.published_version, post_edit_graph)
    check("but the DRAFT current_version did advance, ready for the human to review+republish",
          post_edit_graph.current_version == publish_resp.published_version + 1, post_edit_graph)

    # ── get_my_workflow / propose_graph(graph_id=...): an unknown id fails cleanly ─
    bogus = await call_tool("get_my_workflow", {"graph_id": "does-not-exist"})
    check("get_my_workflow on an unknown id fails with a clean error, not a crash", bogus.get("status") == "error", bogus)
    bogus_edit = await call_tool("propose_graph", {"graph_id": "does-not-exist", "nodes": [TRIGGER, STEP_1], "edges": [EDGE_1]})
    check("propose_graph edit on an unknown id fails clean, and does not fall back to creating a new graph",
          bogus_edit.get("status") == "error" and "graphId" not in bogus_edit, bogus_edit)


asyncio.run(main())
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
