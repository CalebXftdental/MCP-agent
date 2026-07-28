"""User-buildable workflow graphs: CRUD, versioning, publish, validation."""
from __future__ import annotations

from policy import manifest
from policy.resolve import resolve as resolve_grant
from starlette.responses import JSONResponse
from store import get_store
import audit
import orchestrator
import workflow_graph_store
import workflows

from mcp_server import mcp
from .deps import _session, _unauthorized


def _graph_node_from_wire(d: dict) -> dict:
    return {
        "node_id": d.get("node_id") or d.get("nodeId") or "",
        "kind": d.get("kind") or "",
        "title": d.get("title") or "",
        "tool": d.get("tool") or "",
        "config": d.get("config") or {},
        "input_bindings": d.get("input_bindings") or d.get("inputBindings") or {},
        "position": d.get("position") or {},
    }


def _graph_edge_from_wire(d: dict) -> dict:
    return {
        "edge_id": d.get("edge_id") or d.get("edgeId") or "",
        "source_node_id": d.get("source_node_id") or d.get("sourceNodeId") or "",
        "target_node_id": d.get("target_node_id") or d.get("targetNodeId") or "",
    }


async def _workflow_graphs(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
        records = workflow_graph_store.list_graphs(owner=owner)
        return JSONResponse({"graphs": [r.public_dict() for r in records]})
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json()
        new_graph = workflow_graph_store.create_graph(
            display_name=str(body.get("display_name") or body.get("displayName") or "My Workflow"),
            description=str(body.get("description") or ""),
            owner_record=record, get_category=store.get_category, get_department=store.get_department,
            nodes=[_graph_node_from_wire(n) for n in (body.get("nodes") or [])],
            edges=[_graph_edge_from_wire(e) for e in (body.get("edges") or [])],
            created_by=claims["name"], notes=str(body.get("notes") or ""),
            graph_id=str(body.get("graph_id") or body.get("graphId") or ""),
            reserved_ids=set(workflows.TEMPLATES.keys()),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="create_workflow_graph", target=new_graph.graph_id)
    return JSONResponse(new_graph.public_dict(include_nodes=True), status_code=201)


def _require_graph_owner(claims, gid: str):
    """Return (graph, None) or (None, error_response)."""
    graph = workflow_graph_store.get_graph(gid)
    if graph is None:
        return None, JSONResponse({"error": "not found"}, status_code=404)
    if graph.owner != claims["name"] and claims.get("role") != "admin":
        return None, _unauthorized(is_admin=True)
    return graph, None


async def _workflow_graph_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    return JSONResponse(graph.public_dict(include_nodes=True))


async def _workflow_graph_versions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None:
        return JSONResponse({"error": "account not found"}, status_code=403)
    try:
        body = await request.json()
        updated = workflow_graph_store.add_graph_version(
            graph.graph_id,
            nodes=[_graph_node_from_wire(n) for n in (body.get("nodes") or [])],
            edges=[_graph_edge_from_wire(e) for e in (body.get("edges") or [])],
            owner_record=record, get_category=store.get_category, get_department=store.get_department,
            created_by=claims["name"], notes=str(body.get("notes") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="version_workflow_graph", target=graph.graph_id, detail=f"v{updated.current_version}")
    return JSONResponse(updated.public_dict(include_nodes=True), status_code=201)


async def _workflow_graph_publish(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    version = body.get("version")
    try:
        updated = workflow_graph_store.publish_graph(graph.graph_id, version=int(version) if version is not None else None, actor=claims["name"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="publish_workflow_graph", target=updated.graph_id, detail=f"v{updated.published_version}")
    return JSONResponse(updated.public_dict())


async def _workflow_graph_validate(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None:
        return JSONResponse({"error": "account not found"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    if body.get("nodes") is not None or body.get("edges") is not None:
        nodes = [_graph_node_from_wire(n) for n in (body.get("nodes") or [])]
        edges = [_graph_edge_from_wire(e) for e in (body.get("edges") or [])]
    else:
        version = graph.version_record()
        nodes = [_graph_node_from_wire(n.public_dict()) for n in (version.nodes if version else [])]
        edges = [_graph_edge_from_wire(e.public_dict()) for e in (version.edges if version else [])]
    owner_grant = resolve_grant(record, store.get_category, store.get_department)
    blockers = workflow_graph_store.validate_graph(nodes, edges, owner_grant=owner_grant)
    return JSONResponse({"ready": not blockers, "blockers": blockers, "warnings": []})


async def _workflow_graph_catalog(request):
    """Self-service tool palette for the graph builder -- any active user, filtered
    to THEIR own grant (drives the greyed-out/locked palette state; not itself a
    security check -- _govern's live decide() is what actually enforces access)."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None:
        return JSONResponse({"error": "account not found"}, status_code=403)
    grant = resolve_grant(record, store.get_category, store.get_department)
    tools = await mcp.list_tools()
    specs = orchestrator.build_tool_specs(tools, None)
    out = []
    for spec in specs:
        fn = spec["function"]
        canonical = manifest.canonical(fn["name"]) or fn["name"]
        policy = manifest.get(canonical)
        granted = grant.all_tools if grant is not None else True
        if not granted and policy is not None:
            granted = grant.allows_tool(policy.backend, canonical)
        out.append({
            "name": fn["name"],
            "canonical": canonical,
            "description": fn["description"],
            "riskLevel": policy.risk if policy else None,
            "approvalRequired": bool(policy.approval_required) if policy else False,
            "backend": policy.backend if policy else None,
            "granted": granted,
            "parameters": fn["parameters"],
            "outputFields": sorted((policy.fields if policy else {}).keys()),
        })
    return JSONResponse({"tools": out})
