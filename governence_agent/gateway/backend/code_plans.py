"""Read-only code planning lane (opencode)."""
from __future__ import annotations

from starlette.responses import JSONResponse
from store import get_store
import approval_store
import audit
import code_plan_store

from .deps import _session, _unauthorized


async def _code_plans(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    all_requested = request.query_params.get("all") in ("1", "true", "yes")
    owner = None if claims["role"] == "admin" and all_requested else claims["name"]
    limit = int(request.query_params.get("limit") or 100)
    records = [r.public_dict(include_template=False) for r in code_plan_store.list_plans(owner=owner, limit=limit)]
    return JSONResponse({"plans": records})


async def _code_plan_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    plan = code_plan_store.get_plan(request.path_params["pid"])
    if not plan:
        return JSONResponse({"error": "not found"}, status_code=404)
    if claims["role"] != "admin" and plan.owner != claims["name"]:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(plan.public_dict(include_template=True))


async def _code_plan_request_approval(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    plan = code_plan_store.get_plan(request.path_params["pid"])
    if not plan:
        return JSONResponse({"error": "not found"}, status_code=404)
    if claims["role"] != "admin" and plan.owner != claims["name"]:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json() if request.headers.get("content-length") else {}
    reason = str(body.get("reason") or f"Approve code plan {plan.plan_id}: {plan.summary}")
    approval = approval_store.create_approval(
        requested_by=claims["name"],
        reason=reason,
        risk_level=plan.risk_level,
        artifact_ids=[],
        workflow_run_id="",
    )
    updated = code_plan_store.attach_approval(plan.plan_id, approval.approval_id) or plan
    audit.log_policy_change(actor=claims["name"], action="request_code_plan_approval", target=plan.plan_id, detail=reason[:200])
    return JSONResponse({"plan": updated.public_dict(), "approval": approval.public_dict()}, status_code=201)
