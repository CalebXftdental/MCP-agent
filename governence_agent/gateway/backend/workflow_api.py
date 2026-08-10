"""Workflow templates, preflight, runs, and the built-in run implementations."""
from __future__ import annotations

from dataclasses import replace
from policy import manifest
from policy.resolve import resolve as resolve_grant
from starlette.responses import JSONResponse
from store import get_store
import approval_store
import artifact_store
import audit
import mcp_clients
import json
import os
import request_context as ctx
import time
import workflow_graph_interpreter
import workflow_graph_store
import workflows

from govern import _govern
from .deps import (
    _effective_category_ids,
    _parse_tool_json,
    _require_admin_or_category,
    _session,
    _unauthorized,
)


def _missing_required_categories(grant, effective_categories: set[str], required_categories: list[str], get_category) -> list[str]:
    """Override-aware per-category check for the hardcoded workflow templates.

    A plain `required_set - effective_categories` diff (the old behavior) only
    sees categories added to `record.categories`/department -- it's blind to a
    self-service request approved via `overrides.grantTools` (session.py/
    admin_policy.py's current "selections" flow never touches `record.categories`
    at all), so a principal who was actually granted a category's tools through
    that path still showed up as missing it here, even though the exact same
    grant already satisfies graph-backed workflows' missing_tool_access() and
    direct tool calls' allows_tool(). Falls back to effective_categories (rather
    than treating it as satisfied) for backend-less "route marker" categories
    (files/workflow_runner/...), which have no tools for allows_tool() to check.
    """
    if grant.all_tools:
        return []
    missing = []
    for cid in required_categories:
        if cid in effective_categories:
            continue
        category = get_category(cid)
        if category is None or not category.backend:
            missing.append(cid)
            continue
        needed_tools = manifest.tools_for_backend(category.backend) if category.tools == "*" else category.tools
        if needed_tools and all(grant.allows_tool(category.backend, t) for t in needed_tools):
            continue
        missing.append(cid)
    return missing


async def _execute_workflow_for_record(record, template_id: str, body: dict, source: str = "manual") -> dict:
    template = workflows.get_template(template_id)
    if template is None:
        return {"error": "unknown workflow template", "status_code": 404, "status": "failed"}
    preflight = _workflow_preflight_for(record, template_id, body)
    if not preflight.get("ready"):
        blockers = list(preflight.get("blockers") or [])
        status_code = 400 if preflight.get("missingInputs") and len(blockers) == 1 else 403
        audit.log_policy_change(actor=record.name, action="block_workflow_preflight", target=template_id, detail="; ".join(blockers))
        return {"error": "; ".join(blockers) or "workflow is not ready", "status_code": status_code, "status": "failed", "preflight": preflight}
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    run = workflows.new_run(template_id, record.name, body)
    audit.log_policy_change(actor=record.name, action="start_workflow", target=run.run_id, detail=f"{template_id}; source={source}")
    runners = {
        "customer_360_report": _run_customer_360_workflow,
        "shipment_exception_report": _run_shipment_exception_workflow,
        "vendor_ap_summary": _run_vendor_ap_summary_workflow,
        "customer_email_draft": _run_customer_email_draft_workflow,
        "weekly_executive_brief": _run_weekly_executive_brief_workflow,
    }
    runner = runners.get(template_id)
    if runner is not None:
        result = await runner(run, body)
    else:
        graph = workflow_graph_store.get_active_graph(template_id)
        if graph is None:
            run = workflows.update_run(run, status="failed", error="workflow template is not executable yet")
            return {"error": "workflow template is not executable yet", **workflows.run_dict(run)}
        # Pin this run to the exact graph version it started with -- if the owner
        # publishes a newer version while this run is paused mid-graph, resume must
        # keep walking the version the run began against, not whatever is newest.
        run = workflows.update_run(run, inputs={**run.inputs, "__graph_version": graph.published_version})
        result = await workflow_graph_interpreter.run_graph(run, graph, body)
    if result.get("error") and result.get("status_code"):
        workflows.update_run(run, status="failed", error=result["error"])
        return {"error": result["error"], "runId": run.run_id, "status_code": result["status_code"]}
    action = "workflow_requires_approval" if result.get("status") == "approval_required" else "complete_workflow"
    audit.log_policy_change(actor=record.name, action=action, target=result.get("runId", run.run_id), detail=f"{template_id}; source={source}")
    return result


# Workflow catalog and execution API. Customer 360 is executable now; the other
# templates are visible as draft roadmap items while their step runners land.
async def _admin_workflow_health(request):
    claims, err = _require_admin_or_category(request, "workflow_admin")
    if err:
        return err
    try:
        stuck_after = float(request.query_params.get("stuck_after_sec") or request.query_params.get("stuckAfterSec") or 1800)
    except (TypeError, ValueError):
        stuck_after = 1800
    summary = workflows.workflow_health(stuck_after_sec=stuck_after)
    return JSONResponse(summary)


async def _workflows(request):
    if not _session(request):
        return _unauthorized()
    return JSONResponse({"workflows": workflows.list_templates()})


async def _workflow_template(request):
    if not _session(request):
        return _unauthorized()
    template = workflows.get_template(request.path_params["tid"])
    if template is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(template)


