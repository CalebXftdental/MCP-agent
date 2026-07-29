"""Shared surface every backend module leans on: the session cookie and auth
guards, the static HTML the shell serves, and view helpers used across domains.

Moved verbatim from app.py -- the guards keep their exact contracts (a response,
or a (claims, error) pair), so handler bodies did not have to change."""
from __future__ import annotations

from auth.session import verify_session
from pathlib import Path
from policy import manifest
from policy.resolve import resolve as resolve_grant
from starlette.responses import JSONResponse
from store import get_store
import analytics
import json
import os


# The only thing that could NOT move verbatim: these were Path(__file__).parent
# when __file__ was gateway/app.py. deps.py sits one level deeper, so static/ is
# resolved from the package's parent instead.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_DASHBOARD_PATH = _STATIC_DIR / "dashboard.html"


_DASHBOARD_HTML = _DASHBOARD_PATH.read_text(encoding="utf-8") if _DASHBOARD_PATH.exists() else "<h1>governance gateway</h1>"


_ADMIN_PATH = _STATIC_DIR / "admin.html"


_ADMIN_HTML = _ADMIN_PATH.read_text(encoding="utf-8") if _ADMIN_PATH.exists() else "<h1>governance admin</h1>"


def _static(name: str, fallback: str) -> str:
    p = _STATIC_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else fallback


_ACCOUNT_HTML = _static("account.html", "<h1>my access</h1>")


_SIGNUP_HTML = _static("signup.html", "<h1>sign up</h1>")


_CHAT_HTML = _static("chat.html", "<h1>assistant</h1>")


# The built React SPA (gateway/frontend, `npm run build`) ships to frontend/dist/.
# Prefer it once it exists; fall back to the legacy hand-written shell so the
# gateway still runs for anyone who hasn't built the frontend yet (e.g. local dev
# touching only the Python side). See gateway/frontend/README.md and DEPLOY.md
# for the build step that must run before a deploy.
_FRONTEND_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_FRONTEND_INDEX_PATH = _FRONTEND_DIST_DIR / "index.html"
_APP_HTML = (
    _FRONTEND_INDEX_PATH.read_text(encoding="utf-8") if _FRONTEND_INDEX_PATH.exists()
    else _static("app.html", "<h1>governance</h1>")
)


_LOGO_PATH = _STATIC_DIR / "frontier-logo.png"


_LOGO_BYTES = _LOGO_PATH.read_bytes() if _LOGO_PATH.exists() else b""


_COOKIE = "gov_session"


_COOKIE_SECURE = (os.getenv("GOVERNANCE_COOKIE_SECURE") or "true").lower() != "false"


_SESSION_TTL = int(os.getenv("GOVERNANCE_SESSION_TTL_SEC") or str(60 * 60))


# Chat history: a conversation idle this long is closed + summarized (chat_log.py),
# checked both lazily (on the next message to that conversation_id) and by the
# background sweep below (the general guarantee -- catches abandoned tabs).
_CHAT_IDLE_SEC = int(os.getenv("GOVERNANCE_CHAT_IDLE_SEC") or str(60 * 60))


_CHAT_SWEEP_INTERVAL_SEC = int(os.getenv("GOVERNANCE_CHAT_SWEEP_INTERVAL_SEC") or "300")


