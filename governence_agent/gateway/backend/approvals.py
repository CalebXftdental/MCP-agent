"""The approval queue and decisions on it."""
from __future__ import annotations

from dataclasses import replace
from starlette.responses import JSONResponse
import approval_store
import audit
import workflows

from .deps import _require_admin, _session, _unauthorized


async def _approvals(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
        include_decided = request.query_params.get("include_decided", "1").lower() not in ("0", "false", "no")
        approvals = approval_store.list_approvals(requested_by=owner, include_decided=include_decided)
        return JSONResponse({"approvals": [a.public_dict() for a in approvals]})
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason") or "Approval requested").strip()
    approval = approval_store.create_approval(
        requested_by=claims["name"],
        reason=reason,
        risk_level=str(body.get("risk_level") or "medium"),
        artifact_ids=list(body.get("artifact_ids") or []),
        workflow_run_id=str(body.get("workflow_run_id") or ""),
    )
    audit.log_policy_change(actor=claims["name"], action="request_approval", target=approval.approval_id, detail=reason)
    return JSONResponse(approval.public_dict(), status_code=201)


async def _approval_decide(request):
    claims, err = _require_admin(request)
    if err:
        return err
    action = request.path_params["action"]
    status = "approved" if action == "approve" else "denied" if action == "deny" else ""
    if not status:
        return JSONResponse({"error": "action must be approve or deny"}, status_code=400)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    approval = approval_store.decide_approval(
        request.path_params["aid"], approver=claims["name"], status=status, note=str(body.get("note") or ""),
    )
    if approval is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if approval.workflow_run_id:
        run = workflows.get_run_record(approval.workflow_run_id)
        if run and run.status == "approval_required":
            steps = []
            for step in run.steps:
                if step.type == "approval" and step.status in ("pending", "running") and (not step.outputs.get("approvalId") or step.outputs.get("approvalId") == approval.approval_id):
                    outputs = dict(step.outputs)
                    outputs.update({"approvalId": approval.approval_id, "approvalStatus": approval.status, "approver": approval.approver})
                    steps.append(replace(step, status=("completed" if approval.status == "approved" else "failed"), outputs=outputs, error=(approval.decision_note if approval.status == "denied" else None)))
                else:
                    steps.append(step)
            workflows.update_run(run, steps=steps, status=("approval_required" if approval.status == "approved" else "failed"), error=(approval.decision_note if approval.status == "denied" else None))
    audit.log_policy_change(actor=claims["name"], action=f"{status}_approval", target=approval.approval_id)
    return JSONResponse(approval.public_dict())
