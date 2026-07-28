"""Scheduled automations and their due-run trigger."""
from __future__ import annotations

from starlette.responses import JSONResponse
from store import get_store
import agent_store
import audit
import automation_store
import time
import workflows

from .deps import _session, _unauthorized
from .workflow_api import _execute_workflow_for_record


async def _automations(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        owner = None if claims.get("role") == "admin" and request.query_params.get("all", "1") != "0" else claims["name"]
        records = [a.public_dict() for a in automation_store.list_automations(owner=owner)]
        return JSONResponse({"automations": records})
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    template_id = str(body.get("template_id") or body.get("templateId") or "").strip()
    template = workflows.get_template(template_id)
    if template is None:
        return JSONResponse({"error": "unknown workflow template"}, status_code=400)
    if template.get("status") != "active":
        return JSONResponse({"error": "workflow template is disabled"}, status_code=403)
    inputs = body.get("inputs") or {}
    if not isinstance(inputs, dict):
        return JSONResponse({"error": "inputs must be an object"}, status_code=400)
    try:
        interval_sec = int(body.get("interval_sec") or body.get("intervalSec") or 86400)
        next_run_at = body.get("next_run_at", body.get("nextRunAt"))
        next_run_at = float(next_run_at) if next_run_at is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid interval or next_run_at"}, status_code=400)
    agent_id = str(body.get("agent_id") or body.get("agentId") or "").strip()
    actor_type = "user"
    owner_name = record.name
    if agent_id:
        if claims.get("role") != "admin":
            return _unauthorized(is_admin=True)
        profile = agent_store.get_agent(agent_id)
        if profile is None:
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        if profile.status != "active":
            return JSONResponse({"error": "agent is disabled"}, status_code=403)
        if not agent_store.allowed_for_template(profile, template_id):
            return JSONResponse({"error": "agent is not allowed to run this workflow"}, status_code=403)
        owner_name = profile.consumer_id
        actor_type = "agent"
    automation = automation_store.create_automation(
        owner=owner_name,
        template_id=template_id,
        display_name=str(body.get("display_name") or body.get("displayName") or template_id),
        inputs=inputs,
        interval_sec=interval_sec,
        next_run_at=next_run_at,
        actor_type=actor_type,
        agent_id=agent_id,
    )
    audit.log_policy_change(actor=claims["name"], action="create_automation", target=automation.automation_id, detail=f"{template_id}; actor={owner_name}")
    return JSONResponse(automation.public_dict(), status_code=201)


async def _automation_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    automation = automation_store.get_automation(request.path_params["aid"])
    if automation is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if automation.owner != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    if request.method == "DELETE":
        automation_store.delete_automation(automation.automation_id)
        audit.log_policy_change(actor=claims["name"], action="delete_automation", target=automation.automation_id)
        return JSONResponse({"ok": True})
    return JSONResponse(automation.public_dict())


async def _automation_run_due(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    now = float(body.get("now") or time.time())
    owner = str(body.get("owner") or "").strip() or None
    due = automation_store.due_automations(now=now, owner=owner)
    results = []
    store = get_store()
    controls = store.get_controls() if hasattr(store, "get_controls") else {}
    for auto in due:
        template = workflows.get_template(auto.template_id)
        if template is None or template.get("status") != "active":
            automation_store.mark_run(auto.automation_id, run_id="", status="template_disabled", now=now)
            results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "template_disabled"})
            continue
        agent = agent_store.get_agent(auto.agent_id) if auto.agent_id else None
        if auto.actor_type == "agent":
            if controls.get("paused_agents"):
                automation_store.mark_run(auto.automation_id, run_id="", status="agents_paused", now=now)
                results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "agents_paused"})
                continue
            if agent is None:
                automation_store.mark_run(auto.automation_id, run_id="", status="agent_missing", now=now)
                results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "agent_missing"})
                continue
            if not agent_store.allowed_for_template(agent, auto.template_id):
                automation_store.mark_run(auto.automation_id, run_id="", status="agent_not_allowed", now=now)
                results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "agent_not_allowed"})
                continue
        consumer = next((c for c in store.consumers() if c.name == auto.owner), None)
        if not consumer or consumer.status != "active":
            automation_store.mark_run(auto.automation_id, run_id="", status="owner_inactive", now=now)
            results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "owner_inactive"})
            continue
        result = await _execute_workflow_for_record(consumer, auto.template_id, dict(auto.inputs), source=("agent:" + auto.agent_id if auto.agent_id else "automation:" + auto.automation_id))
        status = result.get("status") or "failed"
        automation_store.mark_run(auto.automation_id, run_id=result.get("runId", ""), status=status, now=now)
        if agent and status == "completed":
            agent_store.mark_run(agent.agent_id, run_id=result.get("runId", ""), now=now)
        results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": status, "run": result})
    return JSONResponse({"ran": len(results), "results": results})
