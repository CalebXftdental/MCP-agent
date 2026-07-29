"""HTML pages and static assets: the SPA shell, legacy pages, logo, health probe."""
from __future__ import annotations

from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.responses import RedirectResponse
from starlette.responses import Response

from .deps import _APP_HTML, _CHAT_HTML, _FRONTEND_DIST_DIR, _LEGACY_APP_HTML, _LOGO_BYTES, _session

_FAVICON_PATH = _FRONTEND_DIST_DIR / "favicon.png"
_FAVICON_BYTES = _FAVICON_PATH.read_bytes() if _FAVICON_PATH.exists() else b""


async def _chat_page(request):
    if not _session(request):
        # The React shell renders its own sign-in view for an anonymous
        # session (see _app_shell below) rather than a separate login page.
        return HTMLResponse(_APP_HTML, headers={"Cache-Control": "no-store"})
    return HTMLResponse(_CHAT_HTML)


async def _health(_request):
    return JSONResponse({"status": "ok", "service": "governance-gateway"})


async def _app_shell(request):
    """The React SPA shell. Serves `/`, `/login`, and `/signup` -- the only
    page routes left that are actually meant to be navigated to directly --
    regardless of session state: the client checks /dashboard/me itself and
    renders sign-in/create-account when anonymous, the routed panel
    otherwise, hiding admin sections for non-admins (admin APIs enforce the
    role server-side regardless).

    /login and /signup are the canonical entry points for a signed-out
    visitor (LoginPage.tsx reads the path to decide which mode to default to
    and where its own mode-switch link points); `/` falls back to the same
    sign-in view for anyone who lands there anonymously (e.g. a session that
    expired mid-use)."""
    # no-store: the shell is an inline HTML string (no hashed asset URL to bust),
    # so without this a browser can keep showing a stale dashboard after a deploy.
    return HTMLResponse(_APP_HTML, headers={"Cache-Control": "no-store"})


async def _legacy_shell(request):
    """The pre-React shell, unconditionally -- unlike `_app_shell`, never
    prefers the built SPA. This is what `/legacy/<key>` serves: the escape
    hatch `PlaceholderPage` links to for a tab that hasn't been ported yet,
    which is otherwise unreachable once a dist build exists (`_app_shell`
    serves the React build for every /dashboard* path too, legacy or not)."""
    if not _session(request):
        return HTMLResponse(_APP_HTML, headers={"Cache-Control": "no-store"})
    return HTMLResponse(_LEGACY_APP_HTML, headers={"Cache-Control": "no-store"})


async def _dashboard_redirect(_request):
    # /dashboard is deprecated in favour of / -- kept as a redirect (not
    # removed outright) so an old bookmark or link still lands somewhere
    # correct instead of 404ing.
    return RedirectResponse(url="/", status_code=308)


def _dashboard_section_redirect(section: str):
    # /dashboard/<section> is deprecated in favour of the React shell's own
    # path routing (/<section> directly, now that tabs have real URLs).
    # Forwarding the intended tab (rather than a bare redirect to /) means an
    # old per-section bookmark still lands on the right one -- resolveRoute()
    # already turns an unrecognised or since-retired key into the Home
    # fallback, the same as it does for any other stale path, so nothing
    # section-specific needs handling here.
    async def _redirect(_request):
        return RedirectResponse(url=f"/{section}", status_code=308)

    return _redirect


async def _signup_redirect(_request):
    # /dashboard/signup is deprecated in favour of /signup -- kept as a
    # redirect (not removed outright) so an old bookmark or link still lands
    # somewhere correct instead of quietly defaulting to sign-in.
    return RedirectResponse(url="/signup", status_code=308)


async def _chat_redirect(_request):
    return RedirectResponse(url="/chat", status_code=308)


async def _logo_redirect(_request):
    return RedirectResponse(url="/logo.png", status_code=308)


_RESERVED_PREFIXES = ("/backend/", "/assets/")


async def _catch_all(request):
    """Last-resort route -- registered after every other one, so it only ever
    catches a path nothing more specific claimed. That's exactly the tab
    paths now that routing is path-based (`/monitor`, `/files`, ...): there's
    no per-tab route to register, since the client decides the panel from
    `location.pathname` itself (see useRoute.ts) and any of these paths
    should just get the same shell as `/`.

    Two things it deliberately does NOT do that as a naive catch-all:
      - Serve the shell for `/backend/*` or `/assets/*` -- those already have
        real routes for everything that exists; if this is reached for one
        anyway, the path genuinely doesn't exist and should 404, not silently
        become an HTML page that makes the real error harder to notice.
      - Serve the shell for what looks like a missing static file (a dotted
        last segment, e.g. a typo'd image or script path) -- same reasoning,
        a broken asset should read as a 404, not as this page's markup.
    """
    path = request.url.path
    if path.startswith(_RESERVED_PREFIXES):
        return Response(status_code=404)
    if "." in path.rsplit("/", 1)[-1]:
        return Response(status_code=404)
    return HTMLResponse(_APP_HTML, headers={"Cache-Control": "no-store"})


async def _logo(_request):
    if not _LOGO_BYTES:
        return Response(status_code=404)
    return Response(_LOGO_BYTES, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def _favicon(_request):
    if not _FAVICON_BYTES:
        return Response(status_code=404)
    return Response(_FAVICON_BYTES, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
