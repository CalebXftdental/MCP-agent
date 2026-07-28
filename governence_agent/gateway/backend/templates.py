"""Reusable report/deck/document/email/calendar/workflow templates."""
from __future__ import annotations

from policy import manifest
from starlette.responses import JSONResponse
import audit
import template_store

from .deps import _session, _unauthorized


async def _templates(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        include_disabled = claims.get("role") == "admin" and request.query_params.get("include_disabled") == "1"
        records = template_store.list_templates(
            template_type=request.query_params.get("type"),
            include_disabled=include_disabled,
        )
        return JSONResponse({"templates": [r.public_dict(include_versions=False, include_content=False) for r in records]})
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json()
        record = template_store.create_template(
            display_name=str(body.get("display_name") or body.get("displayName") or "Template"),
            template_type=str(body.get("template_type") or body.get("templateType") or "generic"),
            content=body.get("content") or {},
            created_by=claims["name"],
            description=str(body.get("description") or ""),
            classification=list(body.get("classification") or [manifest.INTERNAL]),
            tags=list(body.get("tags") or []),
            allowed_workflow_ids=list(body.get("allowed_workflow_ids") or body.get("allowedWorkflowIds") or []),
            notes=str(body.get("notes") or ""),
            template_id=str(body.get("template_id") or body.get("templateId") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - validation to client
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="create_template", target=record.template_id, detail=record.template_type)
    return JSONResponse(record.public_dict(include_versions=True, include_content=True), status_code=201)


async def _template_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = template_store.get_template(request.path_params["tid"])
    if record is None or (record.status == "disabled" and claims.get("role") != "admin"):
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "GET":
        include_content = request.query_params.get("content") == "1" or claims.get("role") == "admin"
        return JSONResponse(record.public_dict(include_versions=True, include_content=include_content))
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json()
        updated = template_store.update_template(
            record.template_id,
            display_name=body.get("display_name", body.get("displayName", record.display_name)),
            description=body.get("description", record.description),
            classification=body.get("classification", record.classification),
            tags=body.get("tags", record.tags),
            allowed_workflow_ids=body.get("allowed_workflow_ids", body.get("allowedWorkflowIds", record.allowed_workflow_ids)),
            status=body.get("status", record.status),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="update_template", target=record.template_id)
    return JSONResponse(updated.public_dict(include_versions=True, include_content=True))


async def _template_versions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    record = template_store.get_template(request.path_params["tid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
        updated = template_store.add_version(record.template_id, content=body.get("content") or {}, created_by=claims["name"], notes=str(body.get("notes") or ""))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="version_template", target=record.template_id, detail=f"v{updated.current_version}")
    return JSONResponse(updated.public_dict(include_versions=True, include_content=True), status_code=201)


async def _template_disable(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    updated = template_store.disable_template(request.path_params["tid"])
    if updated is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    audit.log_policy_change(actor=claims["name"], action="disable_template", target=updated.template_id)
    return JSONResponse(updated.public_dict(include_versions=True, include_content=True))
