"""Smoke test for the workflow copilot's two new tools:
  - update_workflow_plan (RAM-only scratchpad, workflow_scratchpad.py)
  - propose_graph (writes a real DRAFT into workflow_graph_store, same
    validation a hand-built graph goes through)
Plus the exclude mechanism that keeps both WORKFLOW-ONLY (Home chat must
never see them), mirroring orchestrator.WORKFLOW_ONLY_TOOLS.
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
TMP = ROOT / "_smoke" / ".tmp" / "workflow-copilot-tools"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "cop_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "cop_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-copilot-tools-secret",
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
check("app.py imports cleanly (no circular-import regression)", True)

import orchestrator  # noqa: E402
import request_context as ctx  # noqa: E402
import workflow_scratchpad  # noqa: E402
from mcp_server import mcp  # noqa: E402
from policy.resolve import resolve as resolve_grant  # noqa: E402
from store import get_store  # noqa: E402
from store.models import ConsumerRecord  # noqa: E402
from auth.passwords import hash_password  # noqa: E402

store = get_store()
record = ConsumerRecord(
    consumer_id="user:copilot_tester", name="copilot_tester", key_hash="", status="active",
    role="user", type="user", categories=["office"],
    login_password_hash=hash_password("tester_password"),
)
store.upsert_consumer(record)
ctx.consumer_ctx.set(record.name)
ctx.consumer_record_ctx.set(record)
ctx.ip_ctx.set("127.0.0.1")


async def call_tool(name, args):
    call_args = {"session_id": "copilot-tools-test", **args}  # args' own session_id (if any) wins
    result = await mcp.call_tool(name, call_args)
    content = result[0] if isinstance(result, tuple) else result
    for part in content or []:
        if getattr(part, "text", None) is not None:
            return json.loads(part.text)
    return {}


async def main():
    # ── The exclude mechanism: Home must never see the two workflow-only tools ─
    grant = resolve_grant(record, store.get_category, store.get_department)
    all_tools = await mcp.list_tools()
    home_specs = orchestrator.build_tool_specs(all_tools, grant, exclude=orchestrator.WORKFLOW_ONLY_TOOLS)
    workflow_specs = orchestrator.build_tool_specs(all_tools, grant, exclude=frozenset())
    home_names = {s["function"]["name"] for s in home_specs}
    workflow_names = {s["function"]["name"] for s in workflow_specs}
    check("Home's tool list excludes update_workflow_plan", "update_workflow_plan" not in home_names, home_names)
    check("Home's tool list excludes propose_graph", "propose_graph" not in home_names, home_names)
    check("Home's tool list still includes submit_workflow_request (shared, harmless)",
          "submit_workflow_request" in home_names, home_names)
    check("Workflow copilot's tool list includes update_workflow_plan", "update_workflow_plan" in workflow_names)
    check("Workflow copilot's tool list includes propose_graph", "propose_graph" in workflow_names)

    # ── The scratchpad: partial updates merge, full plan echoes back ───────────
    session_id = "workflow-chat:copilot_tester"
    workflow_scratchpad.clear_plan(session_id)

    p1 = await call_tool("update_workflow_plan", {"session_id": session_id,
                                                    "fields_discovered": [{"tool": "get_customer_order_summary", "field": "status", "values": ["A", "I", "C"]}]})
    check("first update_workflow_plan call succeeds", p1.get("status") == "success", p1)
    check("plan echoes back the fields just recorded", p1.get("plan", {}).get("fieldsDiscovered"), p1)
    check("plan's other slots start empty", p1.get("plan", {}).get("modulesChosen") == [], p1)

    p2 = await call_tool("update_workflow_plan", {"session_id": session_id, "modules_chosen": ["get_customer_order_summary", "create_excel_report"]})
    check("second call succeeds", p2.get("status") == "success", p2)
    check("second call's plan STILL has the fields from the first call (merge, not overwrite)",
          p2.get("plan", {}).get("fieldsDiscovered") == p1["plan"]["fieldsDiscovered"], (p1, p2))
    check("second call's plan has the newly-added modules", p2.get("plan", {}).get("modulesChosen") == ["get_customer_order_summary", "create_excel_report"], p2)

    p3 = await call_tool("update_workflow_plan", {"session_id": session_id})
    # updatedAt legitimately bumps on every touch, including a pure read (keeps
    # an actively-used scratchpad's idle timer alive) -- compare content only.
    p2_content = {k: v for k, v in p2.get("plan", {}).items() if k != "updatedAt"}
    p3_content = {k: v for k, v in p3.get("plan", {}).items() if k != "updatedAt"}
    check("a no-args call just re-reads the current plan's content, doesn't clear it", p3_content == p2_content, (p2, p3))

    other_session_plan = await call_tool("update_workflow_plan", {"session_id": "workflow-chat:someone_else"})
    check("a DIFFERENT session_id gets its own empty plan, not cross-talk from this one",
          other_session_plan.get("plan", {}).get("fieldsDiscovered") == [], other_session_plan)

    # ── propose_graph: a VALID graph becomes a real, reviewable draft ──────────
    valid = await call_tool("propose_graph", {
        "session_id": session_id,
        "display_name": "Copilot-proposed test workflow",
        "nodes": [
            {"nodeId": "t1", "kind": "trigger"},
            {"nodeId": "n1", "kind": "tool_call", "tool": "create_pdf_packet", "title": "Build packet",
             "config": {"title": "Test", "sections": [{"heading": "H", "bullets": ["a"]}], "classification": ["INTERNAL"]}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
    })
    check("valid graph proposal succeeds", valid.get("status") == "success", valid)
    proposed_gid = valid.get("graphId")
    check("a real graphId came back", bool(proposed_gid), valid)
    check("the proposed graph is a DRAFT, not published", valid.get("graphStatus") == "draft", valid)

    import workflow_graph_store
    stored = workflow_graph_store.get_graph(proposed_gid)
    check("the draft is actually retrievable from the real graph store (not just echoed)", stored is not None, proposed_gid)
    check("the stored draft's owner is the real principal, not something the model could spoof",
          stored is not None and stored.owner == record.name, stored)
    check("the stored draft has the exact node the model proposed",
          stored is not None and any(n.tool == "create_pdf_packet" for n in stored.version_record().nodes), stored)

    # ── propose_graph: an INVALID graph is rejected with a real blocker, not silently accepted ─
    invalid = await call_tool("propose_graph", {
        "session_id": session_id,
        "display_name": "Missing trigger",
        "nodes": [{"nodeId": "n1", "kind": "tool_call", "tool": "create_pdf_packet", "config": {}}],
        "edges": [],
    })
    check("a graph with no trigger node is rejected", invalid.get("status") == "error", invalid)
    check("the error message explains why (mentions 'trigger')", "trigger" in (invalid.get("message") or "").lower(), invalid)
    check("no graph was created for the rejected proposal", "graphId" not in invalid, invalid)

    # ── propose_graph: a send-risk tool with no approval_gate is rejected too ──
    send_no_gate = await call_tool("propose_graph", {
        "session_id": session_id,
        "display_name": "Send without gate",
        "nodes": [
            {"nodeId": "t1", "kind": "trigger"},
            {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft", "config": {"to": ["a@example.com"], "subject": "s", "body_markdown": "b"}},
            {"nodeId": "n2", "kind": "tool_call", "tool": "send_email_draft", "config": {}},
        ],
        "edges": [
            {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"},
            {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"},
        ],
    })
    check("a send-risk node without an approval gate is rejected, even from the copilot",
          send_no_gate.get("status") == "error" and "approval gate" in (send_no_gate.get("message") or "").lower(),
          send_no_gate)

    # ── propose_graph: a hallucinated/misspelled node kind is rejected loudly at
    #    build time, instead of being silently skipped forever at run time (the
    #    interpreter's kind dispatch has no else/default branch) ─────────────
    bad_kind = await call_tool("propose_graph", {
        "session_id": session_id,
        "display_name": "Bad kind",
        "nodes": [
            {"nodeId": "t1", "kind": "trigger"},
            {"nodeId": "n1", "kind": "tool_calls", "tool": "create_pdf_packet", "config": {"title": "x", "sections": [], "classification": ["INTERNAL"]}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
    })
    check("a node with an unrecognized kind (typo'd 'tool_calls') is rejected, not silently accepted",
          bad_kind.get("status") == "error", bad_kind)
    check("the error message names the bad kind", "tool_calls" in (bad_kind.get("message") or ""), bad_kind)
    check("no graph was created for the bad-kind proposal", "graphId" not in bad_kind, bad_kind)

    # ── propose_graph: the 'filter' kind (a valid 5th kind, easy to forget when
    #    only 4 are spelled out literally in a docstring) is genuinely accepted ─
    with_filter = await call_tool("propose_graph", {
        "session_id": session_id,
        "display_name": "Includes a filter node",
        "nodes": [
            {"nodeId": "t1", "kind": "trigger", "config": {"inputs": [{"name": "rows", "label": "Rows"}]}},
            {"nodeId": "n1", "kind": "filter", "input_bindings": {"input": {"source": "trigger", "path": "rows"}},
             "config": {"conditions": {"all": [{"field": "id", "op": "eq", "value": "A"}]}}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
    })
    check("a graph using the 'filter' node kind is accepted from the copilot", with_filter.get("status") == "success", with_filter)


asyncio.run(main())
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
