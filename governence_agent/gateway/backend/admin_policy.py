"""Admin CRUD over the policy store: consumers, categories, departments,
whitelist, access requests, agent profiles. Every write is audited."""
from __future__ import annotations

from auth import graph_mailer
from auth.passwords import hash_password
from dataclasses import replace
from departments import Department
from policy import manifest
from policy.categories import Category
from starlette.responses import JSONResponse
from store import get_store
from store.keys import generate_api_key
from store.keys import hash_api_key
from store.models import ConsumerRecord
import agent_store
import audit
import os
import personal_knowledge_store
import time
import workflows

from .deps import _consumer_public, _require_admin, _require_admin_or_category, _writable_or_error


async def _admin_agents(request):
    claims, err = _require_admin_or_category(request, "agent_admin")
    if err:
        return err
    if request.method == "GET":
        include_disabled = request.query_params.get("includeDisabled", "1") != "0"
        return JSONResponse({"agents": [a.public_dict() for a in agent_store.list_agents(include_disabled=include_disabled)]})
    store, err = _writable_or_error()
    if err:
        return err
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    display_name = str(body.get("display_name") or body.get("displayName") or "Office automation agent").strip()
    consumer_id = str(body.get("consumer_id") or body.get("consumerId") or display_name.lower().replace(" ", "_")).strip()
    if not consumer_id:
        return JSONResponse({"error": "consumer_id is required"}, status_code=400)
    if store.get_consumer(consumer_id) or agent_store.get_agent_by_consumer(consumer_id):
        return JSONResponse({"error": f"agent consumer {consumer_id} already exists"}, status_code=409)
    allowed = [str(x).strip() for x in list(body.get("allowed_template_ids") or body.get("allowedTemplateIds") or []) if str(x).strip()]
    invalid = [tid for tid in allowed if workflows.get_template(tid) is None]
    if invalid:
        return JSONResponse({"error": "unknown workflow template", "templateIds": invalid}, status_code=400)
    categories = [str(x).strip() for x in list(body.get("categories") or []) if str(x).strip()]
    api_key = generate_api_key()
    now = time.time()
    consumer = ConsumerRecord(
        consumer_id=consumer_id,
        name=consumer_id,
        key_hash=hash_api_key(api_key),
        status="active",
        role="user",
        type="agent",
        categories=categories,
        key_created_at=now,
        key_rotated_at=now,
    )
    store.upsert_consumer(consumer)
    try:
        max_runs_per_day = int(body.get("max_runs_per_day") or body.get("maxRunsPerDay") or 24)
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_runs_per_day must be an integer"}, status_code=400)
    profile = agent_store.create_agent(
        display_name=display_name,
        consumer_id=consumer_id,
        description=str(body.get("description") or ""),
        categories=categories,
        allowed_template_ids=allowed,
        max_runs_per_day=max_runs_per_day,
        created_by=claims["name"],
    )
    audit.log_policy_change(actor=claims["name"], action="create_agent", target=profile.agent_id, detail=consumer_id)
    return JSONResponse({"agent": profile.public_dict(), "api_key": api_key}, status_code=201)


async def _admin_agent_item(request):
    claims, err = _require_admin_or_category(request, "agent_admin")
    if err:
        return err
    profile = agent_store.get_agent(request.path_params["aid"])
    if profile is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "GET":
        return JSONResponse(profile.public_dict())
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    changes = {}
    if "display_name" in body or "displayName" in body:
        changes["display_name"] = str(body.get("display_name") or body.get("displayName") or profile.display_name).strip()
    if "description" in body:
        changes["description"] = str(body.get("description") or "")
    if "categories" in body:
        categories = [str(x).strip() for x in list(body.get("categories") or []) if str(x).strip()]
        changes["categories"] = categories
        store, store_err = _writable_or_error()
        if store_err:
            return store_err
        consumer = store.get_consumer(profile.consumer_id)
        if consumer:
            store.upsert_consumer(replace(consumer, categories=categories))
    if "allowed_template_ids" in body or "allowedTemplateIds" in body:
        allowed = [str(x).strip() for x in list(body.get("allowed_template_ids") or body.get("allowedTemplateIds") or []) if str(x).strip()]
        invalid = [tid for tid in allowed if workflows.get_template(tid) is None]
        if invalid:
            return JSONResponse({"error": "unknown workflow template", "templateIds": invalid}, status_code=400)
        changes["allowed_template_ids"] = allowed
    if "max_runs_per_day" in body or "maxRunsPerDay" in body:
        try:
            changes["max_runs_per_day"] = max(1, int(body.get("max_runs_per_day") or body.get("maxRunsPerDay") or profile.max_runs_per_day))
        except (TypeError, ValueError):
            return JSONResponse({"error": "max_runs_per_day must be an integer"}, status_code=400)
    if "status" in body:
        status = str(body.get("status") or "").strip().lower()
        if status not in ("active", "disabled"):
            return JSONResponse({"error": "status must be active or disabled"}, status_code=400)
        changes["status"] = status
        store, store_err = _writable_or_error()
        if store_err:
            return store_err
        consumer = store.get_consumer(profile.consumer_id)
        if consumer:
            store.upsert_consumer(replace(consumer, status="active" if status == "active" else "disabled"))
    updated = agent_store.update_agent(profile.agent_id, **changes) if changes else profile
    audit.log_policy_change(actor=claims["name"], action="update_agent", target=profile.agent_id, detail=updated.status)
    return JSONResponse(updated.public_dict())