async def _admin_workflow_template_status(request):
    claims, err = _require_admin_or_category(request, "workflow_admin")
    if err:
        return err
    template_id = request.path_params["tid"]
    template = workflows.get_template(template_id)
    if template is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    action = request.path_params["action"]
    status = "disabled" if action == "disable" else "active" if action == "enable" else ""
    if not status:
        return JSONResponse({"error": "unknown action"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    updated = workflows.set_template_status(template_id, status=status, actor=claims["name"], reason=str(body.get("reason") or ""))
    audit.log_policy_change(actor=claims["name"], action=f"{action}_workflow_template", target=template_id, detail=str(body.get("reason") or ""))
    return JSONResponse(updated)


def _workflow_input_requirements(template_id: str) -> list[dict]:
    base = {
        "customer_360_report": [
            {"name": "customer_id", "label": "Customer ID", "sampleDefault": "SAMPLE100", "requiredWhenSampleFalse": True},
        ],
        "shipment_exception_report": [
            {"name": "customer_id", "label": "Customer ID", "sampleDefault": "SAMPLE100", "requiredWhenSampleFalse": True},
        ],
        "vendor_ap_summary": [
            {"name": "vendor_code", "label": "Vendor Code", "sampleDefault": "VEND100", "requiredWhenSampleFalse": True},
        ],
        "customer_email_draft": [
            {"name": "customer_id", "label": "Customer ID", "sampleDefault": "SAMPLE100", "requiredWhenSampleFalse": True},
            {"name": "recipient", "label": "Email recipient", "optional": True},
            {"name": "topic", "label": "Email topic", "optional": True},
        ],
        "weekly_executive_brief": [
            {"name": "start_date", "label": "Start date", "optional": True},
            {"name": "end_date", "label": "End date", "optional": True},
        ],
    }
    if template_id in base:
        return [dict(item) for item in base[template_id]]
    # Graph-backed workflow: the trigger node carries its own input schema in the
    # same {name,label,requiredWhenSampleFalse} shape (workflow_graph_models.py).
    # get_published_graph (not get_active_graph) so this still resolves for a
    # disabled graph -- preflight's own "workflow template is disabled" blocker is
    # what actually stops execution, matching the hardcoded templates' behavior.
    graph = workflow_graph_store.get_published_graph(template_id)
    if graph is None:
        return []
    version = graph.version_record(graph.published_version)
    trigger = next((n for n in (version.nodes if version else []) if n.kind == "trigger"), None)
    return [dict(item) for item in (trigger.config.get("inputs") or [])] if trigger else []


def _workflow_preflight_for(record, template_id: str, inputs: dict | None = None) -> dict:
    template = workflows.get_template(template_id)
    if template is None:
        return {
            "ready": False,
            "templateId": template_id,
            "error": "unknown workflow template",
            "blockers": ["unknown workflow template"],
            "warnings": [],
        }
    inputs = dict(inputs or {})
    sample = bool(inputs.get("sample", True))
    store = get_store()
    grant = resolve_grant(record, store.get_category, store.get_department)
    effective_categories = sorted(_effective_category_ids(store, record))
    required_categories = list(template.get("requiredCategories") or template.get("required_categories") or [])
    if workflows.is_graph_backed(template_id):
        # A graph's derived requiredCategories is a UNION across tool_call nodes,
        # and for a tool grantable by more than one category (e.g. send_email_draft
        # via EITHER email_send_internal or email_send_external) that union can list
        # alternatives that are not all individually required -- a flat AND-of-
        # categories check would wrongly block an owner who holds just one of them.
        # Check actual per-tool access instead (the same EffectiveGrant.allows_tool
        # check validate_graph already uses), which is exact rather than approximate.
        graph = workflow_graph_store.get_graph(template_id)
        version = graph.version_record(graph.published_version) if graph else None
        missing_categories = [] if grant.all_tools else workflow_graph_store.missing_tool_access(version, grant)
    else:
        missing_categories = sorted(_missing_required_categories(
            grant, set(effective_categories), required_categories, store.get_category,
        ))
    # Opt-in blanket gate (expansion.md §7.1 "workflow_runner"), off by default so
    # existing per-template requiredCategories keep working unchanged for
    # deployments that haven't explicitly turned this on.
    if (
        _workflow_runner_category_required()
        and not grant.all_tools
        and "workflow_runner" not in effective_categories
    ):
        missing_categories = sorted(set(missing_categories) | {"workflow_runner"})
    required_inputs = _workflow_input_requirements(template_id)
    missing_inputs = [
        item["name"] for item in required_inputs
        if item.get("requiredWhenSampleFalse") and not sample and not str(inputs.get(item["name"]) or "").strip()
    ]
    blockers: list[str] = []
    if template.get("status") != "active":
        blockers.append("workflow template is disabled")
    if missing_categories:
        blockers.append("missing required access categories: " + ", ".join(missing_categories))
    if missing_inputs:
        blockers.append("missing required inputs: " + ", ".join(missing_inputs))
    controls = store.get_controls() if hasattr(store, "get_controls") else {}
    paused_backends = set(controls.get("paused_backends") or [])
    connectors = []
    configured_backends = set()
    for category_id in required_categories:
        category = store.get_category(category_id)
        if category is None:
            connectors.append({"category": category_id, "backend": "", "configured": False, "error": "unknown category"})
            blockers.append(f"unknown required category: {category_id}")
            continue
        if category.backend in configured_backends:
            continue
        configured_backends.add(category.backend)
        try:
            url = mcp_clients.backend_url(category.backend)
            paused = category.backend in paused_backends
            connectors.append({"category": category_id, "backend": category.backend, "configured": True, "paused": paused, "url": url})
            if paused:
                blockers.append(f"backend is paused: {category.backend}")
        except Exception as exc:  # noqa: BLE001 - normalize configuration issues for UI
            connectors.append({"category": category_id, "backend": category.backend, "configured": False, "paused": category.backend in paused_backends, "error": str(exc)})
            blockers.append(f"backend is not configured: {category.backend}")
    warnings: list[str] = []
    if sample:
        warnings.append("sample mode is enabled; generated outputs are local test artifacts")
    if template_id == "customer_email_draft" and not str(inputs.get("recipient") or "").strip():
        warnings.append("no recipient supplied; draft will use manager@example.com")
    if template_id == "customer_email_draft" and bool(inputs.get("include_packet", True)):
        warnings.append("PDF packet will be generated with the email draft")
    approval_gates = []
    if template_id == "customer_email_draft":
        approval_gates.append({"id": "send_approval", "label": "Send approval", "enabled": bool(inputs.get("request_send_approval"))})
    if template_id == "weekly_executive_brief":
        approval_gates.append({"id": "review_approval", "label": "Review approval", "enabled": bool(inputs.get("request_approval"))})
    return {
        "ready": not blockers,
        "template": template,
        "templateId": template_id,
        "requiredCategories": required_categories,
        "effectiveCategories": effective_categories,
        "legacyAllowAll": bool(grant.all_tools),
        "pausedBackends": sorted(paused_backends & {c.get("backend") for c in connectors if c.get("backend")}),
        "missingCategories": missing_categories,
        "requiredInputs": required_inputs,
        "missingInputs": missing_inputs,
        "approvalGates": approval_gates,
        "connectors": connectors,
        "blockers": blockers,
        "warnings": warnings,
    }


async def _workflow_preflight(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.method == "POST" and request.headers.get("content-length") else {}
    except Exception:
        body = {}
    result = _workflow_preflight_for(record, request.path_params["tid"], body.get("inputs") if isinstance(body.get("inputs"), dict) else body)
    status = 404 if result.get("error") == "unknown workflow template" else 200
    audit.log_policy_change(actor=claims["name"], action="preflight_workflow", target=request.path_params["tid"], detail="ready" if result.get("ready") else "; ".join(result.get("blockers") or []))
    return JSONResponse(result, status_code=status)


async def _workflow_suggestions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    message = str(body.get("message") or body.get("prompt") or "").strip()
    try:
        limit = int(body.get("limit") or 4)
    except (TypeError, ValueError):
        limit = 4
    suggestions = workflows.workflow_suggestions(message, limit=limit)
    return JSONResponse({"suggestions": suggestions})


async def _workflow_suggestion_launch(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    template_id = str(body.get("template_id") or body.get("templateId") or "").strip()
    inputs = body.get("inputs") or {}
    if not isinstance(inputs, dict):
        return JSONResponse({"error": "inputs must be an object"}, status_code=400)
    ctx.ip_ctx.set(ctx.client_ip(request))
    result = await _execute_workflow_for_record(record, template_id, inputs, source="assistant_suggestion")
    if result.get("error") and result.get("status_code"):
        return JSONResponse({"error": result["error"], "runId": result.get("runId")}, status_code=result["status_code"])
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result, status_code=201)


def _workflow_run_timeline(run: dict) -> list[dict]:
    run_id = run.get("runId", "")
    events: list[dict] = []
    for step in run.get("steps") or []:
        events.append({
            "kind": "step",
            "ts": run.get("updatedAt") or run.get("createdAt"),
            "status": step.get("status"),
            "title": step.get("title") or step.get("stepId"),
            "tool": step.get("tool") or "",
            "detail": step.get("error") or "",
            "step": step,
        })
    session_id = "workflow:" + run_id
    for record in audit.recent(1000):
        if record.get("session_id") == session_id:
            events.append({
                "kind": "audit",
                "ts": record.get("ts"),
                "status": record.get("status"),
                "title": record.get("tool"),
                "tool": record.get("tool"),
                "detail": record.get("detail") or record.get("args") or "",
                "audit": record,
            })
        elif record.get("type") == "policy_change" and run_id and str(record.get("tool") or "").endswith(" " + run_id):
            events.append({
                "kind": "policy",
                "ts": record.get("ts"),
                "status": record.get("status"),
                "title": record.get("tool"),
                "tool": record.get("tool"),
                "detail": record.get("detail") or "",
                "audit": record,
            })
    for artifact_id in run.get("artifactIds") or []:
        artifact = artifact_store.get_artifact(artifact_id)
        events.append({
            "kind": "artifact",
            "ts": artifact.created_at if artifact else run.get("updatedAt"),
            "status": "ready" if artifact else "missing",
            "title": artifact.filename if artifact else artifact_id,
            "tool": "artifact",
            "detail": ", ".join(artifact.classification) if artifact else "artifact metadata not found",
            "artifact": artifact.public_dict() if artifact else {"artifactId": artifact_id},
        })
    for approval_id in run.get("approvalIds") or []:
        approval = approval_store.get_approval(approval_id)
        events.append({
            "kind": "approval",
            "ts": approval.created_at if approval else run.get("updatedAt"),
            "status": approval.status if approval else "missing",
            "title": approval.reason if approval else approval_id,
            "tool": "approval",
            "detail": approval.note if approval else "approval metadata not found",
            "approval": approval.public_dict() if approval else {"approvalId": approval_id},
        })
    events.sort(key=lambda e: (e.get("ts") or 0, e.get("kind") or ""))
    return events


async def _workflow_runs(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
    return JSONResponse({"runs": workflows.list_runs(requested_by=owner)})


async def _workflow_run_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    if request.query_params.get("timeline") == "1" or request.query_params.get("include") == "timeline":
        return JSONResponse({**run, "timeline": _workflow_run_timeline(run)})
    return JSONResponse(run)


async def _workflow_run_export_evidence(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    timeline = _workflow_run_timeline(run)
    source_artifact_ids = list(run.get("artifactIds") or [])
    source_artifacts = [a.public_dict() for a in (artifact_store.get_artifact(aid) for aid in source_artifact_ids) if a]
    classification = sorted({label for artifact in source_artifacts for label in (artifact.get("classification") or [])} or {manifest.INTERNAL})
    payload = {
        "kind": "workflow_evidence_packet",
        "schemaVersion": 1,
        "exportedAt": time.time(),
        "exportedBy": claims["name"],
        "run": run,
        "timeline": timeline,
        "artifacts": source_artifacts,
        "approvals": [
            approval.public_dict() for approval in (approval_store.get_approval(aid) for aid in (run.get("approvalIds") or [])) if approval
        ],
    }
    safe_tid = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in str(run.get("templateId") or "workflow"))
    filename = f"workflow-evidence-{safe_tid}-{run['runId']}.json"
    record = artifact_store.create_artifact(
        owner=run.get("requestedBy") or claims["name"],
        title=f"Workflow Evidence - {run.get('templateId')}",
        filename=filename,
        payload=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        artifact_type="json",
        mime_type="application/json",
        classification=classification,
        source_workflow_run_id=run["runId"],
        source_tool_calls=["workflow_evidence_export"],
        source_artifact_ids=source_artifact_ids,
        retention_days=365,
    )
    audit.log_policy_change(actor=claims["name"], action="export_workflow_evidence", target=run["runId"], detail=record.artifact_id)
    return JSONResponse({"artifact": record.public_dict(), "timelineEvents": len(timeline)}, status_code=201)


async def _workflow_run_cancel(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    updated = workflows.cancel_run(run["runId"], actor=claims["name"], reason=str(body.get("reason") or "cancelled"))
    audit.log_policy_change(actor=claims["name"], action="cancel_workflow", target=run["runId"], detail=str(body.get("reason") or ""))
    return JSONResponse(workflows.run_dict(updated))


async def _workflow_run_resume(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    if run.get("status") != "approval_required":
        return JSONResponse(run)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    approval_id = str(body.get("approval_id") or body.get("approvalId") or "").strip()
    approvals = [approval_store.get_approval(aid) for aid in (run.get("approvalIds") or [])]
    approvals = [a for a in approvals if a is not None]
    if approval_id:
        approvals = [a for a in approvals if a.approval_id == approval_id]
    else:
        # A run can accumulate MORE than one approval over its lifetime (a graph run
        # can pause at an explicit approval_gate node, then later at a generalized
        # export-risk auto-pause on a downstream node) -- run.approvalIds never prunes
        # old ones, so without this filter "find any approved approval" could match a
        # stale, already-resolved approval from an earlier gate instead of the one
        # actually blocking the run right now. Restrict to approvals still referenced
        # by a pending/running "approval" step. No-op for the 5 hardcoded workflows,
        # which never have more than one live approval per run.
        pending_ids = {
            s.get("outputs", {}).get("approvalId")
            for s in (run.get("steps") or [])
            if s.get("type") == "approval" and s.get("status") in ("pending", "running")
        }
        pending_ids.discard(None)
        if pending_ids:
            approvals = [a for a in approvals if a.approval_id in pending_ids]
    if not approvals:
        return JSONResponse({"error": "approval is required before resume"}, status_code=409)
    if any(a.status == "denied" for a in approvals):
        record = workflows.get_run_record(run["runId"])
        updated = workflows.update_run(record, status="failed", error="approval denied") if record else None
        return JSONResponse(workflows.run_dict(updated), status_code=409)
    approved = next((a for a in approvals if a.status == "approved"), None)
    if not approved:
        return JSONResponse({"error": "approval is still pending"}, status_code=409)
    if workflows.is_graph_backed(run["templateId"]):
        graph = workflow_graph_store.get_graph(run["templateId"])
        record = workflows.get_run_record(run["runId"])
        result = await workflow_graph_interpreter.resume_graph(record, graph, actor=claims["name"])
        audit.log_policy_change(actor=claims["name"], action="resume_workflow", target=run["runId"], detail=approved.approval_id)
        return JSONResponse(result)
    updated = workflows.resume_run(run["runId"], approval_id=approved.approval_id, actor=claims["name"])
    audit.log_policy_change(actor=claims["name"], action="resume_workflow", target=run["runId"], detail=approved.approval_id)
    return JSONResponse(workflows.run_dict(updated))


def _workflow_runner_category_required() -> bool:
    return (os.getenv("GOVERNANCE_REQUIRE_WORKFLOW_RUNNER_CATEGORY") or "").strip().lower() in ("1", "true", "yes", "on")


def _workflow_table_row_count(tables: list[dict] | None) -> int:
    return sum(len(t.get("rows") or []) for t in (tables or []))


def _workflow_broad_export_threshold(canonical_tool: str = "create_excel_report") -> int:
    """Row-count gate for one export-risk tool. An explicit
    GOVERNANCE_BROAD_EXPORT_APPROVAL_ROWS env var always wins (operator override,
    e.g. for tests/tuning); otherwise fall back to the tool's own declared
    ToolPolicy.max_rows_without_approval (manifest.py §7.3), then a hardcoded 100 --
    so the declarative per-tool field is load-bearing wherever an operator hasn't
    overridden it."""
    env_value = os.getenv("GOVERNANCE_BROAD_EXPORT_APPROVAL_ROWS")
    if env_value is not None:
        try:
            return max(1, int(env_value))
        except (TypeError, ValueError):
            pass
    policy = manifest.get(canonical_tool)
    if policy is not None and policy.max_rows_without_approval is not None:
        return max(1, int(policy.max_rows_without_approval))
    return 100


def _workflow_requires_broad_export_approval(
    tables: list[dict] | None, classification: list[str] | None, body: dict,
    canonical_tool: str = "create_excel_report",
) -> tuple[bool, int, int]:
    if body.get("request_approval") or body.get("requestApproval"):
        return False, _workflow_table_row_count(tables), _workflow_broad_export_threshold(canonical_tool)
    labels = set(classification or [])
    row_count = _workflow_table_row_count(tables)
    threshold = _workflow_broad_export_threshold(canonical_tool)
    sensitive = bool(labels & {manifest.PII, manifest.SENSITIVE})
    return sensitive and row_count >= threshold, row_count, threshold


def _workflow_pause_for_broad_export(run, *, artifact_ids: list[str], row_count: int, threshold: int, step_id: str = "request_broad_export_approval"):
    """`step_id` defaults to the original hardcoded id (unchanged for the 5 hardcoded
    workflows' 3 existing call sites); the graph interpreter passes a per-node id
    (`"<node_id>__export_approval"`) so multiple export-risk nodes in one graph run
    each get their own distinguishable pause step instead of colliding on one id."""
    run = workflows.add_step(run, workflows.WorkflowStep(
        step_id=step_id,
        type="approval",
        status="running",
        tool="approval_store.create_approval",
        title="Request broad export approval",
        inputs={"rowCount": row_count, "threshold": threshold},
    ))
    approval = approval_store.create_approval(
        requested_by=run.requested_by,
        reason=f"Review broad sensitive export before distribution ({row_count} rows, threshold {threshold})",
        risk_level="high",
        artifact_ids=artifact_ids,
        workflow_run_id=run.run_id,
    )
    audit.log_policy_change(actor=run.requested_by, action="request_broad_export_approval", target=approval.approval_id, detail=run.run_id)
    run = workflows.update_run(run, steps=[
        replace(s, status="pending", outputs={"approvalId": approval.approval_id, "status": approval.status, "rowCount": row_count, "threshold": threshold})
        if s.step_id == step_id else s for s in run.steps
    ])
    run = workflows.mark_approval_required(run.run_id, approval_id=approval.approval_id, artifact_ids=artifact_ids) or run
    return run, approval


async def _run_customer_360_workflow(run, body: dict) -> dict:
    customer_id = str(body.get("customer_id") or body.get("customerId") or "").strip()
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_customer_payload(customer_id or "SAMPLE100")
    else:
        if not customer_id:
            return {"error": "customer_id is required unless sample=true", "status_code": 400}
        session_id = "workflow:" + run.run_id
        payload = {"customer_id": customer_id}
        for step_id, title, canonical, args in [
            ("fetch_overview", "Fetch customer overview", "get_customer_overview", {}),
            ("fetch_order_summary", "Fetch order summary", "get_customer_order_summary", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
            }),
            ("fetch_orders", "Fetch recent orders", "get_customer_orders", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
                "page_size": int(body.get("page_size") or 25),
            }),
            ("fetch_shipments", "Fetch shipment status", "get_customer_shipment_status", {
                "max_orders": int(body.get("max_orders") or 5),
            }),
        ]:
            run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool=canonical, title=title))
            raw = await _govern(canonical, session_id, customer_id, args)
            result = _parse_tool_json(raw)
            run = workflows.complete_step(run, step_id, {"status": result.get("status"), "intent": result.get("intent")})
            key = {"fetch_overview": "overview", "fetch_order_summary": "order_summary", "fetch_orders": "orders", "fetch_shipments": "shipments"}[step_id]
            payload[key] = result

    tables = workflows.customer_360_tables(payload)
    sections = workflows.customer_360_sections(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.PII, manifest.SENSITIVE])
    artifacts = []
    for step_id, title, canonical, args in [
        ("build_excel", "Build XLSX appendix", "create_excel_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Customer 360 - {payload.get('customer_id', 'customer')}",
            "tables": tables,
            "classification": classification,
            "filename": f"customer-360-{payload.get('customer_id', 'customer')}.xlsx",
        }),
        ("build_deck", "Build PPTX deck", "create_powerpoint_deck", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Customer 360 - {payload.get('customer_id', 'customer')}",
            "sections": sections,
            "classification": classification,
            "filename": f"customer-360-{payload.get('customer_id', 'customer')}.pptx",
        }),
    ]:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="artifact", status="running", tool=canonical, title=title))
        raw = await _govern(canonical, "workflow:" + run.run_id, "", args)
        result = _parse_tool_json(raw)
        if result.get("status") != "success":
            run = workflows.fail_step(run, step_id, result.get("message") or result.get("errorCode") or "artifact generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, step_id, {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
        artifacts.append(result)
    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    needs_approval, row_count, threshold = _workflow_requires_broad_export_approval(tables, classification, body)
    if needs_approval:
        run, approval = _workflow_pause_for_broad_export(run, artifact_ids=artifact_ids, row_count=row_count, threshold=threshold)
        return {**workflows.run_dict(run), "artifacts": artifacts, "approval": approval.public_dict()}
    run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    return {**workflows.run_dict(run), "artifacts": artifacts}


async def _run_shipment_exception_workflow(run, body: dict) -> dict:
    customer_id = str(body.get("customer_id") or body.get("customerId") or "").strip()
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_shipment_payload(customer_id or "SAMPLE100")
    else:
        if not customer_id:
            return {"error": "customer_id is required unless sample=true", "status_code": 400}
        step_id = "fetch_shipments"
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool="get_customer_shipment_status", title="Fetch shipment status"))
        raw = await _govern("get_customer_shipment_status", "workflow:" + run.run_id, customer_id, {"max_orders": int(body.get("max_orders") or 20)})
        shipments = _parse_tool_json(raw)
        run = workflows.complete_step(run, step_id, {"status": shipments.get("status"), "intent": shipments.get("intent")})
        payload = {"customer_id": customer_id, "shipments": shipments}
    tables = workflows.shipment_exception_tables(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.SENSITIVE])
    args = {
        "owner": ctx.consumer_ctx.get() or run.requested_by,
        "title": f"Shipment Exceptions - {payload.get('customer_id', 'customer')}",
        "tables": tables,
        "classification": classification,
        "filename": f"shipment-exceptions-{payload.get('customer_id', 'customer')}.xlsx",
    }
    run = workflows.add_step(run, workflows.WorkflowStep(step_id="build_excel", type="artifact", status="running", tool="create_excel_report", title="Build shipment exception workbook"))
    result = _parse_tool_json(await _govern("create_excel_report", "workflow:" + run.run_id, "", args))
    if result.get("status") != "success":
        run = workflows.fail_step(run, "build_excel", result.get("message") or result.get("errorCode") or "artifact generation failed")
        return workflows.run_dict(run)
    run = workflows.complete_step(run, "build_excel", {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
    artifact_ids = [result.get("artifactId")]
    needs_approval, row_count, threshold = _workflow_requires_broad_export_approval(tables, classification, body)
    if needs_approval:
        run, approval = _workflow_pause_for_broad_export(run, artifact_ids=artifact_ids, row_count=row_count, threshold=threshold)
        return {**workflows.run_dict(run), "artifacts": [result], "approval": approval.public_dict()}
    run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    return {**workflows.run_dict(run), "artifacts": [result]}


async def _run_vendor_ap_summary_workflow(run, body: dict) -> dict:
    vendor_code = str(body.get("vendor_code") or body.get("vendorCode") or "").strip()
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_vendor_payload(vendor_code or "VEND100")
    else:
        if not vendor_code:
            return {"error": "vendor_code is required unless sample=true", "status_code": 400}
        session_id = "workflow:" + run.run_id
        payload = {"vendor_code": vendor_code}
        for step_id, title, canonical, args in [
            ("fetch_vendor", "Fetch vendor profile", "get_vendor_details", {"vendor_code": vendor_code}),
            ("fetch_ap", "Fetch AP invoices", "get_vendor_ap_invoices", {"vendor_code": vendor_code, "page": 1, "page_size": int(body.get("page_size") or 25)}),
        ]:
            run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool=canonical, title=title))
            result = _parse_tool_json(await _govern(canonical, session_id, "", args))
            run = workflows.complete_step(run, step_id, {"status": result.get("status"), "intent": result.get("intent")})
            payload["vendor" if step_id == "fetch_vendor" else "ap_invoices"] = result
    tables = workflows.vendor_ap_tables(payload)
    sections = workflows.vendor_ap_sections(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.SENSITIVE])
    artifacts = []
    for step_id, title, canonical, args in [
        ("build_excel", "Build AP workbook", "create_excel_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Vendor AP Summary - {payload.get('vendor_code', 'vendor')}",
            "tables": tables,
            "classification": classification,
            "filename": f"vendor-ap-summary-{payload.get('vendor_code', 'vendor')}.xlsx",
        }),
        ("build_doc", "Build AP narrative report", "create_word_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Vendor AP Summary - {payload.get('vendor_code', 'vendor')}",
            "sections": sections,
            "tables": tables,
            "classification": classification,
            "filename": f"vendor-ap-summary-{payload.get('vendor_code', 'vendor')}.docx",
        }),
    ]:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="artifact", status="running", tool=canonical, title=title))
        result = _parse_tool_json(await _govern(canonical, "workflow:" + run.run_id, "", args))
        if result.get("status") != "success":
            run = workflows.fail_step(run, step_id, result.get("message") or result.get("errorCode") or "artifact generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, step_id, {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
        artifacts.append(result)
    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    needs_approval, row_count, threshold = _workflow_requires_broad_export_approval(tables, classification, body)
    if needs_approval:
        run, approval = _workflow_pause_for_broad_export(run, artifact_ids=artifact_ids, row_count=row_count, threshold=threshold)
        return {**workflows.run_dict(run), "artifacts": artifacts, "approval": approval.public_dict()}
    run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    return {**workflows.run_dict(run), "artifacts": artifacts}


async def _run_customer_email_draft_workflow(run, body: dict) -> dict:
    customer_id = str(body.get("customer_id") or body.get("customerId") or "").strip()
    topic = str(body.get("topic") or body.get("email_topic") or body.get("emailTopic") or "account follow-up").strip()
    tone = str(body.get("tone") or "professional").strip() or "professional"
    recipient_raw = body.get("to") or body.get("recipients") or body.get("recipient") or body.get("recipient_email") or body.get("recipientEmail") or []
    if isinstance(recipient_raw, str):
        recipients = [x.strip() for x in recipient_raw.split(",") if x.strip()]
    else:
        recipients = [str(x).strip() for x in (recipient_raw or []) if str(x).strip()]
    if not recipients:
        recipients = ["manager@example.com"]

    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_customer_payload(customer_id or "SAMPLE100")
    else:
        if not customer_id:
            return {"error": "customer_id is required unless sample=true", "status_code": 400}
        session_id = "workflow:" + run.run_id
        payload = {"customer_id": customer_id}
        for step_id, title, canonical, args in [
            ("fetch_overview", "Fetch customer overview", "get_customer_overview", {}),
            ("fetch_order_summary", "Fetch order summary", "get_customer_order_summary", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
            }),
            ("fetch_orders", "Fetch recent orders", "get_customer_orders", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
                "page_size": int(body.get("page_size") or 10),
            }),
            ("fetch_shipments", "Fetch shipment status", "get_customer_shipment_status", {
                "max_orders": int(body.get("max_orders") or 5),
            }),
        ]:
            run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool=canonical, title=title))
            result = _parse_tool_json(await _govern(canonical, session_id, customer_id, args))
            run = workflows.complete_step(run, step_id, {"status": result.get("status"), "intent": result.get("intent")})
            payload[{"fetch_overview": "overview", "fetch_order_summary": "order_summary", "fetch_orders": "orders", "fetch_shipments": "shipments"}[step_id]] = result

    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.PII, manifest.SENSITIVE])
    artifacts = []
    attachment_ids: list[str] = []
    include_packet = body.get("include_packet", body.get("includePacket", True)) not in (False, "false", "0", 0)
    if include_packet:
        packet_args = {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Customer Email Packet - {payload.get('customer_id', 'customer')}",
            "sections": workflows.customer_360_sections(payload),
            "tables": workflows.customer_360_tables(payload),
            "classification": classification,
            "filename": f"customer-email-packet-{payload.get('customer_id', 'customer')}.pdf",
        }
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="build_packet", type="artifact", status="running", tool="create_pdf_packet", title="Build PDF review packet"))
        packet = _parse_tool_json(await _govern("create_pdf_packet", "workflow:" + run.run_id, "", packet_args))
        if packet.get("status") != "success":
            run = workflows.fail_step(run, "build_packet", packet.get("message") or packet.get("errorCode") or "packet generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, "build_packet", {"artifactId": packet.get("artifactId"), "filename": packet.get("filename")})
        artifacts.append(packet)
        if packet.get("artifactId"):
            attachment_ids.append(packet["artifactId"])

    subject = str(body.get("subject") or workflows.customer_email_subject(payload, topic))
    body_markdown = str(body.get("body_markdown") or body.get("bodyMarkdown") or workflows.customer_email_body(payload, topic, tone))
    draft_args = {
        "owner": ctx.consumer_ctx.get() or run.requested_by,
        "to": recipients,
        "cc": list(body.get("cc") or []),
        "subject": subject,
        "body_markdown": body_markdown,
        "attachment_artifact_ids": attachment_ids,
        "classification": classification,
    }
    run = workflows.add_step(run, workflows.WorkflowStep(step_id="create_draft", type="artifact", status="running", tool="create_email_draft", title="Create governed email draft"))
    draft = _parse_tool_json(await _govern("create_email_draft", "workflow:" + run.run_id, "", draft_args))
    if draft.get("status") != "success":
        run = workflows.fail_step(run, "create_draft", draft.get("message") or draft.get("errorCode") or "email draft generation failed")
        return workflows.run_dict(run)
    run = workflows.complete_step(run, "create_draft", {"artifactId": draft.get("artifactId"), "draftId": draft.get("draftId"), "filename": draft.get("filename")})
    artifacts.append(draft)

    approval = None
    if body.get("request_send_approval") or body.get("requestSendApproval") or body.get("send"):
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="request_send_approval", type="approval", status="running", tool="approval_store.create_approval", title="Request send approval"))
        approval = approval_store.create_approval(
            requested_by=run.requested_by,
            reason=str(body.get("approval_reason") or body.get("approvalReason") or f"Approve external send for {subject}"),
            risk_level="high" if any(x in classification for x in (manifest.PII, manifest.SENSITIVE)) else "medium",
            artifact_ids=[draft.get("artifactId") or draft.get("draftId")],
            workflow_run_id=run.run_id,
        )
        audit.log_policy_change(actor=run.requested_by, action="request_workflow_send_approval", target=approval.approval_id, detail=run.run_id)
        run = workflows.update_run(run, steps=[replace(s, status="pending", outputs={"approvalId": approval.approval_id, "status": approval.status}) if s.step_id == "request_send_approval" else s for s in run.steps])

    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    if approval:
        run = workflows.mark_approval_required(run.run_id, approval_id=approval.approval_id, artifact_ids=artifact_ids) or run
    else:
        run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    out = {**workflows.run_dict(run), "artifacts": artifacts}
    if approval:
        out["approval"] = approval.public_dict()
    return out


