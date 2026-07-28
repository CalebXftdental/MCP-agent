"""Queued email and calendar sends awaiting a delivery adapter."""
from __future__ import annotations

from starlette.responses import JSONResponse
import calendar_send_store
import email_send_store

from .deps import _session, _unauthorized


async def _email_sends(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    records = [r.public_dict() for r in email_send_store.list_sends(owner=owner, limit=limit)]
    return JSONResponse({"sends": records})


async def _email_send_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = email_send_store.get_send(request.path_params["sid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if record.owner != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    return JSONResponse(record.public_dict())


async def _calendar_sends(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    records = [r.public_dict() for r in calendar_send_store.list_sends(owner=owner, limit=limit)]
    return JSONResponse({"sends": records})


async def _calendar_send_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = calendar_send_store.get_send(request.path_params["sid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if record.owner != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    return JSONResponse(record.public_dict())