async def _admin_catalog(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    return JSONResponse({
        "backends": {b: sorted(manifest.tools_for_backend(b)) for b in sorted(manifest.backends())},
        "levels": [manifest.PUBLIC, manifest.INTERNAL, manifest.PII, manifest.SENSITIVE],
        "categories": [c.id for c in store.categories()],
        "departments": [d.id for d in store.departments()],
        "roles": ["user", "admin"], "types": ["agent", "user"],
    })


async def _admin_consumers(request):
    claims, err = _require_admin(request)
    if err:
        return err
    if request.method == "GET":
        return JSONResponse({"consumers": [_consumer_public(r) for r in get_store().consumers()]})
    # POST -> create a consumer, mint an API key (shown once)
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    consumer_id = str(body.get("consumer_id") or name).strip()
    if store.get_consumer(consumer_id):
        return JSONResponse({"error": f"consumer {consumer_id} already exists"}, status_code=409)
    api_key = generate_api_key()
    password = body.get("password")
    _now = time.time()
    # role="admin" is an unconditional full-access bypass in policy/resolve.py --
    # categories/department/overrides/allowed_levels below are stored as given
    # (useful for bookkeeping, e.g. which department an admin nominally sits
    # in) but never narrow an admin's actual tool-calling access.
    record = ConsumerRecord(
        consumer_id=consumer_id, name=name, key_hash=hash_api_key(api_key),
        status=body.get("status", "active"), role=body.get("role", "user"),
        type=body.get("type", "agent"), categories=list(body.get("categories") or []),
        department=body.get("department") or "",
        rate_limit_per_hour=body.get("rate_limit_per_hour"),
        ip_allowlist=list(body.get("ip_allowlist") or []),
        overrides=body.get("overrides") or {}, allowed_levels=frozenset(body.get("allowed_levels") or []),
        login_password_hash=hash_password(password) if password else None,
        key_created_at=_now, key_rotated_at=_now,
    )
    store.upsert_consumer(record)
    audit.log_policy_change(actor=claims["name"], action="create_consumer", target=consumer_id)
    return JSONResponse({"consumer": _consumer_public(record), "api_key": api_key}, status_code=201)


async def _admin_consumer_item(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    cid = request.path_params["cid"]
    existing = store.get_consumer(cid)
    if not existing:
        return JSONResponse({"error": "not found"}, status_code=404)

    if request.method == "DELETE":
        # Cascade personal-KB documents BEFORE deleting the consumer record, keyed
        # by `existing.name` (= `owner` everywhere else in this codebase) -- not
        # `cid`/consumer_id. If `name` is ever reused for a new consumer after
        # this, the new person must not silently inherit the old owner's private
        # documents (digest_persoanl_kb.md §2's cascade-delete gap).
        deleted_docs = personal_knowledge_store.delete_all_for_owner(existing.name)
        store.delete_consumer(cid)
        audit.log_policy_change(actor=claims["name"], action="delete_consumer", target=cid,
                                detail=f"cascaded {deleted_docs} personal knowledge document(s)" if deleted_docs else None)
        return JSONResponse({"ok": True})

    body = await request.json()
    fields = {}
    for f in ("status", "role", "type", "categories", "rate_limit_per_hour", "overrides", "full_name", "department"):
        if f in body:
            fields[f] = body[f]
    if "ip_allowlist" in body:
        fields["ip_allowlist"] = list(body["ip_allowlist"] or [])
    if "allowed_levels" in body:
        fields["allowed_levels"] = frozenset(body["allowed_levels"] or [])
    if body.get("password"):
        fields["login_password_hash"] = hash_password(body["password"])

    store.upsert_consumer(replace(existing, **fields))
    audit.log_policy_change(actor=claims["name"], action="update_consumer", target=cid,
                            detail=",".join(sorted(fields)))
    return JSONResponse({"consumer": _consumer_public(store.get_consumer(cid))})


async def _admin_consumer_rotate(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    cid = request.path_params["cid"]
    existing = store.get_consumer(cid)
    if not existing:
        return JSONResponse({"error": "not found"}, status_code=404)
    from dataclasses import replace
    api_key = generate_api_key()
    _now = time.time()
    store.upsert_consumer(replace(existing, key_hash=hash_api_key(api_key),
                                  key_created_at=existing.key_created_at or _now, key_rotated_at=_now))
    audit.log_policy_change(actor=claims["name"], action="rotate_key", target=cid)
    return JSONResponse({"api_key": api_key})


async def _admin_categories(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        from store.file_store import _category_to_dict
        return JSONResponse({"categories": [_category_to_dict(c) for c in store.categories()]})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    if not body.get("id") or not body.get("backend"):
        return JSONResponse({"error": "id and backend are required"}, status_code=400)
    if body["backend"] not in manifest.backends():
        return JSONResponse(
            {"error": f"unknown backend {body['backend']!r}; one of: {sorted(manifest.backends())}"},
            status_code=400)
    cat = Category(
        id=body["id"], display_name=body.get("display_name", body["id"]), backend=body["backend"],
        tools=("*" if body.get("tools") == "*" else frozenset(body.get("tools") or [])),
        levels=frozenset(body.get("levels") or []), data_domains=list(body.get("data_domains") or []),
    )
    store.upsert_category(cat)
    audit.log_policy_change(actor=claims["name"], action="upsert_category", target=cat.id)
    return JSONResponse({"ok": True, "id": cat.id})


async def _admin_category_item(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    cid = request.path_params["cid"]
    store.delete_category(cid)
    audit.log_policy_change(actor=claims["name"], action="delete_category", target=cid)
    return JSONResponse({"ok": True})


async def _admin_departments(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        from store.file_store import _department_to_dict
        return JSONResponse({"departments": [_department_to_dict(d) for d in store.departments()]})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    if not body.get("id"):
        return JSONResponse({"error": "id is required"}, status_code=400)
    unknown = [c for c in (body.get("categories") or []) if store.get_category(c) is None]
    if unknown:
        return JSONResponse({"error": f"unknown categor{'y' if len(unknown)==1 else 'ies'}: {', '.join(unknown)}"},
                            status_code=400)
    dept = Department(id=body["id"], display_name=body.get("display_name", body["id"]),
                      categories=tuple(body.get("categories") or []))
    store.upsert_department(dept)
    audit.log_policy_change(actor=claims["name"], action="upsert_department", target=dept.id)
    return JSONResponse({"ok": True, "id": dept.id})


async def _admin_department_item(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    did = request.path_params["did"]
    store.delete_department(did)
    audit.log_policy_change(actor=claims["name"], action="delete_department", target=did)
    return JSONResponse({"ok": True})


async def _admin_whitelist(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        return JSONResponse({"whitelist": store.get_whitelist()})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    store.set_whitelist(list(body.get("cidrs") or []))
    audit.log_policy_change(actor=claims["name"], action="set_whitelist", target="global")
    return JSONResponse({"ok": True, "whitelist": store.get_whitelist()})


async def _admin_policy_changes(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"changes": audit.recent_policy_changes(200)})


async def _admin_requests(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    reqs = get_store().list_access_requests()
    if request.query_params.get("status"):
        reqs = [r for r in reqs if r.get("status") == request.query_params["status"]]
    return JSONResponse({"requests": reqs})


def _find_request(store, rid):
    for r in store.list_access_requests():
        if r.get("id") == rid:
            return r
    return None


async def _admin_request_approve(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    rid = request.path_params["rid"]
    req = _find_request(store, rid)
    if not req:
        return JSONResponse({"error": "not found"}, status_code=404)
    if req["kind"] == "workflow":
        # Not a grant -- there's nothing to merge into a consumer's overrides. This
        # just marks the request as reviewed/acknowledged so it drops off the
        # pending queue; building the actual capability is separate, manual work.
        store.update_access_request(rid, {"status": "acknowledged", "decided_by": claims["name"], "decided_at": time.time()})
        audit.log_policy_change(actor=claims["name"], action="acknowledge_workflow_request", target=req.get("consumer_id", ""))
        return JSONResponse({"ok": True})
    body = await request.json() if request.headers.get("content-length") else {}
    record = store.get_consumer(req["consumer_id"])
    if not record:
        return JSONResponse({"error": "consumer no longer exists"}, status_code=409)

    if req["kind"] == "account":
        categories = (body.get("categories") or req.get("categories") or record.categories)
        store.upsert_consumer(replace(record, status="active", categories=list(categories),
                                      overrides=body.get("overrides") or record.overrides))
        if record.email:
            login_url = (os.getenv("GATEWAY_PUBLIC_URL") or "").rstrip("/")
            try:
                await graph_mailer.send_account_approved_email(
                    record.email, record.full_name, f"{login_url}/login" if login_url else "")
            except graph_mailer.GraphMailerError:
                # Best-effort -- the account is already active either way; they
                # can still sign in without ever seeing this email.
                pass
    elif "selections" in req:  # access request (tool-level, grouped by category)
        overrides = {k: dict(v) for k, v in (record.overrides or {}).items()}
        any_valid = False
        for sel in req.get("selections") or []:
            backend = sel.get("backend") or ""
            tools = [t for t in (sel.get("tools") or []) if t]
            if backend not in manifest.backends() or not tools:
                continue
            any_valid = True
            b = overrides.get(backend, {})
            b["grantTools"] = sorted(set(b.get("grantTools", [])) | set(tools))
            # Grant the category's levels too, else the newly-granted tool comes
            # back with everything classified redacted -- the requester picked
            # tools, not PUBLIC/INTERNAL/PII/SENSITIVE, so this is what makes the
            # grant actually show real data.
            category = store.get_category(sel.get("category"))
            if category:
                b["grantLevels"] = sorted(set(b.get("grantLevels", [])) | set(category.levels))
            overrides[backend] = b
        if not any_valid:
            return JSONResponse(
                {"error": "request has no valid selections; deny it and ask the requester to resubmit"},
                status_code=409,
            )
        store.upsert_consumer(replace(record, overrides=overrides))
    elif "categories" in req:  # access request (whole-category), from an earlier
        # iteration of this self-service redesign -- kept so any request already
        # pending when this shipped can still be approved.
        requested = [c for c in (req.get("categories") or []) if store.get_category(c) is not None]
        if not requested:
            return JSONResponse(
                {"error": "request has no valid categories; deny it and ask the requester to resubmit"},
                status_code=409,
            )
        categories = sorted(set(record.categories or []) | set(requested))
        store.upsert_consumer(replace(record, categories=categories))
    else:
        # Legacy shape (backend/tools/levels -> overrides), from before the
        # category-based self-service redesign -- kept only so a request already
        # pending when this shipped can still be approved.
        backend = req.get("backend") or ""
        if backend not in manifest.backends():
            # No silent default to a guessed backend -- a request predating the
            # 2026-07 domain split may have no backend or the stale "minierp".
            return JSONResponse(
                {"error": f"request has no valid backend ({backend!r}); deny it and ask the "
                          f"requester to resubmit with one of: {sorted(manifest.backends())}"},
                status_code=409,
            )
        overrides = {k: dict(v) for k, v in (record.overrides or {}).items()}
        b = overrides.get(backend, {})
        b["grantTools"] = sorted(set(b.get("grantTools", [])) | set(req.get("tools") or []))
        b["grantLevels"] = sorted(set(b.get("grantLevels", [])) | set(req.get("levels") or []))
        overrides[backend] = b
        store.upsert_consumer(replace(record, overrides=overrides))

    store.update_access_request(rid, {"status": "approved", "decided_by": claims["name"], "decided_at": time.time()})
    audit.log_policy_change(actor=claims["name"], action=f"approve_{req['kind']}", target=req["consumer_id"])
    return JSONResponse({"ok": True})


async def _admin_request_deny(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    rid = request.path_params["rid"]
    req = _find_request(store, rid)
    if not req:
        return JSONResponse({"error": "not found"}, status_code=404)
    if req["kind"] == "workflow":
        store.update_access_request(rid, {"status": "dismissed", "decided_by": claims["name"], "decided_at": time.time()})
        audit.log_policy_change(actor=claims["name"], action="dismiss_workflow_request", target=req.get("consumer_id", ""))
        return JSONResponse({"ok": True})
    if req["kind"] == "account":
        record = store.get_consumer(req["consumer_id"])
        if record:
            store.upsert_consumer(replace(record, status="disabled"))
    store.update_access_request(rid, {"status": "denied", "decided_by": claims["name"], "decided_at": time.time()})
    audit.log_policy_change(actor=claims["name"], action=f"deny_{req['kind']}", target=req["consumer_id"])
    return JSONResponse({"ok": True})


async def _admin_access_suggestions(request):
    """Denial-derived access requests: aggregate 'not_granted' denials by (consumer, tool)."""
    _claims, err = _require_admin(request)
    if err:
        return err
    agg: dict[tuple, int] = {}
    for r in audit.recent(1000):
        if r.get("type") == "denied" and r.get("detail") == "not_granted":
            key = (r.get("consumer"), r.get("tool"))
            agg[key] = agg.get(key, 0) + 1
    suggestions = [{"consumer": c, "tool": t, "attempts": n} for (c, t), n in agg.items()]
    suggestions.sort(key=lambda s: s["attempts"], reverse=True)
    return JSONResponse({"suggestions": suggestions})