async def _run_weekly_executive_brief_workflow(run, body: dict) -> dict:
    start_date = str(body.get("start_date") or body.get("startDate") or "").strip()
    end_date = str(body.get("end_date") or body.get("endDate") or "").strip()
    limit = int(body.get("limit") or 5)
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_executive_payload(start_date or "2026-07-15", end_date or "2026-07-22")
    else:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="fetch_top_customers", type="tool_call", status="running", tool="get_top_customers_by_spend", title="Fetch top customers by spend"))
        top = _parse_tool_json(await _govern("get_top_customers_by_spend", "workflow:" + run.run_id, "", {
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        }))
        run = workflows.complete_step(run, "fetch_top_customers", {"status": top.get("status"), "intent": top.get("intent")})
        records = list(top.get("records") or top.get("customers") or [])
        total = sum(float(r.get("totalSpend") or r.get("spend") or r.get("grandTotal") or 0) for r in records)
        payload = {
            "start_date": start_date,
            "end_date": end_date,
            "top_customers": {"status": top.get("status"), "records": records, "totalSpend": round(total, 2)},
            "exceptions": [
                {"Area": "Analytics", "Signal": "Review concentration in top customer spend", "Owner": "Management", "Priority": "Medium"},
            ],
            "kpis": {"Revenue in brief": round(total, 2), "Top customer count": len(records), "Open executive risks": 1, "High priority risks": 0},
        }

    tables = workflows.executive_brief_tables(payload)
    sections = workflows.executive_brief_sections(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.SENSITIVE, manifest.PII])
    artifacts = []
    for step_id, title, canonical, args in [
        ("build_excel", "Build executive KPI workbook", "create_excel_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Weekly Executive Brief - {payload.get('end_date', 'week')}",
            "tables": tables,
            "classification": classification,
            "filename": f"weekly-executive-brief-{payload.get('end_date', 'week')}.xlsx",
        }),
        ("build_deck", "Build executive briefing deck", "create_powerpoint_deck", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Weekly Executive Brief - {payload.get('end_date', 'week')}",
            "sections": sections,
            "classification": classification,
            "filename": f"weekly-executive-brief-{payload.get('end_date', 'week')}.pptx",
        }),
        ("build_pdf", "Build executive PDF packet", "create_pdf_packet", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Weekly Executive Brief - {payload.get('end_date', 'week')}",
            "sections": sections,
            "tables": tables,
            "classification": classification,
            "filename": f"weekly-executive-brief-{payload.get('end_date', 'week')}.pdf",
        }),
    ]:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="artifact", status="running", tool=canonical, title=title))
        result = _parse_tool_json(await _govern(canonical, "workflow:" + run.run_id, "", args))
        if result.get("status") != "success":
            run = workflows.fail_step(run, step_id, result.get("message") or result.get("errorCode") or "artifact generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, step_id, {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
        artifacts.append(result)

    approval = None
    if body.get("request_approval") or body.get("requestApproval"):
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="request_review_approval", type="approval", status="running", tool="approval_store.create_approval", title="Request executive review approval"))
        approval = approval_store.create_approval(
            requested_by=run.requested_by,
            reason=str(body.get("approval_reason") or body.get("approvalReason") or "Review weekly executive brief before distribution"),
            risk_level="high",
            artifact_ids=[a.get("artifactId") for a in artifacts if a.get("artifactId")],
            workflow_run_id=run.run_id,
        )
        audit.log_policy_change(actor=run.requested_by, action="request_executive_brief_approval", target=approval.approval_id, detail=run.run_id)
        run = workflows.update_run(run, steps=[replace(s, status="pending", outputs={"approvalId": approval.approval_id, "status": approval.status}) if s.step_id == "request_review_approval" else s for s in run.steps])

    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    if approval:
        run = workflows.mark_approval_required(run.run_id, approval_id=approval.approval_id, artifact_ids=artifact_ids) or run
    else:
        run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    out = {**workflows.run_dict(run), "artifacts": artifacts}
    if approval:
        out["approval"] = approval.public_dict()
    return out


async def _workflow_run_start(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    template_id = request.path_params["tid"]
    template = workflows.get_template(template_id)
    if template is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    ctx.ip_ctx.set(ctx.client_ip(request))
    result = await _execute_workflow_for_record(record, template_id, body)
    if result.get("error") and result.get("status_code"):
        return JSONResponse({"error": result["error"], "runId": result.get("runId")}, status_code=result["status_code"])
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result, status_code=201)
