"""Smoke test for the `loop` node -- run a span of steps once per row
(loopnodedesign.md, closing finalize_stage_1.md section 3.2b's for-each gap).

Part 1 (no server): the executor's mechanics against a stubbed govern/parse
pair, the same dependency-injection the paginate test uses. Covers the parts a
reader of the design would most doubt:
  - per-iteration step ids, so N rows leave N distinct step records
  - loop_item / loop_index binding resolution
  - a body node binding to ANOTHER body node resolving to THIS iteration's copy
    (the defect rev 1 of the design would have shipped: it scoped step ids but
    not binding resolution, so a two-node body silently dropped the argument)
  - both caps, cancellation, and both on_error modes
  - artifact ids from every row reaching the run record
  - a non-list input failing loudly rather than doing nothing

Part 2 (real validate_graph): the region rules -- dominance, single entry, no
escaping output, body kind and risk restrictions, and every budget cap -- each
rejected with its own stable check id.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "loop-node"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
os.environ["GOVERNANCE_STATE_DIR"] = str(TMP / "state")

import workflows  # noqa: E402
import workflow_graph_store as wgs  # noqa: E402
import workflow_graph_interpreter as interp  # noqa: E402
from workflow_graph_models import GraphEdge, GraphNode  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


# ── stub backend ──────────────────────────────────────────────────────────────

CALLS: list[dict] = []


def make_govern(fail_on_customer=None, fail_status="error"):
    # `customer_id` arrives as its own parameter, not inside `args` -- the
    # executor pops it out (same as it does for the paginate keys) before the
    # governed call, so the stub mirrors that shape.
    async def govern(tool, session_id, customer_id, args):
        CALLS.append({"tool": tool, "customer_id": customer_id, "args": dict(args)})
        if fail_on_customer is not None and customer_id == fail_on_customer:
            return {"status": fail_status, "message": f"backend said no to {fail_on_customer}"}
        return {
            "status": "ok",
            "artifactId": f"af_{customer_id}_{len(CALLS)}",
            "subject": f"draft for {customer_id}",
            "rows": [{"r": i} for i in range(10)],   # a big list, to exercise compaction
        }
    return govern


def parse(raw):
    return raw if isinstance(raw, dict) else {}


def loop_graph(*, body_ids, config=None, body_nodes=None):
    """A trigger -> loop -> body chain, wired the way both builder views wire it."""
    nodes = [
        GraphNode(node_id="trigger", kind="trigger"),
        GraphNode(node_id="n_loop", kind="loop", title="For each customer",
                  config={"body": list(body_ids), **(config or {})},
                  input_bindings={"input": {"source": "trigger", "path": "customers"}}),
    ]
    nodes.extend(body_nodes or [])
    edges = [GraphEdge(edge_id="e0", source_node_id="trigger", target_node_id="n_loop"),
             GraphEdge(edge_id="e1", source_node_id="n_loop", target_node_id=body_ids[0])]
    for i in range(len(body_ids) - 1):
        edges.append(GraphEdge(edge_id=f"eb{i}", source_node_id=body_ids[i], target_node_id=body_ids[i + 1]))
    return nodes, edges


def draft_node(node_id="n_draft", **bindings):
    return GraphNode(
        node_id=node_id, kind="tool_call", tool="create_email_draft",
        input_bindings={"customer_id": {"source": "loop_item", "path": "customerId"}, **bindings},
    )


async def run_loop(nodes, edges, inputs, *, govern=None, body_order=None):
    """Drive _execute_loop_node directly with a real WorkflowRun."""
    CALLS.clear()
    workflows.reload_for_tests()
    run = workflows.new_run("t_loop", "alice", inputs)
    nodes_by_id = {n.node_id: n for n in nodes}
    order = wgs.topological_node_ids(nodes, edges)
    loop = nodes_by_id["n_loop"]
    body = set((loop.config or {}).get("body") or [])
    resolved_order = body_order if body_order is not None else [nid for nid in order if nid in body]
    run, outcome = await interp._execute_loop_node(
        run, loop, session_id="s", owner="alice",
        govern=govern or make_govern(), parse=parse,
        nodes_by_id=nodes_by_id, body_order=resolved_order,
    )
    return run, outcome


def loop_step(run):
    return next(s for s in run.steps if s.step_id == "n_loop")


ROWS = [{"customerId": "C1"}, {"customerId": "C2"}, {"customerId": "C3"}]


# ── Part 1: executor mechanics ────────────────────────────────────────────────

async def test_basic_fanout():
    print("\n[1] one row in, one iteration out")
    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()])
    run, outcome = await run_loop(nodes, edges, {"customers": ROWS})

    out = loop_step(run).outputs
    check("the loop step completed", loop_step(run).status == "completed")
    check("one iteration per row", out["iterations"] == 3 and out["succeeded"] == 3, out)
    check("itemCount reports the input size", out["itemCount"] == 3)
    check("nothing was truncated", out["truncated"] is False and out["truncatedReason"] is None)
    check("the tool was called once per row", len(CALLS) == 3, len(CALLS))
    check("each call got ITS OWN row's value",
          [c["customer_id"] for c in CALLS] == ["C1", "C2", "C3"],
          [c["customer_id"] for c in CALLS])

    ids = [s.step_id for s in run.steps if s.step_id.startswith("n_draft")]
    check("each iteration recorded its own step", ids == ["n_draft#0", "n_draft#1", "n_draft#2"], ids)
    check("results carry one entry per row", len(out["results"]) == 3, out["results"])
    check("every row's artifact id was collected", len(out["artifactIds"]) == 3, out["artifactIds"])
    check("the owner arg is still injected, not bindable",
          all(c["args"].get("owner") == "alice" for c in CALLS))


async def test_loop_index_and_whole_item():
    print("\n[2] loop_index, and a scalar array's whole item")
    node = GraphNode(node_id="n_draft", kind="tool_call", tool="create_email_draft",
                     input_bindings={"customer_id": {"source": "loop_item"},
                                     "subject": {"source": "loop_index"}})
    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[node])
    await run_loop(nodes, edges, {"customers": ["C9", "C8"]})
    check("a pathless loop_item binds the whole row",
          [c["customer_id"] for c in CALLS] == ["C9", "C8"],
          [c["customer_id"] for c in CALLS])
    check("loop_index binds the 0-based position",
          [c["args"]["subject"] for c in CALLS] == [0, 1],
          [c["args"]["subject"] for c in CALLS])


async def test_body_to_body_binding():
    print("\n[3] a body node reading ANOTHER body node reads THIS row's copy")
    first = draft_node("n_first")
    # binds to the first body node's output -- must resolve to n_first#i, not n_first
    second = GraphNode(node_id="n_second", kind="tool_call", tool="create_email_draft",
                       input_bindings={"customer_id": {"source": "loop_item", "path": "customerId"},
                                       "subject": {"source": "node", "node_id": "n_first", "path": "subject"}})
    nodes, edges = loop_graph(body_ids=["n_first", "n_second"], body_nodes=[first, second])
    run, _ = await run_loop(nodes, edges, {"customers": ROWS})

    seconds = [c for c in CALLS if c["args"].get("subject")]
    check("the second node ran once per row", len(seconds) == 3, len(seconds))
    check("and saw its OWN iteration's upstream value",
          [c["args"]["subject"] for c in seconds] ==
          ["draft for C1", "draft for C2", "draft for C3"],
          [c["args"].get("subject") for c in seconds])
    check("both body nodes recorded per-iteration steps",
          {s.step_id for s in run.steps} >= {"n_first#0", "n_second#0", "n_first#2", "n_second#2"})


async def test_outer_binding_still_global():
    print("\n[4] a body node reading an OUTER node still reads it globally")
    workflows.reload_for_tests()
    run = workflows.new_run("t_loop", "alice", {"customers": ROWS})
    from workflow_models import WorkflowStep
    run = workflows.add_step(run, WorkflowStep(step_id="n_outer", type="tool_call", status="completed",
                                                outputs={"subject": "shared-subject"}))
    node = GraphNode(node_id="n_draft", kind="tool_call", tool="create_email_draft",
                     input_bindings={"customer_id": {"source": "loop_item", "path": "customerId"},
                                     "subject": {"source": "node", "node_id": "n_outer", "path": "subject"}})
    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[node])
    CALLS.clear()
    run, _ = await interp._execute_loop_node(
        run, {n.node_id: n for n in nodes}["n_loop"], session_id="s", owner="alice",
        govern=make_govern(), parse=parse,
        nodes_by_id={n.node_id: n for n in nodes}, body_order=["n_draft"],
    )
    check("every row saw the same outer value",
          [c["args"]["subject"] for c in CALLS] == ["shared-subject"] * 3,
          [c["args"].get("subject") for c in CALLS])


async def test_caps_and_cancel():
    print("\n[5] caps and cancellation stop loudly")
    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()],
                              config={"max_iterations": 2})
    run, _ = await run_loop(nodes, edges, {"customers": ROWS})
    out = loop_step(run).outputs
    check("max_iterations stops the walk", out["iterations"] == 2 and len(CALLS) == 2, out)
    check("and says so", out["truncated"] is True and out["truncatedReason"] == "max_iterations", out)
    check("itemCount still reports the FULL input, so the gap is visible", out["itemCount"] == 3)

    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()],
                              config={"max_duration_sec": 0.001})
    run, _ = await run_loop(nodes, edges, {"customers": ROWS})
    out = loop_step(run).outputs
    check("max_duration_sec also stops it", out["truncatedReason"] == "max_duration", out)

    # Cancel mid-flight: the stub cancels the run while row 0 is in the backend.
    workflows.reload_for_tests()
    run = workflows.new_run("t_loop", "alice", {"customers": ROWS})
    CALLS.clear()

    async def cancelling_govern(tool, session_id, customer_id, args):
        CALLS.append({"tool": tool, "customer_id": customer_id, "args": dict(args)})
        workflows.cancel_run(run.run_id, actor="admin", reason="stop")
        return {"status": "ok", "artifactId": f"af_{len(CALLS)}"}

    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()])
    run, _ = await interp._execute_loop_node(
        run, {n.node_id: n for n in nodes}["n_loop"], session_id="s", owner="alice",
        govern=cancelling_govern, parse=parse,
        nodes_by_id={n.node_id: n for n in nodes}, body_order=["n_draft"],
    )
    out = loop_step(run).outputs
    check("a cancel stops the fan-out after the in-flight row", len(CALLS) == 1, len(CALLS))
    check("and is reported as the reason", out["truncatedReason"] == "cancelled", out)


async def test_failure_modes():
    print("\n[6] on_error: fail (default) vs continue")
    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()])
    run, outcome = await run_loop(nodes, edges, {"customers": ROWS}, govern=make_govern(fail_on_customer="C2"))
    out = loop_step(run).outputs
    check("fail-fast stops at the bad row", outcome.get("failed") is True and len(CALLS) == 2, len(CALLS))
    check("the loop step is failed", loop_step(run).status == "failed")
    check("but the record says how far it got",
          out["succeeded"] == 1 and out["failed"] == 1 and out["iterations"] == 2, out)
    check("and names the row that broke", out["errors"][0]["index"] == 1, out["errors"])
    check("the artifact the FIRST row created is still recorded", len(out["artifactIds"]) == 1, out["artifactIds"])

    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()],
                              config={"on_error": "continue", "max_failures": 5})
    run, outcome = await run_loop(nodes, edges, {"customers": ROWS}, govern=make_govern(fail_on_customer="C2"))
    out = loop_step(run).outputs
    check("continue mode runs every row", len(CALLS) == 3, len(CALLS))
    check("and reports the split", out["succeeded"] == 2 and out["failed"] == 1, out)
    check("the loop step still completes", loop_step(run).status == "completed")
    check("results only carry the rows that worked", len(out["results"]) == 2, out["results"])

    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()],
                              config={"on_error": "continue", "max_failures": 0})
    run, _ = await run_loop(nodes, edges, {"customers": ROWS}, govern=make_govern(fail_on_customer="C2"))
    out = loop_step(run).outputs
    check("max_failures stops it early", out["truncatedReason"] == "max_failures", out)


async def test_bad_input_and_empty():
    print("\n[7] empty list completes; a non-list fails loudly")
    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()])
    run, outcome = await run_loop(nodes, edges, {"customers": []})
    out = loop_step(run).outputs
    check("an empty list is 0 iterations, not an error",
          outcome.get("failed") is False and out["iterations"] == 0, out)
    check("and calls nothing", len(CALLS) == 0)

    run, outcome = await run_loop(nodes, edges, {"customers": {"not": "a list"}})
    check("a non-list input FAILS rather than silently doing nothing", outcome.get("failed") is True)
    check("with a message naming the problem", "did not resolve to a list" in (loop_step(run).error or ""),
          loop_step(run).error)


async def test_compaction():
    print("\n[8] finished iterations are compacted out of the run record")
    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()])
    run, _ = await run_loop(nodes, edges, {"customers": ROWS})
    step = next(s for s in run.steps if s.step_id == "n_draft#0")
    check("a big list output was cut to a preview", len(step.outputs["rows"]) == 3, step.outputs["rows"])
    check("its true length is still reported", step.outputs["rowsTotalCount"] == 10, step.outputs)
    check("and the cut is flagged", step.outputs["rowsTruncated"] is True)
    check("scalars survive untouched", step.outputs["artifactId"].startswith("af_C1"), step.outputs)
    out = loop_step(run).outputs
    check("results were captured BEFORE compaction",
          out["results"][0]["subject"] == "draft for C1", out["results"][0])


# ── Part 2: validate_graph region rules ───────────────────────────────────────

class _Grant:
    all_tools = True

    def allows_tool(self, backend, tool):
        return True


def ids_of(checks):
    return sorted({c.check for c in checks})


def validate(nodes, edges):
    return wgs.validate_graph(nodes, edges, owner_grant=_Grant())


def test_validation():
    print("\n[9] validate_graph: the region rules and the budgets")

    nodes, edges = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()])
    check("a well-formed loop passes", validate(nodes, edges) == [], validate(nodes, edges))

    # Dominance: an edge from the trigger straight into the body bypasses the loop.
    bad = edges + [GraphEdge(edge_id="ex", source_node_id="trigger", target_node_id="n_draft")]
    check("a body reachable without the loop is rejected",
          "loop_body_not_dominated" in ids_of(validate(nodes, bad)), ids_of(validate(nodes, bad)))

    # Single entry.
    two = loop_graph(body_ids=["n_a", "n_b"], body_nodes=[draft_node("n_a"), draft_node("n_b")])
    extra = two[1] + [GraphEdge(edge_id="ee", source_node_id="n_loop", target_node_id="n_b")]
    check("two entry points into one body are rejected",
          "loop_body_multiple_entries" in ids_of(validate(two[0], extra)), ids_of(validate(two[0], extra)))

    # Output escaping the body.
    outside = GraphNode(node_id="n_after", kind="tool_call", tool="create_email_draft",
                        input_bindings={"customer_id": {"source": "node", "node_id": "n_draft", "path": "subject"}})
    esc_nodes = nodes + [outside]
    esc_edges = edges + [GraphEdge(edge_id="ea", source_node_id="n_loop", target_node_id="n_after")]
    check("consuming a per-row output from outside is rejected",
          "loop_body_output_escapes" in ids_of(validate(esc_nodes, esc_edges)), ids_of(validate(esc_nodes, esc_edges)))

    # Body kind + risk restrictions.
    gate = GraphNode(node_id="n_gate", kind="approval_gate")
    gn, ge = loop_graph(body_ids=["n_gate"], body_nodes=[gate])
    check("an approval gate inside a body is rejected (v1)",
          "loop_body_invalid_kind" in ids_of(validate(gn, ge)), ids_of(validate(gn, ge)))

    sender = GraphNode(node_id="n_send", kind="tool_call", tool="send_email_draft",
                       input_bindings={"customer_id": {"source": "loop_item", "path": "customerId"},
                                       "artifact_id": {"source": "loop_item", "path": "artifactId"}})
    sn, se = loop_graph(body_ids=["n_send"], body_nodes=[sender])
    got = ids_of(validate(sn, se))
    check("a send-risk tool inside a body is rejected", "loop_body_forbidden_risk" in got, got)

    exporter = GraphNode(node_id="n_xls", kind="tool_call", tool="create_excel_report",
                         config={"title": "t"}, input_bindings={"tables": {"source": "loop_item"}})
    xn, xe = loop_graph(body_ids=["n_xls"], body_nodes=[exporter])
    got = ids_of(validate(xn, xe))
    check("an EXPORT-risk tool inside a body is rejected too (it can pause)",
          "loop_body_forbidden_risk" in got, got)

    # loop_item outside a body.
    stray = GraphNode(node_id="n_stray", kind="tool_call", tool="create_email_draft",
                      input_bindings={"customer_id": {"source": "loop_item", "path": "customerId"}})
    sn2 = [GraphNode(node_id="trigger", kind="trigger"), stray]
    se2 = [GraphEdge(edge_id="e", source_node_id="trigger", target_node_id="n_stray")]
    check("a loop_item binding outside any body is rejected",
          "loop_item_outside_body" in ids_of(validate(sn2, se2)), ids_of(validate(sn2, se2)))

    # Budgets.
    cn, ce = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()], config={"max_iterations": 500})
    got = ids_of(validate(cn, ce))
    check("max_iterations over the ceiling is rejected", "loop_iteration_ceiling" in got, got)

    # 100 rows x 3 governed calls each = 300, over the 200-call per-run budget.
    cn, ce = loop_graph(body_ids=["n_a", "n_b", "n_c"],
                        body_nodes=[draft_node("n_a"), draft_node("n_b"), draft_node("n_c")],
                        config={"max_iterations": 100})
    got = ids_of(validate(cn, ce))
    check("the governed-call budget is enforced at author time", "loop_call_budget" in got, got)
    cn, ce = loop_graph(body_ids=["n_a", "n_b"], body_nodes=[draft_node("n_a"), draft_node("n_b")],
                        config={"max_iterations": 100})
    check("...and exactly at the budget is allowed", validate(cn, ce) == [], validate(cn, ce))

    ai = GraphNode(node_id="n_ai", kind="llm_transform", config={"kind": "draft_reply"},
                   input_bindings={"input_text": {"source": "loop_item", "path": "customerId"}})
    an, ae = loop_graph(body_ids=["n_ai"], body_nodes=[ai], config={"max_iterations": 25})
    got = ids_of(validate(an, ae))
    check("an AI step in the body caps iterations much lower", "loop_llm_budget" in got, got)
    an, ae = loop_graph(body_ids=["n_ai"], body_nodes=[ai], config={"max_iterations": 5})
    check("...and 5 is accepted", validate(an, ae) == [], validate(an, ae))

    # Config shape.
    bn, be = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()], config={"on_error": "shrug"})
    check("an unknown on_error is rejected", "invalid_loop_config" in ids_of(validate(bn, be)))
    bn, be = loop_graph(body_ids=["n_draft"], body_nodes=[draft_node()], config={"result_node": "nope"})
    check("a result_node outside the body is rejected", "invalid_loop_config" in ids_of(validate(bn, be)))

    # Reserved character, and a shared body node.
    rn = [GraphNode(node_id="trigger", kind="trigger"), GraphNode(node_id="bad#0", kind="tool_call", tool="create_email_draft")]
    re_ = [GraphEdge(edge_id="e", source_node_id="trigger", target_node_id="bad#0")]
    check("'#' in a node id is rejected (it builds iteration step ids)",
          "reserved_node_id_char" in ids_of(validate(rn, re_)), ids_of(validate(rn, re_)))

    shared = [
        GraphNode(node_id="trigger", kind="trigger"),
        GraphNode(node_id="n_loop", kind="loop", config={"body": ["n_draft"]},
                  input_bindings={"input": {"source": "trigger", "path": "c"}}),
        GraphNode(node_id="n_loop2", kind="loop", config={"body": ["n_draft"]},
                  input_bindings={"input": {"source": "trigger", "path": "c"}}),
        draft_node(),
    ]
    shared_edges = [
        GraphEdge(edge_id="e0", source_node_id="trigger", target_node_id="n_loop"),
        GraphEdge(edge_id="e1", source_node_id="n_loop", target_node_id="n_draft"),
        GraphEdge(edge_id="e2", source_node_id="trigger", target_node_id="n_loop2"),
    ]
    check("a node owned by two loops is rejected",
          "loop_body_shared" in ids_of(validate(shared, shared_edges)), ids_of(validate(shared, shared_edges)))

    check("'loop' is a known node kind now", "loop" in wgs.VALID_NODE_KINDS)


async def main():
    await test_basic_fanout()
    await test_loop_index_and_whole_item()
    await test_body_to_body_binding()
    await test_outer_binding_still_global()
    await test_caps_and_cancel()
    await test_failure_modes()
    await test_bad_input_and_empty()
    await test_compaction()
    test_validation()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
