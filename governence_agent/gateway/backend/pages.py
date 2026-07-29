"""HTML pages and static assets: the SPA shell, legacy pages, logo, health probe."""
from __future__ import annotations

from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.responses import RedirectResponse
from starlette.responses import Response

from .deps import _APP_HTML, _CHAT_HTML, _FRONTEND_DIST_DIR, _LOGIN_HTML, _LOGO_BYTES, _SIGNUP_HTML, _session

_FAVICON_PATH = _FRONTEND_DIST_DIR / "favicon.svg"
_FAVICON_BYTES = _FAVICON_PATH.read_bytes() if _FAVICON_PATH.exists() else b""


async def _chat_page(request):
    if not _session(request):
        return HTMLResponse(_LOGIN_HTML, status_code=401)
    return HTMLResponse(_CHAT_HTML)


async def _root(_request):
    # The gateway serves no page at "/"; send browsers to the dashboard.
    return RedirectResponse(url="/dashboard")


async def _health(_request):
    return JSONResponse({"status": "ok", "service": "governance-gateway"})


async def _dashboard(request):
    """Single themed app shell (left-nav SPA). Serves every /dashboard[/section]
    path; the client renders the panel from the URL and hides admin sections for
    non-admins (admin APIs enforce the role server-side regardless)."""
    if not _session(request):
        return HTMLResponse(_LOGIN_HTML, status_code=401)
    # no-store: the shell is an inline HTML string (no hashed asset URL to bust),
    # so without this a browser can keep showing a stale dashboard after a deploy.
    return HTMLResponse(_APP_HTML, headers={"Cache-Control": "no-store"})


async def _signup_page(_request):
    return HTMLResponse(_SIGNUP_HTML)


async def _logo(_request):
    if not _LOGO_BYTES:
        return Response(status_code=404)
    return Response(_LOGO_BYTES, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def _favicon(_request):
    if not _FAVICON_BYTES:
        return Response(status_code=404)
    return Response(_FAVICON_BYTES, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
