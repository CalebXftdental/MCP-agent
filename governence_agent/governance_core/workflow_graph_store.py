"""Durable store for user-buildable workflow graphs ("My Workflow").

File-backed, in-memory-cached, atomic-tmp-rename -- the same pattern already
used by template_store.py, approval_store.py, and gateway/workflows.py. The
one thing this store adds beyond a plain CRUD registry is validate_graph():
the deterministic, build-time gate that keeps a user-assembled graph safe
BEFORE it's ever runnable (the live PDP decide() call in gateway/app.py's
_govern() remains the actual run-time security boundary regardless -- this is
a UX guardrail, not a replacement for it).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path

import store_concurrency
from policy import manifest
from policy.categories import CATEGORIES
from policy.resolve import EffectiveGrant, resolve as resolve_grant
from workflow_graph_models import GraphCheck, GraphEdge, GraphNode, WorkflowGraphDefinition, WorkflowGraphVersion

_GRAPHS: dict[str, WorkflowGraphDefinition] = {}
_LOADED = False

# Node ids ending in this suffix are reserved for the interpreter's own synthetic
# export-approval steps (gateway/workflow_graph_interpreter.py) -- a user-authored
# node can never collide with one.
RESERVED_NODE_ID_SUFFIX = "__export_approval"

# An llm_transform node's input_text may only ever be bound to something that has
# already passed through _govern's redaction (tool_call/llm_transform outputs) or
# is raw user-supplied trigger input -- never anything else.
_LLM_TRANSFORM_ALLOWED_SOURCE_KINDS = {"tool_call", "llm_transform"}

# The only node kinds the interpreter (workflow_graph_interpreter.py::_interpret)
# knows how to execute -- its dispatch is an if/elif chain with NO else/default
# branch, so a node whose kind isn't one of these isn't rejected at run time, it
# is silently SKIPPED: no step, no output, no error, and anything bound to its
# output resolves to nothing with no indication why. That failure mode is exactly
# the kind of thing that must never reach a saved graph -- hence the check in
# validate_graph below, at BUILD time, where a clear blocker can still stop it.
VALID_NODE_KINDS = {"trigger", "tool_call", "approval_gate", "llm_transform", "filter", "loop", "join"}

# ── join node ──────────────────────────────────────────────────────────────────
#
# Merges two already-fetched arrays by a shared key (e.g. a filter's `matched`
# customers enriched with a bulk lookup tool's rows) -- a generic, reusable
# alternative to a `loop` calling a per-record tool once per row, for exactly the
# case where the second dataset can already be fetched in ONE bulk call (a
# `where: {"key": {"in": [...]}}`-style query) rather than one call per row.
# No body/subgraph concept like `loop` -- it's a single self-contained node, same
# shape as `filter`, just with two inputs instead of one.
JOIN_ON_MISSING_VALUES = {"keep", "drop"}

# ── loop node (see loopnodedesign.md) ─────────────────────────────────────────
#
# A loop runs a span of nodes once per row of an array. Its `body` nodes stay
# REAL nodes with real edges -- the loop node just owns them -- which is what
# lets every check in validate_graph keep working unchanged and fail closed. The
# region rules enforced below are what make that ownership well defined:
# dominance (the only way into a body is through its loop), single entry, no
# output escaping the body, and no node owned by two loops.
ITERATION_SEPARATOR = "#"

# v1 body restrictions. `approval_gate`, nested `loop` and `filter` are excluded
# because each needs semantics that do not exist yet (resume-mid-loop, nested
# scopes, per-row tables). SEND is excluded for the obvious reason; EXPORT is
# excluded because it has its OWN implicit pause path -- the broad-export
# approval in workflow_graph_interpreter -- which v1 equally cannot resume from.
LOOP_BODY_KINDS = {"tool_call", "llm_transform"}

LOOP_DEFAULT_MAX_ITERATIONS = 25
LOOP_MAX_ITERATIONS_CEILING = 100
LOOP_DEFAULT_MAX_DURATION_SEC = 300.0
# max_iterations x tool_call nodes in the body. Every one is a real governed
# call, so this makes a published graph's blast radius knowable BEFORE it runs
# -- closing finalize_stage_1.md's open per-run budget question as a build-time
# blocker rather than a run-time surprise.
LOOP_MAX_GOVERNED_CALLS = 200
# A body containing an llm_transform is bounded far tighter: with USE_LOCAL_LLM
# every iteration is an inference on one shared server with a handful of slots
# (see concurrency_and_scale.md), so 25 rows is minutes of GPU, not seconds.
LOOP_MAX_ITERATIONS_WITH_LLM = 5

# A filter node's condition ops -- pure, deterministic, no library/network call.
# Single source of truth: the interpreter (gateway/workflow_graph_interpreter.py)
# imports this same set rather than duplicating it, so a new op can never be
# accepted by validate_graph but silently unhandled at run time (or vice versa).
FILTER_OPS = {
    "eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "not_in",
    # Compare a date-string field against "now minus N days" -- deterministic per
    # run (real wall-clock time), not re-derived by a model. This is what lets a
    # raw field like lastOrderDate drive a "days since" rule with no separate
    # precomputed field needed.
    "older_than_days", "newer_than_days",
}

# Best-effort, cosmetic-only tool -> output-type map for template-dict projection
# (gateway/workflows.py's outputTypes field). Not authoritative for anything.
_TOOL_OUTPUT_TYPES = {
    "create_excel_report": "xlsx",
    "create_powerpoint_deck": "pptx",
    "create_word_report": "docx",
    "create_pdf_packet": "pdf",
    "create_email_draft": "email_draft",
    "draft_calendar_invite": "calendar_invite",
}


def _state_root() -> Path:
    configured = os.getenv("GOVERNANCE_STATE_DIR")
    if configured:
        return Path(configured).resolve()
    artifact_dir = os.getenv("GOVERNANCE_ARTIFACT_DIR")
    if artifact_dir:
        return (Path(artifact_dir).resolve().parent / "state").resolve()
    if Path("/home/data").exists():
        return Path("/home/data/governance-state").resolve()
    return Path("/tmp/governance-state").resolve()


def _store_file() -> Path:
    configured = os.getenv("GOVERNANCE_WORKFLOW_GRAPH_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "workflow-graphs.json"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (value or "graph").strip().lower()).strip("_")
    return slug or "graph"


# ── dict <-> dataclass (snake_case internal shape, same convention as template_store) ──

def _node_to_dict(n: GraphNode) -> dict:
    return {
        "node_id": n.node_id, "kind": n.kind, "title": n.title, "tool": n.tool,
        "config": dict(n.config), "input_bindings": dict(n.input_bindings), "position": dict(n.position),
    }


def _node_from_dict(d: dict) -> GraphNode:
    return GraphNode(
        node_id=d["node_id"], kind=d.get("kind", ""), title=d.get("title", ""), tool=d.get("tool", ""),
        config=dict(d.get("config") or {}), input_bindings=dict(d.get("input_bindings") or {}),
        position=dict(d.get("position") or {}),
    )


def _edge_to_dict(e: GraphEdge) -> dict:
    return {"edge_id": e.edge_id, "source_node_id": e.source_node_id, "target_node_id": e.target_node_id}


def _edge_from_dict(d: dict) -> GraphEdge:
    return GraphEdge(edge_id=d["edge_id"], source_node_id=d["source_node_id"], target_node_id=d["target_node_id"])


def _version_to_dict(v: WorkflowGraphVersion) -> dict:
    return {
        "version": v.version, "created_by": v.created_by, "created_at": v.created_at,
        "nodes": [_node_to_dict(n) for n in v.nodes], "edges": [_edge_to_dict(e) for e in v.edges],
        "notes": v.notes,
    }


def _version_from_dict(d: dict) -> WorkflowGraphVersion:
    return WorkflowGraphVersion(
        version=int(d.get("version") or 1), created_by=d.get("created_by", ""), created_at=float(d.get("created_at") or 0.0),
        nodes=[_node_from_dict(n) for n in d.get("nodes", [])], edges=[_edge_from_dict(e) for e in d.get("edges", [])],
        notes=d.get("notes", ""),
    )


def _graph_to_dict(g: WorkflowGraphDefinition) -> dict:
    return {
        "graph_id": g.graph_id, "display_name": g.display_name, "description": g.description, "owner": g.owner,
        "status": g.status, "published_version": g.published_version, "current_version": g.current_version,
        "versions": [_version_to_dict(v) for v in g.versions],
        "created_at": g.created_at, "updated_at": g.updated_at,
    }


def _graph_from_dict(d: dict) -> WorkflowGraphDefinition:
    return WorkflowGraphDefinition(
        graph_id=d["graph_id"], display_name=d.get("display_name", ""), description=d.get("description", ""),
        owner=d.get("owner", ""), status=d.get("status", "draft"),
        published_version=int(d.get("published_version") or 0), current_version=int(d.get("current_version") or 1),
        versions=[_version_from_dict(v) for v in d.get("versions", [])],
        created_at=float(d.get("created_at") or 0.0), updated_at=float(d.get("updated_at") or 0.0),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _GRAPHS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("graphs", []):
                record = _graph_from_dict(item)
                _GRAPHS[record.graph_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _GRAPHS.clear()
    _LOADED = True


def _save() -> None:
    payload = {"version": 1, "graphs": [_graph_to_dict(g) for g in sorted(_GRAPHS.values(), key=lambda x: x.created_at)]}
    store_concurrency.atomic_write_text(_store_file(), json.dumps(payload, indent=2, ensure_ascii=False))


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _GRAPHS.clear()


# ── validation (expansion.md-style deny-by-default, checked at save AND re-checked at run) ──

def _build_adjacency(nodes: list[GraphNode], edges: list[GraphEdge]) -> tuple[dict[str, list[str]], dict[str, int]]:
    node_ids = {n.node_id for n in nodes}
    out_edges: dict[str, list[str]] = {nid: [] for nid in node_ids}
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    for e in edges:
        if e.source_node_id in node_ids and e.target_node_id in node_ids:
            out_edges[e.source_node_id].append(e.target_node_id)
            in_degree[e.target_node_id] += 1
    return out_edges, in_degree


def _topological_order(nodes: list[GraphNode], edges: list[GraphEdge]) -> list[str] | None:
    """Kahn's algorithm. Returns None if the graph has a cycle."""
    out_edges, in_degree = _build_adjacency(nodes, edges)
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    order: list[str] = []
    remaining = dict(in_degree)
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for target in out_edges.get(nid, []):
            remaining[target] -= 1
            if remaining[target] == 0:
                queue.append(target)
    return order if len(order) == len(nodes) else None


