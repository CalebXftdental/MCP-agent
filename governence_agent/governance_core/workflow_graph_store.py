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

from policy import manifest
from policy.categories import CATEGORIES
from policy.resolve import EffectiveGrant, resolve as resolve_grant
from workflow_graph_models import GraphEdge, GraphNode, WorkflowGraphDefinition, WorkflowGraphVersion

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
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "graphs": [_graph_to_dict(g) for g in sorted(_GRAPHS.values(), key=lambda x: x.created_at)]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


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


def _reachable_from(start: str, nodes: list[GraphNode], edges: list[GraphEdge], *, exclude_kinds: set[str] = frozenset()) -> set[str]:
    """BFS reachable set from `start`, walking only through edges whose endpoints
    are NOT in `exclude_kinds` (used by the mandatory-gate-before-send check: removing
    approval_gate nodes severs any path that passes through one)."""
    excluded_ids = {n.node_id for n in nodes if n.kind in exclude_kinds}
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


def validate_graph(nodes: list[GraphNode] | list[dict], edges: list[GraphEdge] | list[dict], *, owner_grant: EffectiveGrant) -> list[str]:
    """Deterministic build-time validation. Returns a list of human-readable
    blocker strings; [] means valid. Never raises -- callers decide whether to
    hard-reject (create_graph/add_graph_version do) or just display blockers
    (the /workflow-graphs/{gid}/validate dry-run route does)."""
    nodes = [n if isinstance(n, GraphNode) else _node_from_dict(n) for n in nodes]
    edges = [e if isinstance(e, GraphEdge) else _edge_from_dict(e) for e in edges]
    blockers: list[str] = []

    node_ids = [n.node_id for n in nodes]
    if len(set(node_ids)) != len(node_ids):
        blockers.append("duplicate node ids in graph")
        return blockers  # everything below assumes unique ids
    for nid in node_ids:
        if nid.endswith(RESERVED_NODE_ID_SUFFIX):
            blockers.append(f"node id {nid!r} uses the reserved suffix {RESERVED_NODE_ID_SUFFIX!r}")

    triggers = [n for n in nodes if n.kind == "trigger"]
    if len(triggers) != 1:
        blockers.append(f"a graph must have exactly one trigger node (found {len(triggers)})")

    order = _topological_order(nodes, edges)
    if order is None:
        blockers.append("graph contains a cycle -- it must be a directed acyclic graph")

    if triggers and order is not None:
        trigger = triggers[0]
        _, in_degree = _build_adjacency(nodes, edges)
        if in_degree.get(trigger.node_id, 0) != 0:
            blockers.append("the trigger node cannot have any incoming connections")
        reachable = _reachable_from(trigger.node_id, nodes, edges)
        orphans = [n.node_id for n in nodes if n.node_id != trigger.node_id and n.node_id not in reachable]
        if orphans:
            blockers.append(f"nodes not reachable from the trigger: {', '.join(sorted(orphans))}")

    for n in nodes:
        if n.kind != "tool_call":
            continue
        policy = manifest.get(n.tool)
        if policy is None:
            blockers.append(f"node {n.node_id!r} references an unknown tool {n.tool!r}")
            continue
        if not owner_grant.allows_tool(policy.backend, n.tool):
            blockers.append(f"node {n.node_id!r}: you do not have access to {n.tool!r} (backend {policy.backend!r})")

    # Mandatory approval-gate before any send-risk tool_call node, on EVERY path.
    if triggers and order is not None:
        trigger_id = triggers[0].node_id
        reachable_without_gates = _reachable_from(trigger_id, nodes, edges, exclude_kinds={"approval_gate"})
        for n in nodes:
            if n.kind != "tool_call":
                continue
            policy = manifest.get(n.tool)
            if policy is not None and policy.risk == manifest.SEND and n.node_id in reachable_without_gates:
                blockers.append(f"node {n.node_id!r} calls a send-risk tool ({n.tool!r}) reachable without passing through an approval gate")

    # llm_transform input_text may only bind to a tool_call/llm_transform node's output
    # (never approval_gate) -- trigger/literal sources are always fine.
    by_id = {n.node_id: n for n in nodes}
    for n in nodes:
        if n.kind != "llm_transform":
            continue
        binding = n.input_bindings.get("input_text")
        if isinstance(binding, dict) and binding.get("source") == "node":
            source_node = by_id.get(binding.get("node_id"))
            if source_node is None:
                blockers.append(f"node {n.node_id!r}: input_text is bound to an unknown node {binding.get('node_id')!r}")
            elif source_node.kind not in _LLM_TRANSFORM_ALLOWED_SOURCE_KINDS:
                blockers.append(f"node {n.node_id!r}: input_text may only be bound to a tool_call or llm_transform node's output, not {source_node.kind!r}")

    return blockers


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
    blockers = validate_graph(nodes, edges, owner_grant=owner_grant)
    if blockers:
        raise ValueError("; ".join(blockers))
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
    blockers = validate_graph(nodes, edges, owner_grant=owner_grant)
    if blockers:
        raise ValueError("; ".join(blockers))
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
