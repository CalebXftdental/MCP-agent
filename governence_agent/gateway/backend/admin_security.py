"""Admin read models over the audit trail plus the break-glass controls."""
from __future__ import annotations

from policy import manifest
from starlette.responses import JSONResponse
from store import get_store
import analytics
import artifact_store
import audit
import edge
import json
import safety
import scope_store
import time

from .deps import _range_sec, _require_admin, _session, _unauthorized, _writable_or_error


async def _admin_calls(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    # Only actual tool-call attempts (Layer 2: reached, or were denied by, a
    # specific tool's policy) -- auth failures, rate limits, and policy changes
    # (signup, key rotation, category edits) are real events but not "a tool
    # call"; policy changes already have their own view (/admin/policy-changes).
    # Filter BEFORE truncating to `limit` so noisy non-call events (e.g. a burst
    # of failed logins) can't crowd real tool calls out of the window.
    calls = [c for c in audit.recent(1000) if c.get("type") in ("call", "denied")]
    # Role-scoped monitoring: a non-admin sees only its own principal's calls.
    if claims.get("role") != "admin":
        calls = [c for c in calls if c.get("consumer") == claims["name"]]
    # Classify against the FULL available window (not yet truncated to `limit`)
    # so a burst/enumeration pattern near the edge of the window isn't
    # under/over-counted by an unrelated display cap.
    calls = safety.classify(calls)
    return JSONResponse({"calls": calls[:limit]})


async def _admin_audit_export(request):
    claims, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    try:
        hours = float(body.get("hours") or request.query_params.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 24 * 30))
    try:
        limit = int(body.get("limit") or request.query_params.get("limit") or 5000)
    except (TypeError, ValueError):
        limit = 5000
    limit = max(1, min(limit, 20000))
    event_type = str(body.get("type") or request.query_params.get("type") or "all").strip().lower()
    consumer = str(body.get("consumer") or request.query_params.get("consumer") or "").strip()
    since = time.time() - hours * 3600
    records = audit.read_since(since, limit=limit)
    if event_type != "all":
        records = [r for r in records if r.get("type") == event_type]
    if consumer:
        records = [r for r in records if r.get("consumer") == consumer]
    records = safety.classify(records)
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    payload = {
        "kind": "audit_export",
        "schemaVersion": 1,
        "exportedAt": time.time(),
        "exportedBy": claims["name"],
        "filters": {"hours": hours, "type": event_type, "consumer": consumer, "limit": limit},
        "summary": {"total": len(records), "byType": counts, "suspicious": sum(1 for r in records if r.get("suspicious"))},
        "records": records,
    }
    safe_type = event_type if event_type != "all" else "all-events"
    filename = f"audit-export-{safe_type}-{int(payload['exportedAt'])}.json"
    record = artifact_store.create_artifact(
        owner=claims["name"],
        title="Audit Export",
        filename=filename,
        payload=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        artifact_type="json",
        mime_type="application/json",
        classification=[manifest.INTERNAL],
        source_tool_calls=["audit_export"],
        retention_days=365,
    )
    audit.log_policy_change(actor=claims["name"], action="export_audit", target=record.artifact_id, detail=f"records={len(records)}; hours={hours}; type={event_type}; consumer={consumer or '*'}")
    return JSONResponse({"artifact": record.public_dict(), "summary": payload["summary"], "filters": payload["filters"]}, status_code=201)


async def _admin_sessions(request):
    claims = _session(request)
    if not claims or claims.get("role") != "admin":
        return _unauthorized(is_admin=bool(claims))
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    return JSONResponse({"sessions": scope_store.snapshot(limit)})


async def _admin_rate_limits(request):
    claims = _session(request)
    if not claims or claims.get("role") != "admin":
        return _unauthorized(is_admin=bool(claims))
    return JSONResponse({"rate_limits": edge.rate_limit_snapshot()})


async def _admin_overview(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse(analytics.overview_cached(_range_sec(request)))


async def _admin_alerts(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"alerts": analytics.alerts_cached()})


async def _admin_alert_action(request):
    claims, err = _require_admin(request)
    if err:
        return err
    action = request.path_params["action"]
    if action not in ("ack", "resolve"):
        return JSONResponse({"error": "action must be ack or resolve"}, status_code=400)
    alert_id = request.path_params["aid"]
    status = "acknowledged" if action == "ack" else "resolved"
    # The policy-change event IS the durable state: analytics.alerts() reconstructs
    # each incident's status from these (see analytics._alert_statuses).
    audit.log_policy_change(actor=claims["name"], action=f"alert_{action}", target=alert_id)
    analytics.invalidate_cache("alerts")  # reflect the new status on the next poll
    return JSONResponse({"ok": True, "id": alert_id, "status": status})


async def _admin_consumer_profile(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    profile = analytics.consumer_profile(request.path_params["cid"])
    if profile is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(profile)


async def _admin_credential_hygiene(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"consumers": analytics.credential_hygiene_cached()})


async def _admin_backend_health(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    probe = request.query_params.get("probe", "1") != "0"
    return JSONResponse({"backends": await analytics.backends_health_cached(probe=probe)})


async def _admin_controls(request):
    """Break-glass controls: pause all agents and/or specific backends. GET reads
    current state + the backend list; PUT sets it (enforced in _govern)."""
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        return JSONResponse({"controls": store.get_controls(), "backends": sorted(manifest.backends())})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    controls = {"paused_agents": bool(body.get("paused_agents")),
                "paused_backends": [b for b in (body.get("paused_backends") or []) if b in manifest.backends()]}
    store.set_controls(controls)
    audit.log_policy_change(actor=claims["name"], action="set_controls", target="global",
                            detail=json.dumps(controls))
    return JSONResponse({"ok": True, "controls": store.get_controls()})
