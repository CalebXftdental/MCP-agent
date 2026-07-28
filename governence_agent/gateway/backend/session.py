"""Login/logout, the caller's own access, key rotation, access requests, playground."""
from __future__ import annotations

from auth import lockout
from auth.passwords import hash_password
from auth.passwords import verify_password
from auth.session import issue_session
from auth.session import login_enabled
from dataclasses import replace
from policy import manifest
from policy.resolve import resolve as resolve_grant
from starlette.responses import JSONResponse
from store import get_store
from store.keys import generate_api_key
from store.keys import hash_api_key
from store.models import ConsumerRecord
import audit
import edge
import json
import request_context as ctx
import safety
import time
import uuid

from govern import _govern
from .deps import (
    _COOKIE,
    _COOKIE_SECURE,
    _SESSION_TTL,
    _effective_access_view,
    _effective_category_ids,
    _session,
    _tool_info,
    _unauthorized,
    _writable_or_error,
)


async def _login(request):
    if not login_enabled():
        return JSONResponse({"error": "login is not configured"}, status_code=500)
    client_ip = ctx.client_ip(request)
    ua = request.headers.get("user-agent", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    lock_key = f"{username}|{client_ip}"

    if lockout.is_locked(lock_key):
        audit.log_auth_denied(path="/dashboard/login", client_ip=client_ip, user_agent=ua, reason="locked_out")
        return JSONResponse({"error": "too many attempts; try again later"}, status_code=429)

    record = get_store().get_by_username(username)
    if record is None or not verify_password(password, record.login_password_hash):
        tripped = lockout.record_failure(lock_key)
        audit.log_auth_denied(path="/dashboard/login", client_ip=client_ip, user_agent=ua,
                               reason="locked_out" if tripped else "bad_credentials")
        return JSONResponse({"error": "invalid username or password"}, status_code=401)

    lockout.reset(lock_key)
    token = issue_session(record.consumer_id, record.name, record.role, _SESSION_TTL)
    resp = JSONResponse({"ok": True, "name": record.name, "role": record.role})
    resp.set_cookie(_COOKIE, token, max_age=_SESSION_TTL, httponly=True,
                    secure=_COOKIE_SECURE, samesite="strict", path="/")
    return resp


async def _logout(_request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE, path="/")
    return resp


async def _me(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    return JSONResponse({"name": claims["name"], "role": claims["role"]})


async def _categories_catalog(request):
    """Self-service (any logged-in user, not admin-only): the full category catalog,
    each with its actual tool list, for the 'My Access' request-access picker --
    category groups the picker (and supplies backend + redaction levels at approval
    time), but the user selects individual TOOLS within it, not the whole category."""
    if not _session(request):
        return _unauthorized()
    out = []
    for c in get_store().categories():
        names = manifest.tools_for_backend(c.backend) if c.tools == "*" else c.tools
        out.append({
            "id": c.id, "display_name": c.display_name, "backend": c.backend,
            "tools": _tool_info(names),
            # Gateway-level permission markers (files/workflow_runner/workflow_admin/
            # agent_admin) have no MCP tools of their own -- data_domains is the only
            # explanation the picker can show for what holding them actually does.
            "data_domains": list(getattr(c, "data_domains", None) or []),
        })
    return JSONResponse({"categories": out})


async def _departments(_request):
    """Public: the signup form's department options (id/display_name/current categories).
    Reads the live, admin-editable store (not the code seed), so an admin's edits to a
    department show up here immediately too."""
    return JSONResponse({"departments": [
        {"id": d.id, "display_name": d.display_name, "categories": list(d.categories)}
        for d in get_store().departments()
    ]})


async def _signup(request):
    store, err = _writable_or_error()
    if err:
        return err
    if not login_enabled():
        return JSONResponse({"error": "signup is not configured"}, status_code=500)
    body = await request.json()
    full_name = str(body.get("full_name") or "").strip()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    department_id = str(body.get("department") or "").strip()
    if not full_name or not username or not password or not department_id:
        return JSONResponse(
            {"error": "full name, username, password, and department are required"}, status_code=400)
    department = store.get_department(department_id)
    if department is None:
        return JSONResponse(
            {"error": f"unknown department {department_id!r}; one of: "
                      f"{sorted(d.id for d in store.departments())}"},
            status_code=400)
    consumer_id = f"user:{username}"
    if store.get_by_username(username) or store.get_consumer(consumer_id):
        return JSONResponse({"error": "that username is taken"}, status_code=409)
    # categories stays empty -- the department's CURRENT categories are resolved live
    # on every request (policy/resolve.py), not copied here. Editing the department
    # later reaches this user automatically; no per-user field to keep in sync.
    store.upsert_consumer(ConsumerRecord(
        consumer_id=consumer_id, name=username, key_hash="", status="active",
        role="user", type="user", categories=[],
        login_password_hash=hash_password(password),
        full_name=full_name, department=department.id,
    ))
    audit.log_policy_change(actor=username, action="signup", target=consumer_id,
                            detail=f"department={department.id}")

    token = issue_session(consumer_id, username, "user", _SESSION_TTL)
    resp = JSONResponse({"ok": True, "status": "active", "name": username}, status_code=201)
    resp.set_cookie(_COOKIE, token, max_age=_SESSION_TTL, httponly=True,
                    secure=_COOKIE_SECURE, samesite="strict", path="/")
    return resp


async def _my_access(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record:
        return JSONResponse({"name": claims["name"], "status": "unknown"})
    active = record.status == "active"
    my_requests = [r for r in get_store().list_access_requests() if r.get("consumer_id") == record.consumer_id]
    return JSONResponse({
        "name": record.name, "full_name": record.full_name, "department": record.department,
        "status": record.status, "role": record.role,
        "type": record.type, "categories": record.categories,
        "effective_categories": sorted(_effective_category_ids(get_store(), record)),
        "has_key": bool(record.key_hash),
        "access": _effective_access_view(record) if active else {},
        "requests": my_requests,
    })


async def _my_key_rotate(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    store, err = _writable_or_error()
    if err:
        return err
    record = store.get_consumer(claims["sub"])
    if not record:
        return JSONResponse({"error": "not found"}, status_code=404)
    if record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    api_key = generate_api_key()
    _now = time.time()
    store.upsert_consumer(replace(record, key_hash=hash_api_key(api_key),
                                  key_created_at=record.key_created_at or _now, key_rotated_at=_now))
    audit.log_policy_change(actor=record.name, action="rotate_own_key", target=record.consumer_id)
    return JSONResponse({"api_key": api_key})


async def _request_access(request):
    """Self-service: request specific TOOLS, grouped/picked by category (category
    supplies the backend + redaction levels so a granted tool isn't just returned
    masked to nothing -- a normal user shouldn't have to reason about PUBLIC/
    INTERNAL/PII/SENSITIVE directly). Admin reviews via /admin/requests; approving
    merges the requested tools (+ that category's levels) into the consumer's
    `overrides` for that backend (see _admin_request_approve) -- categories/
    department stay untouched, so this is purely additive on top of them."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    store, err = _writable_or_error()
    if err:
        return err
    record = store.get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    body = await request.json()
    raw_selections = body.get("selections") or []
    if not raw_selections:
        return JSONResponse({"error": "at least one category with tools is required"}, status_code=400)

    grant = resolve_grant(record, store.get_category, store.get_department)
    selections = []
    for sel in raw_selections:
        cat_id = str(sel.get("category") or "").strip()
        category = store.get_category(cat_id)
        if category is None:
            return JSONResponse({"error": f"unknown category {cat_id!r}"}, status_code=400)
        available = set(manifest.tools_for_backend(category.backend)) if category.tools == "*" else set(category.tools)
        requested_tools = [str(t).strip() for t in (sel.get("tools") or []) if str(t).strip()]
        invalid = sorted(set(requested_tools) - available)
        if invalid:
            return JSONResponse(
                {"error": f"{cat_id}: not part of this category: {', '.join(invalid)}"}, status_code=400)
        new_tools = sorted({t for t in requested_tools if not grant.allows_tool(category.backend, t)})
        if new_tools:
            selections.append({"category": cat_id, "backend": category.backend, "tools": new_tools})
    if not selections:
        return JSONResponse({"error": "you already have all of the selected tools"}, status_code=400)

    req = {
        "id": uuid.uuid4().hex[:12], "kind": "access", "consumer_id": record.consumer_id,
        "username": record.name, "selections": selections,
        "justification": str(body.get("justification") or ""), "status": "pending",
        "created_at": time.time(),
    }
    store.add_access_request(req)
    audit.log_policy_change(
        actor=record.name, action="request_access", target=record.consumer_id,
        detail="; ".join(f"{s['category']}: {', '.join(s['tools'])}" for s in selections),
    )
    return JSONResponse({"ok": True, "id": req["id"]}, status_code=201)


async def _my_denials(request):
    """This caller's recent 'not granted' denials, each mapped to the category +
    tool it would take to request -- powering the 'why denied -> request' loop."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    name = claims["name"]
    cats = get_store().categories()
    agg: dict[str, dict] = {}
    for c in audit.recent(1000):
        if c.get("type") != "denied" or c.get("consumer") != name or str(c.get("detail") or "") != "not_granted":
            continue
        tool = c.get("tool") or ""
        canon = manifest.canonical(tool)
        if not canon:
            continue
        e = agg.get(tool)
        if not e:
            pol = manifest.get(canon)
            backend = pol.backend if pol else None
            cat = next((k for k in cats if k.backend == backend and (k.tools == "*" or canon in k.tools)), None)
            e = agg[tool] = {"tool": tool, "canonical": canon,
                             "description": (pol.description if pol else canon) or canon,
                             "backend": backend, "category": cat.id if cat else None,
                             "category_name": cat.display_name if cat else None,
                             "risk": pol.risk if pol else None,
                             "attempts": 0, "last_ts": 0}
        e["attempts"] += 1
        e["last_ts"] = max(e["last_ts"], c.get("ts") or 0)
    return JSONResponse({"denials": sorted(agg.values(), key=lambda x: -x["last_ts"])})


async def _my_activity(request):
    """This caller's own governed calls + a small summary + rate-limit headroom."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    name = claims["name"]
    calls = safety.classify([c for c in audit.recent(1000)
                             if c.get("type") in ("call", "denied") and c.get("consumer") == name])
    summary = {
        "total": len(calls),
        "ok": sum(1 for c in calls if c.get("status") == "ok"),
        "denied": sum(1 for c in calls if c.get("status") == "denied"),
        "error": sum(1 for c in calls if c.get("status") == "error"),
        "redactions": sum(1 for c in calls if str(c.get("detail") or "").startswith("redacted:")),
        "rows": sum((c.get("rows") or 0) for c in calls),
    }
    rl = next((r for r in edge.rate_limit_snapshot() if r.get("consumer") == name), None)
    return JSONResponse({"calls": calls[:200], "summary": summary, "rate_limit": rl})


async def _try_tool(request):
    """In-browser playground: run one governed tool call as THIS user (full PDP +
    scope + redaction + audit), so they can test what their grant actually returns."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    tool = str(body.get("tool") or "").strip()
    canon = manifest.canonical(tool) or tool
    if manifest.get(canon) is None:
        return JSONResponse({"error": f"unknown tool {tool!r}"}, status_code=400)
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return JSONResponse({"error": "args must be a JSON object"}, status_code=400)
    policy = manifest.get(canon)
    if policy and policy.backend in ("office", "email", "knowledge", "calendar", "code"):
        args = {**args, "owner": record.name}
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set(ctx.client_ip(request))
    raw = await _govern(canon, "playground:" + record.consumer_id, str(body.get("customer_id") or ""), args)
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        result = raw
    return JSONResponse({"tool": manifest.namespaced(canon), "result": result})