_LOGIN_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Frontier Governance — Sign in</title>
<style>
:root{--highlight:#2FC7BA;--highlight-darker:#2ab3a7;--gray900:#212121;--gray700:#A1A1A1;
--gray200:#E7E7E7;--gray50:#F6F7F8;--error:#f44336}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
background:linear-gradient(135deg,#eafaf8,var(--gray50));font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--gray900)}
.card{background:#fff;border:1px solid var(--gray200);border-radius:16px;padding:2rem 1.9rem;width:22rem;
box-shadow:0 10px 40px -12px rgba(33,33,33,.22)}
.logo{width:46px;height:46px;border-radius:12px;background:linear-gradient(135deg,var(--highlight),#5ad6cb);
display:grid;place-items:center;color:#fff;font-weight:700;font-size:1.4rem;margin-bottom:1rem}
h2{margin:0 0 .15rem;font-size:1.25rem}p.sub{margin:0 0 1.4rem;color:var(--gray700);font-size:.85rem}
label{display:block;font-size:.78rem;font-weight:600;color:var(--gray700);margin:.7rem 0 .25rem}
input{width:100%;padding:.6rem .7rem;border:1px solid var(--gray200);border-radius:9px;font:inherit}
input:focus{outline:2px solid var(--highlight);border-color:transparent}
button{width:100%;margin-top:1.2rem;padding:.65rem;border:0;border-radius:9px;background:var(--highlight);
color:#fff;font-weight:650;font-size:.95rem;cursor:pointer}button:hover{background:var(--highlight-darker)}
#e{color:var(--error);font-size:.83rem;min-height:1.1rem;margin:.6rem 0 0;text-align:center}
.foot{margin:1rem 0 0;text-align:center;font-size:.82rem;color:var(--gray700)}
.foot a{color:var(--highlight);text-decoration:none;font-weight:600}
</style></head><body>
<form class=card id=f>
<div class=logo>F</div>
<h2>Frontier Governance</h2><p class=sub>Sign in to the control plane.</p>
<label for=u>Username</label><input id=u autofocus autocomplete=username>
<label for=p>Password</label><input id=p type=password autocomplete=current-password>
<button>Sign in</button><p id=e></p>
<p class=foot>No account? <a href=/dashboard/signup>Create one</a></p>
</form>
<script>f.onsubmit=async e=>{e.preventDefault();const r=await fetch('/dashboard/login',
{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});
if(r.ok){location='/dashboard'}else{const j=await r.json().catch(()=>({}));document.getElementById('e').textContent=j.error||'Sign in failed'}}</script>
</body></html>"""


def _session(request) -> dict | None:
    return verify_session(request.cookies.get(_COOKIE))


def _unauthorized(is_admin: bool = False):
    return JSONResponse({"error": "forbidden" if is_admin else "unauthorized"},
                        status_code=403 if is_admin else 401)


def _tool_info(names) -> list[dict]:
    """Non-technical view of a set of canonical tool names, for any UI a normal
    user (not an admin) looks at -- name + a plain-English description, falling
    back to the raw name if a tool somehow has none."""
    out = []
    for n in sorted(names):
        policy = manifest.get(n)
        out.append({
            "name": n,
            "description": (policy.description if policy else "") or n,
            "risk": policy.risk if policy else None,
            "approvalRequired": bool(policy.approval_required) if policy else False,
        })
    return out


def _effective_access_view(record) -> dict:
    """Human-readable view of a principal's resolved grant (for 'My Access')."""
    grant = resolve_grant(record, get_store().get_category, get_store().get_department)
    view = {}
    if grant.all_tools:
        for b in manifest.backends():
            view[b] = {"tools": _tool_info(manifest.tools_for_backend(b)), "levels": sorted(grant.levels_for(b))}
    else:
        for b, tools in grant.tools_by_backend.items():
            view[b] = {"tools": _tool_info(tools), "levels": sorted(grant.levels_for(b))}
    return view


def _effective_category_ids(store, record) -> set[str]:
    """Category ids this principal already effectively holds -- its own `categories`
    plus its department's CURRENT ones, if any (live, same as policy/resolve.py).
    Used to grey out "already granted" options in the self-service request-access
    picker; not a security boundary (that's still resolve()/decide())."""
    ids = set(record.categories or [])
    if record.department:
        dept = store.get_department(record.department)
        if dept:
            ids |= set(dept.categories)
    return ids


def _parse_tool_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"status": "error", "raw": raw}


def _require_admin(request):
    """Return (claims, None) for an admin session, else (None, error_response)."""
    claims = _session(request)
    if not claims:
        return None, _unauthorized()
    if claims.get("role") != "admin":
        return None, _unauthorized(is_admin=True)
    return claims, None


def _require_admin_or_category(request, category_id: str):
    """Like _require_admin, but also let a non-admin holding `category_id` through
    (expansion.md §7.1's workflow_admin/agent_admin -- ADDITIVE: the admin role
    always still works, this only widens who ELSE can reach the route)."""
    claims = _session(request)
    if not claims:
        return None, _unauthorized()
    if claims.get("role") == "admin":
        return claims, None
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is not None and category_id in _effective_category_ids(store, record):
        return claims, None
    return None, _unauthorized(is_admin=True)


def _consumer_public(r) -> dict:
    """Consumer view without secrets (never expose key_hash / password hash).
    `categories` is this consumer's OWN stored field (may be empty for a
    department-linked user by design -- see departments.py); `effective_categories`
    is what they ACTUALLY resolve to right now (own categories + their department's
    current ones, if any), so the admin UI doesn't read a department member's row
    as "somehow has zero access"."""
    store = get_store()
    grant = resolve_grant(r, store.get_category, store.get_department)
    return {
        "consumer_id": r.consumer_id, "name": r.name, "full_name": r.full_name, "department": r.department,
        "status": r.status,
        "type": r.type, "role": r.role, "categories": r.categories,
        # what this consumer ACTUALLY resolves to right now (own categories + their
        # department's current ones, if any) -- shown so a department member's row
        # doesn't read as "somehow has zero access" just because their own
        # `categories` field is empty by design (see departments.py).
        "effective_backends": ["*"] if grant.all_tools else sorted(grant.tools_by_backend.keys()),
        "rate_limit_per_hour": r.rate_limit_per_hour, "ip_allowlist": list(r.ip_allowlist),
        "overrides": r.overrides, "allowed_levels": sorted(r.allowed_levels),
        "has_key": bool(r.key_hash), "has_login": bool(r.login_password_hash),
    }


def _writable_or_error():
    store = get_store()
    if not store.writable:
        return None, JSONResponse(
            {"error": "policy store is read-only; set GOVERNANCE_STORE_FILE (or Cosmos) to enable editing"},
            status_code=409)
    return store, None


def _range_sec(request, default: int = 86400) -> int:
    return analytics.RANGES.get(request.query_params.get("range", "24h"), default)