def _reachable_from(start: str, nodes: list[GraphNode], edges: list[GraphEdge], *,
                     exclude_kinds: set[str] = frozenset(), exclude_ids: set[str] = frozenset()) -> set[str]:
    """BFS reachable set from `start`, walking only through edges whose endpoints
    are NOT excluded. `exclude_kinds` severs paths through a whole node kind (the
    mandatory-gate-before-send check removes approval_gate nodes); `exclude_ids`
    severs specific nodes (the loop dominance check removes the loop node, so a
    body node that is STILL reachable proves there is a way in that bypasses its
    loop)."""
    excluded_ids = {n.node_id for n in nodes if n.kind in exclude_kinds} | set(exclude_ids)
    if start in excluded_ids:
        return set()
    out_edges, _ = _build_adjacency(
        [n for n in nodes if n.node_id not in excluded_ids],
        [e for e in edges if e.source_node_id not in excluded_ids and e.target_node_id not in excluded_ids],
    )
    seen = {start}
    queue = [start]
    while queue:
        nid = queue.pop(0)
        for nxt in out_edges.get(nid, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def topological_node_ids(nodes: list[GraphNode], edges: list[GraphEdge]) -> list[str] | None:
    """Public wrapper around the same Kahn's-algorithm ordering validate_graph uses,
    for the interpreter (gateway/workflow_graph_interpreter.py) to walk a graph in
    dependency order. None means the graph has a cycle (shouldn't happen for an
    already-validated/published graph, but the caller should still check)."""
    return _topological_order(nodes, edges)


def validate_graph(nodes: list[GraphNode] | list[dict], edges: list[GraphEdge] | list[dict], *, owner_grant: EffectiveGrant) -> list[GraphCheck]:
    """Deterministic build-time validation. Returns a list of structured
    GraphCheck results; [] means valid. Never raises -- callers decide
    whether to hard-reject (create_graph/add_graph_version do, by joining
    each check's `.message`) or just display them (the
    /workflow-graphs/{gid}/validate dry-run route, and the workflow
    copilot's scratchpad -- see gateway/app.py's propose_graph)."""
    nodes = [n if isinstance(n, GraphNode) else _node_from_dict(n) for n in nodes]
    edges = [e if isinstance(e, GraphEdge) else _edge_from_dict(e) for e in edges]
    checks: list[GraphCheck] = []

    def blocker(check: str, message: str, node_id: str | None = None) -> None:
        checks.append(GraphCheck(check=check, severity="error", message=message, node_id=node_id))

    node_ids = [n.node_id for n in nodes]
    if len(set(node_ids)) != len(node_ids):
        blocker("duplicate_node_ids", "duplicate node ids in graph")
        return checks  # everything below assumes unique ids
    for nid in node_ids:
        if nid.endswith(RESERVED_NODE_ID_SUFFIX):
            blocker("reserved_node_id", f"node id {nid!r} uses the reserved suffix {RESERVED_NODE_ID_SUFFIX!r}", nid)
        if ITERATION_SEPARATOR in nid:
            blocker(
                "reserved_node_id_char",
                f"node id {nid!r} contains the reserved character {ITERATION_SEPARATOR!r}, which the "
                "interpreter uses to build per-iteration step ids inside a loop body "
                f"({ITERATION_SEPARATOR}0, {ITERATION_SEPARATOR}1, ...)",
                nid,
            )

    for n in nodes:
        if n.kind not in VALID_NODE_KINDS:
            blocker(
                "unknown_node_kind",
                f"node {n.node_id!r} has unknown kind {n.kind!r} -- must be one of "
                f"{sorted(VALID_NODE_KINDS)} (any other value is silently skipped at run time, "
                "never executed, rather than failing loudly, so this is rejected here instead)",
                n.node_id,
            )

    triggers = [n for n in nodes if n.kind == "trigger"]
    if len(triggers) != 1:
        blocker("trigger_count", f"a graph must have exactly one trigger node (found {len(triggers)})")

    order = _topological_order(nodes, edges)
    if order is None:
        blocker("cycle", "graph contains a cycle -- it must be a directed acyclic graph")

    if triggers and order is not None:
        trigger = triggers[0]
        _, in_degree = _build_adjacency(nodes, edges)
        if in_degree.get(trigger.node_id, 0) != 0:
            blocker("trigger_has_incoming_edge", "the trigger node cannot have any incoming connections", trigger.node_id)
        reachable = _reachable_from(trigger.node_id, nodes, edges)
        orphans = [n.node_id for n in nodes if n.node_id != trigger.node_id and n.node_id not in reachable]
        if orphans:
            blocker("unreachable_node", f"nodes not reachable from the trigger: {', '.join(sorted(orphans))}")

    for n in nodes:
        if n.kind != "tool_call":
            continue
        policy = manifest.get(n.tool)
        if policy is None:
            blocker("unknown_tool", f"node {n.node_id!r} references an unknown tool {n.tool!r}", n.node_id)
            continue
        if not owner_grant.allows_tool(policy.backend, n.tool):
            blocker("tool_access_denied", f"node {n.node_id!r}: you do not have access to {n.tool!r} (backend {policy.backend!r})", n.node_id)

    # Every arg the tool's policy marks required_args must be set -- either a
    # literal in config, or wired to a binding (resolved at run time, so not
    # checkable here). An omission here would otherwise reach the backend tool
    # silently (e.g. create_excel_report falling back to a generic filename when
    # `title` is missing) instead of failing loudly at author time.
    for n in nodes:
        if n.kind != "tool_call":
            continue
        policy = manifest.get(n.tool)
        if policy is None or not policy.required_args:
            continue
        config = n.config or {}
        bindings = n.input_bindings or {}
        missing = [arg for arg in policy.required_args if arg not in bindings and not config.get(arg)]
        if missing:
            blocker(
                "missing_required_args",
                f"node {n.node_id!r}: {n.tool!r} is missing required field(s) {missing} -- "
                "set a value or bind it to another node's output",
                n.node_id,
            )

    # A paginate:true tool_call node loops calling the same tool, bumping page,
    # while its own JSON response reports hasMore (see
    # gateway/workflow_graph_interpreter.py's _exhaust_tool_call). No allowlist
    # of which tools may set this -- it's a safe no-op on a tool with no
    # hasMore-shaped output -- but max_pages/max_duration_sec, if given, must be
    # sane, since a bad value here would otherwise silently degrade into "loop
    # once" or "loop until the interpreter's own default cap" at run time
    # instead of failing at author time.
    for n in nodes:
        if n.kind != "tool_call" or not (n.config or {}).get("paginate"):
            continue
        config = n.config or {}
        max_pages = config.get("max_pages")
        if max_pages is not None and (not isinstance(max_pages, (int, float)) or isinstance(max_pages, bool) or max_pages <= 0):
            blocker("invalid_paginate_config", f"node {n.node_id!r}: max_pages must be a positive number, got {max_pages!r}", n.node_id)
        max_duration_sec = config.get("max_duration_sec")
        if max_duration_sec is not None and (not isinstance(max_duration_sec, (int, float)) or isinstance(max_duration_sec, bool) or max_duration_sec <= 0):
            blocker("invalid_paginate_config", f"node {n.node_id!r}: max_duration_sec must be a positive number, got {max_duration_sec!r}", n.node_id)

    # Mandatory approval-gate before any send-risk tool_call node, on EVERY path.
    if triggers and order is not None:
        trigger_id = triggers[0].node_id
        reachable_without_gates = _reachable_from(trigger_id, nodes, edges, exclude_kinds={"approval_gate"})
        for n in nodes:
            if n.kind != "tool_call":
                continue
            policy = manifest.get(n.tool)
            if policy is not None and policy.risk == manifest.SEND and n.node_id in reachable_without_gates:
                blocker(
                    "send_risk_without_approval_gate",
                    f"node {n.node_id!r} calls a send-risk tool ({n.tool!r}) reachable without passing through an approval gate",
                    n.node_id,
                )

    by_id = {n.node_id: n for n in nodes}

    # ── loop nodes: the structured-region rules (loopnodedesign.md §3, §5) ────
    #
    # Body membership is ownership, not an exemption: body nodes keep their edges
    # and stay subject to every other check above, so reachability and the
    # mandatory send-gate check need no special case and cannot fail open. What
    # must be proved here is that the ownership is unambiguous.
    loops = [n for n in nodes if n.kind == "loop"]
    body_owner: dict[str, str] = {}
    for loop in loops:
        config = loop.config or {}
        body = config.get("body")
        if not isinstance(body, list) or not body:
            blocker("loop_missing_body", f"node {loop.node_id!r}: a loop needs a non-empty `body` list of node ids", loop.node_id)
            continue
        for bid in body:
            if bid not in by_id:
                blocker("loop_body_unknown_node", f"node {loop.node_id!r}: body references unknown node {bid!r}", loop.node_id)
            elif bid == loop.node_id:
                blocker("loop_body_unknown_node", f"node {loop.node_id!r}: a loop cannot contain itself", loop.node_id)
            elif bid in body_owner:
                blocker(
                    "loop_body_shared",
                    f"node {bid!r} is in the body of both {body_owner[bid]!r} and {loop.node_id!r} -- "
                    "a node may belong to at most one loop",
                    loop.node_id,
                )
            else:
                body_owner[bid] = loop.node_id

    for loop in loops:
        config = loop.config or {}
        body = [b for b in (config.get("body") or []) if b in by_id and body_owner.get(b) == loop.node_id]
        if not body:
            continue

        if not isinstance(loop.input_bindings, dict) or "input" not in (loop.input_bindings or {}):
            blocker("loop_missing_input", f"node {loop.node_id!r}: a loop needs an `input` binding -- the list to iterate", loop.node_id)

        llm_in_body = 0
        tool_calls_in_body = 0
        for bid in body:
            b = by_id[bid]
            if b.kind not in LOOP_BODY_KINDS:
                blocker(
                    "loop_body_invalid_kind",
                    f"node {bid!r} (kind {b.kind!r}) cannot be inside a loop body -- v1 allows only "
                    f"{sorted(LOOP_BODY_KINDS)}. An approval gate, a nested loop or a filter inside a "
                    "body needs semantics that do not exist yet (pausing mid-iteration, nested item "
                    "scopes, per-row tables).",
                    bid,
                )
                continue
            if b.kind == "llm_transform":
                llm_in_body += 1
                continue
            tool_calls_in_body += 1
            policy = manifest.get(b.tool)
            if policy is None:
                continue
            if policy.risk == manifest.SEND:
                blocker(
                    "loop_body_forbidden_risk",
                    f"node {bid!r} calls a send-risk tool ({b.tool!r}) inside a loop body -- a fan-out of "
                    "sends needs one approval per row, which v1 cannot pause for",
                    bid,
                )
            elif policy.risk == manifest.EXPORT:
                blocker(
                    "loop_body_forbidden_risk",
                    f"node {bid!r} calls an export-risk tool ({b.tool!r}) inside a loop body -- an export "
                    "can trigger the broad-export approval and pause mid-iteration, which v1 cannot "
                    "resume from. Build the export once, after the loop, over its collected results.",
                    bid,
                )

        # Dominance: with the loop node removed, no body node may still be
        # reachable from the trigger -- otherwise there is a way into the body
        # that does not go through its loop, and "once per row" is meaningless.
        if triggers and order is not None:
            bypass = _reachable_from(triggers[0].node_id, nodes, edges, exclude_ids={loop.node_id})
            escaped = sorted(b for b in body if b in bypass)
            if escaped:
                blocker(
                    "loop_body_not_dominated",
                    f"node {loop.node_id!r}: body node(s) {escaped} are reachable without passing through "
                    "the loop -- every path into a body must go through its loop node",
                    loop.node_id,
                )

        # Single entry: exactly one body node is wired directly from the loop.
        entries = sorted({e.target_node_id for e in edges
                          if e.source_node_id == loop.node_id and e.target_node_id in body})
        if len(entries) != 1:
            blocker(
                "loop_body_multiple_entries",
                f"node {loop.node_id!r}: a loop body must have exactly one entry node wired from the loop "
                f"(found {len(entries)}: {entries})",
                loop.node_id,
            )

        # No escape: a per-iteration value has no single meaning outside the loop,
        # so only the loop's own aggregate output may be consumed downstream.
        for n in nodes:
            if n.node_id in body or n.node_id == loop.node_id:
                continue
            for arg, binding in (n.input_bindings or {}).items():
                if isinstance(binding, dict) and binding.get("source") == "node" and binding.get("node_id") in body:
                    blocker(
                        "loop_body_output_escapes",
                        f"node {n.node_id!r} binds {arg!r} to {binding.get('node_id')!r}, which runs once per "
                        f"iteration inside loop {loop.node_id!r} -- bind to the loop's own output "
                        "(results/artifactIds/...) instead",
                        n.node_id,
                    )

        result_node = config.get("result_node")
        if result_node is not None and result_node not in body:
            blocker(
                "invalid_loop_config",
                f"node {loop.node_id!r}: result_node {result_node!r} is not in this loop's body",
                loop.node_id,
            )

        def _positive(value) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0

        for key in ("max_iterations", "max_duration_sec"):
            value = config.get(key)
            if value is not None and not _positive(value):
                blocker("invalid_loop_config", f"node {loop.node_id!r}: {key} must be a positive number, got {value!r}", loop.node_id)
        on_error = config.get("on_error")
        if on_error is not None and on_error not in ("fail", "continue"):
            blocker("invalid_loop_config", f"node {loop.node_id!r}: on_error must be 'fail' or 'continue', got {on_error!r}", loop.node_id)
        max_failures = config.get("max_failures")
        if max_failures is not None and (not isinstance(max_failures, int) or isinstance(max_failures, bool) or max_failures < 0):
            blocker("invalid_loop_config", f"node {loop.node_id!r}: max_failures must be a non-negative integer, got {max_failures!r}", loop.node_id)

        raw_iterations = config.get("max_iterations")
        max_iterations = int(raw_iterations) if _positive(raw_iterations) else LOOP_DEFAULT_MAX_ITERATIONS
        if max_iterations > LOOP_MAX_ITERATIONS_CEILING:
            blocker(
                "loop_iteration_ceiling",
                f"node {loop.node_id!r}: max_iterations {max_iterations} exceeds the ceiling of "
                f"{LOOP_MAX_ITERATIONS_CEILING}. Runs execute inside one request against shared backends; "
                "raising this needs background execution first, not a config change.",
                loop.node_id,
            )
        if tool_calls_in_body and max_iterations * tool_calls_in_body > LOOP_MAX_GOVERNED_CALLS:
            blocker(
                "loop_call_budget",
                f"node {loop.node_id!r}: {max_iterations} iterations x {tool_calls_in_body} tool call(s) per row "
                f"= {max_iterations * tool_calls_in_body} governed calls, over the per-run budget of "
                f"{LOOP_MAX_GOVERNED_CALLS}. Narrow the input, or lower max_iterations.",
                loop.node_id,
            )
        if llm_in_body and max_iterations > LOOP_MAX_ITERATIONS_WITH_LLM:
            blocker(
                "loop_llm_budget",
                f"node {loop.node_id!r}: an AI step inside the body caps max_iterations at "
                f"{LOOP_MAX_ITERATIONS_WITH_LLM} (asked for {max_iterations}). Every row is a separate "
                "inference on one shared model server with only a few concurrent slots, so this is minutes "
                "of work per run. Prefer ONE AI step over the loop's collected results instead of one per row.",
                loop.node_id,
            )

    # loop_item / loop_index only mean something inside a loop body. Anywhere else
    # they resolve to nothing and the argument is silently dropped, which is the
    # exact class of failure this validator exists to stop at author time.
    for n in nodes:
        if n.node_id in body_owner:
            continue
        for arg, binding in (n.input_bindings or {}).items():
            if isinstance(binding, dict) and binding.get("source") in ("loop_item", "loop_index"):
                blocker(
                    "loop_item_outside_body",
                    f"node {n.node_id!r}: {arg!r} uses a {binding.get('source')!r} binding but this node is "
                    "not inside any loop's body",
                    n.node_id,
                )

    # llm_transform input_text may only bind to a tool_call/llm_transform node's output
    # (never approval_gate) -- trigger/literal sources are always fine.
    for n in nodes:
        if n.kind != "llm_transform":
            continue
        binding = n.input_bindings.get("input_text")
        if isinstance(binding, dict) and binding.get("source") == "node":
            source_node = by_id.get(binding.get("node_id"))
            if source_node is None:
                blocker("llm_transform_dangling_reference", f"node {n.node_id!r}: input_text is bound to an unknown node {binding.get('node_id')!r}", n.node_id)
            elif source_node.kind not in _LLM_TRANSFORM_ALLOWED_SOURCE_KINDS:
                blocker(
                    "llm_transform_invalid_input_source",
                    f"node {n.node_id!r}: input_text may only be bound to a tool_call or llm_transform node's output, not {source_node.kind!r}",
                    n.node_id,
                )

    # filter: `input` may only reference an existing node (same dangling-reference
    # shape as llm_transform's input_text above), and every condition-tree leaf's
    # `op` must be one both this validator and the interpreter recognize.
    def _filter_condition_ops(tree) -> list[str]:
        if not isinstance(tree, dict):
            return []
        if "all" in tree or "any" in tree:
            found: list[str] = []
            for child in tree.get("all") or tree.get("any") or []:
                found.extend(_filter_condition_ops(child))
            return found
        return [str(tree.get("op"))] if "op" in tree else []

    for n in nodes:
        if n.kind != "filter":
            continue
        binding = n.input_bindings.get("input")
        if isinstance(binding, dict) and binding.get("source") == "node":
            if by_id.get(binding.get("node_id")) is None:
                blocker("filter_dangling_reference", f"node {n.node_id!r}: input is bound to an unknown node {binding.get('node_id')!r}", n.node_id)
        bad_ops = sorted({op for op in _filter_condition_ops((n.config or {}).get("conditions") or {}) if op not in FILTER_OPS})
        if bad_ops:
            blocker("filter_unknown_op", f"node {n.node_id!r}: unknown filter condition op(s) {bad_ops}", n.node_id)

    # join: `left`/`right` may only reference an existing node (same
    # dangling-reference shape as filter's `input` above); left_key/right_key are
    # required non-empty field names; fields (if given) must be "*" or a list of
    # bare strings / {"from","as"} objects; on_missing (if given) must be a value
    # the interpreter actually recognizes.
    for n in nodes:
        if n.kind != "join":
            continue
        for side in ("left", "right"):
            binding = (n.input_bindings or {}).get(side)
            if isinstance(binding, dict) and binding.get("source") == "node":
                if by_id.get(binding.get("node_id")) is None:
                    blocker(
                        "join_dangling_reference",
                        f"node {n.node_id!r}: {side!r} is bound to an unknown node {binding.get('node_id')!r}",
                        n.node_id,
                    )
        config = n.config or {}
        if not str(config.get("left_key") or "").strip():
            blocker("join_missing_key", f"node {n.node_id!r}: left_key is required and must be a non-empty field name", n.node_id)
        if not str(config.get("right_key") or "").strip():
            blocker("join_missing_key", f"node {n.node_id!r}: right_key is required and must be a non-empty field name", n.node_id)
        on_missing = config.get("on_missing")
        if on_missing is not None and on_missing not in JOIN_ON_MISSING_VALUES:
            blocker(
                "join_invalid_config",
                f"node {n.node_id!r}: on_missing must be one of {sorted(JOIN_ON_MISSING_VALUES)}, got {on_missing!r}",
                n.node_id,
            )
        fields = config.get("fields")
        if fields is not None and fields != "*":
            if not isinstance(fields, list):
                blocker("join_invalid_config", f"node {n.node_id!r}: fields must be \"*\" or a list", n.node_id)
            else:
                for entry in fields:
                    if isinstance(entry, dict):
                        if not str(entry.get("from") or "").strip():
                            blocker("join_invalid_config", f"node {n.node_id!r}: a fields entry object needs a non-empty 'from' name", n.node_id)
                    elif not isinstance(entry, str) or not entry.strip():
                        blocker(
                            "join_invalid_config",
                            f"node {n.node_id!r}: each fields entry must be a non-empty string or a {{'from','as'}} object",
                            n.node_id,
                        )

    return checks


def _categories_for_tool(canonical_tool: str) -> set[str]:
    policy = manifest.get(canonical_tool)
    if policy is None:
        return set()
    return {c.id for c in CATEGORIES.values() if c.backend == policy.backend and (c.tools == "*" or canonical_tool in c.tools)}


def missing_tool_access(version: WorkflowGraphVersion | None, owner_grant: EffectiveGrant) -> list[str]:
    """Exact per-tool access check (same EffectiveGrant.allows_tool validate_graph
    uses), for preflight's blocker list -- NOT the coarse union-of-categories
    required_categories_for_version produces, which can list alternative categories
    that aren't all individually required (see gateway/app.py's _workflow_preflight_for)."""
    if version is None or owner_grant.all_tools:
        return []
    missing: set[str] = set()
    for n in version.nodes:
        if n.kind != "tool_call":
            continue
        policy = manifest.get(n.tool)
        if policy is not None and not owner_grant.allows_tool(policy.backend, n.tool):
            missing.add(f"access:{n.tool}")
    return sorted(missing)


def required_categories_for_version(version: WorkflowGraphVersion | None) -> list[str]:
    if version is None:
        return []
    cats: set[str] = set()
    for n in version.nodes:
        if n.kind == "tool_call":
            cats |= _categories_for_tool(n.tool)
    return sorted(cats)


def output_types_for_version(version: WorkflowGraphVersion | None) -> list[str]:
    if version is None:
        return []
    types = {_TOOL_OUTPUT_TYPES[n.tool] for n in version.nodes if n.kind == "tool_call" and n.tool in _TOOL_OUTPUT_TYPES}
    return sorted(types)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_graph(*, display_name: str, description: str = "", owner_record, get_category, get_department=None,
                  nodes: list[dict], edges: list[dict], created_by: str, notes: str = "",
                  graph_id: str = "", reserved_ids: set[str] | None = None) -> WorkflowGraphDefinition:
    _load()
    owner_grant = resolve_grant(owner_record, get_category, get_department)
    checks = validate_graph(nodes, edges, owner_grant=owner_grant)
    if checks:
        raise ValueError("; ".join(c.message for c in checks))
    reserved = reserved_ids or set()
    now = time.time()
    if graph_id:
        if graph_id in reserved:
            raise ValueError(f"graph_id {graph_id!r} is reserved for a built-in workflow template")
        if graph_id in _GRAPHS:
            raise ValueError(f"graph_id {graph_id!r} already exists")
        gid = graph_id
    else:
        base = _slug(display_name or "graph")
        gid = base
        while gid in _GRAPHS or gid in reserved:
            gid = f"{base}_{uuid.uuid4().hex[:6]}"
    version = WorkflowGraphVersion(
        version=1, created_by=created_by, created_at=now,
        nodes=[n if isinstance(n, GraphNode) else _node_from_dict(n) for n in nodes],
        edges=[e if isinstance(e, GraphEdge) else _edge_from_dict(e) for e in edges],
        notes=notes,
    )
    record = WorkflowGraphDefinition(
        graph_id=gid, display_name=display_name or gid, description=description, owner=owner_record.name,
        status="draft", published_version=0, current_version=1, versions=[version],
        created_at=now, updated_at=now,
    )
    _GRAPHS[gid] = record
    _save()
    return record


def add_graph_version(graph_id: str, *, nodes: list[dict], edges: list[dict], owner_record, get_category,
                       get_department=None, created_by: str, notes: str = "") -> WorkflowGraphDefinition:
    _load()
    existing = _GRAPHS.get(graph_id)
    if existing is None:
        raise ValueError(f"graph {graph_id!r} not found")
    owner_grant = resolve_grant(owner_record, get_category, get_department)
    checks = validate_graph(nodes, edges, owner_grant=owner_grant)
    if checks:
        raise ValueError("; ".join(c.message for c in checks))
    next_version = max([v.version for v in existing.versions] or [0]) + 1
    version = WorkflowGraphVersion(
        version=next_version, created_by=created_by, created_at=time.time(),
        nodes=[n if isinstance(n, GraphNode) else _node_from_dict(n) for n in nodes],
        edges=[e if isinstance(e, GraphEdge) else _edge_from_dict(e) for e in edges],
        notes=notes,
    )
    updated = replace(existing, versions=[*existing.versions, version], current_version=next_version, updated_at=time.time())
    _GRAPHS[graph_id] = updated
    _save()
    return updated


def publish_graph(graph_id: str, *, version: int | None = None, actor: str = "") -> WorkflowGraphDefinition | None:
    _load()
    existing = _GRAPHS.get(graph_id)
    if existing is None:
        return None
    target_version = version if version is not None else existing.current_version
    if existing.version_record(target_version) is None:
        raise ValueError(f"version {target_version} does not exist for graph {graph_id!r}")
    updated = replace(existing, status="active", published_version=target_version, updated_at=time.time())
    _GRAPHS[graph_id] = updated
    _save()
    return updated


def get_graph(graph_id: str) -> WorkflowGraphDefinition | None:
    _load()
    return _GRAPHS.get(graph_id)


def get_active_graph(graph_id: str) -> WorkflowGraphDefinition | None:
    """Only a currently-runnable graph -- status=="active" AND published at least
    once. Used at actual EXECUTION time (defense in depth; preflight already
    blocks a disabled template before this is reached)."""
    g = get_graph(graph_id)
    if g is None or g.status != "active" or g.published_version <= 0:
        return None
    return g


def get_published_graph(graph_id: str) -> WorkflowGraphDefinition | None:
    """A graph that has been published at least once, REGARDLESS of its current
    active/disabled status -- mirrors how a disabled hardcoded WorkflowTemplate
    is still resolvable via workflows.get_template (disabling is a status flag,
    not a delete). Used for template lookup/listing/admin toggling; get_active_graph
    (status=="active") is the stricter check used at actual run time."""
    g = get_graph(graph_id)
    if g is None or g.published_version <= 0:
        return None
    return g


def get_version(graph_id: str, version: int) -> WorkflowGraphVersion | None:
    g = get_graph(graph_id)
    return g.version_record(version) if g else None


def list_graphs(*, status: str | None = None, owner: str | None = None) -> list[WorkflowGraphDefinition]:
    _load()
    records = list(_GRAPHS.values())
    if status:
        records = [r for r in records if r.status == status]
    if owner:
        records = [r for r in records if r.owner == owner]
    records.sort(key=lambda r: r.display_name.lower())
    return records


def set_graph_status(graph_id: str, *, status: str, actor: str = "", reason: str = "") -> WorkflowGraphDefinition | None:
    _load()
    existing = _GRAPHS.get(graph_id)
    if existing is None or status not in ("active", "disabled", "draft"):
        return None
    updated = replace(existing, status=status, updated_at=time.time())
    _GRAPHS[graph_id] = updated
    _save()
    return updated


def delete_graph(graph_id: str) -> bool:
    """Hard delete -- unlike `set_graph_status`, there is no "deleted" status;
    the record is gone from `list_graphs`/`get_graph` immediately. Same
    no-cleanup-elsewhere trade the rest of this codebase already makes for
    deleting a category or department (see their own delete routes): an
    automation or past workflow run still holding this graph_id as its
    template_id keeps that id, but `workflows.get_template`/`get_published_graph`
    simply stop resolving it -- nothing reaches back to prune those references.
    Returns False for an unknown id (a silent no-op, matching
    delete_department/delete_category's own behavior) rather than raising."""
    _load()
    if graph_id not in _GRAPHS:
        return False
    del _GRAPHS[graph_id]
    _save()
    return True
