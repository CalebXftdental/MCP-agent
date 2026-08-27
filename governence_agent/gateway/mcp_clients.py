"""MCP client pool -- the gateway's OUTBOUND connections to backend MCP servers.

The direction is the thing to keep straight: this is the gateway acting as an MCP
*client*, calling servers it depends on. The `backend/` package next door is the
opposite direction -- the HTTP API this gateway *serves* to browsers. (This module
was called `backends.py` until those two sat side by side and the names became a
trap.)

For each governed tool call it opens a Streamable HTTP MCP session to the owning
backend, calls the canonical tool, and returns the backend's raw text result (a
JSON string).

Backends run stateless_http, so a fresh connect + initialize per call is correct
and cheap enough for Stage 1. Backend URLs come from env -- see _DEFAULT_URLS
below for the full set and MINIERP_MCP_URL for the consolidated ERP server.

A per-call timeout bounds a hung backend; failures raise BackendError, which the
gateway turns into an audited error result (fail closed -- never a passthrough).
"""
from __future__ import annotations

import asyncio
import os
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class BackendError(RuntimeError):
    """A backend call failed (transport, timeout, or tool-level error)."""


def _leaves(exc: BaseException) -> list[BaseException]:
    """Flatten an ExceptionGroup tree down to the exceptions that actually failed."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def describe_error(exc: BaseException) -> str:
    """Human-readable cause, unwrapped from anyio's task-group envelope.

    streamablehttp_client runs its transport inside a task group, so an ordinary
    connection refusal arrives as "unhandled errors in a TaskGroup (1
    sub-exception)" -- true, and useless in a health panel. Descend to the real
    leaf exceptions and name their types, deduped, so "backend is not running"
    reads as `ConnectDenied: [WinError 1225] ...` instead.
    """
    seen: dict[str, None] = {}
    for leaf in _leaves(exc):
        text = str(leaf).strip()
        seen.setdefault(f"{type(leaf).__name__}: {text}" if text else type(leaf).__name__, None)
    return "; ".join(seen) or type(exc).__name__


# The four miniERP domains are now served by ONE consolidated backend
# (mcp-minierp) on a single URL. The per-domain `backend` tags survive in
# policy/manifest.py (they route/authorize by domain), but all resolve to the
# same server here. A single MINIERP_MCP_URL env var configures it in prod;
# the legacy per-domain <BACKEND>_MCP_URL vars still override if set.
_DEFAULT_MINIERP_URL = "http://localhost:8021/mcp"

_DEFAULT_URLS = {
    "minierp_orders": _DEFAULT_MINIERP_URL,
    "minierp_finance": _DEFAULT_MINIERP_URL,
    "minierp_accounts": _DEFAULT_MINIERP_URL,
    "minierp_shipments": _DEFAULT_MINIERP_URL,
    "minierp_analytics": _DEFAULT_MINIERP_URL,
    "office": "http://localhost:8030/mcp",
    "email": "http://localhost:8040/mcp",
    "knowledge": "http://localhost:8050/mcp",
    "calendar": "http://localhost:8060/mcp",
    "code": "http://localhost:8070/mcp",
}


def backend_url(backend: str) -> str:
    env_key = f"{backend.upper()}_MCP_URL"
    url = os.getenv(env_key)
    if not url and backend.startswith("minierp_"):
        url = os.getenv("MINIERP_MCP_URL")
    url = url or _DEFAULT_URLS.get(backend)
    if not url:
        raise BackendError(f"No URL configured for backend {backend!r} (set {env_key} or MINIERP_MCP_URL)")
    return url


def _timeout_sec() -> float:
    return float(os.getenv("GATEWAY_BACKEND_TIMEOUT_SEC", "30"))


def _probe_timeout_sec() -> float:
    # Health probes should give up fast -- a hung backend shouldn't stall the
    # admin dashboard for the full call timeout.
    return float(os.getenv("GATEWAY_BACKEND_PROBE_TIMEOUT_SEC", "5"))


async def ping(backend: str, timeout: float | None = None) -> dict:
    """Liveness probe: open a session and `initialize()` only (no tool call).

    Returns {"ok": bool, "latency_ms": float|None, "error": str|None}; never
    raises -- a probe failing is data, not an exception.
    """
    url = backend_url(backend)
    t = timeout if timeout is not None else _probe_timeout_sec()
    start = time.monotonic()

    async def _init() -> None:
        async with streamablehttp_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

    try:
        await asyncio.wait_for(_init(), timeout=t)
        return {"ok": True, "latency_ms": round((time.monotonic() - start) * 1000, 1), "error": None}
    except asyncio.TimeoutError:
        return {"ok": False, "latency_ms": None, "error": f"probe timed out after {t}s"}
    except Exception as exc:  # noqa: BLE001 -- normalize every failure to a result
        # Catches ExceptionGroup too (it subclasses Exception when every leaf is an
        # ordinary Exception, which is the transport-failure case). A bare
        # BaseExceptionGroup carrying e.g. CancelledError deliberately propagates.
        return {"ok": False, "latency_ms": None, "error": describe_error(exc)}


def _extract_text(result) -> str:
    """Pull the text payload out of an MCP CallToolResult."""
    content = getattr(result, "content", None) or []
    for part in content:
        text = getattr(part, "text", None)
        if text is not None:
            return text
    # Some servers only populate structuredContent.
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        import json
        return json.dumps(structured)
    return ""


async def get_tool_schema(backend: str, tool: str) -> dict | None:
    """Fetch `tool`'s outputSchema straight from `backend`'s own MCP server.

    Metadata only -- not a governed data call, so this deliberately bypasses
    _govern (same precedent as the gateway's list_my_workflows/get_my_workflow
    meta tools, which never touch real customer data either). Returns None if
    the backend doesn't declare an outputSchema for this tool, or the tool
    isn't found -- most tools today, since only mcp-minierp's finance domain
    has real outputSchema so far (see finalize_stage_1.md's rollout notes).
    """
    url = backend_url(backend)

    async def _list() -> object:
        async with streamablehttp_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await session.list_tools()

    try:
        result = await asyncio.wait_for(_list(), timeout=_timeout_sec())
    except asyncio.TimeoutError as exc:
        raise BackendError(f"{backend} list_tools timed out after {_timeout_sec()}s") from exc
    except Exception as exc:  # noqa: BLE001 -- normalize every failure to fail-closed
        raise BackendError(f"{backend} list_tools failed: {describe_error(exc)}") from exc

    for t in getattr(result, "tools", None) or []:
        if t.name == tool:
            return getattr(t, "outputSchema", None)
    return None


async def call(backend: str, tool: str, args: dict, timeout: float | None = None) -> str:
    """Call `tool` on `backend` with `args`; return its raw text result.

    `timeout` overrides GATEWAY_BACKEND_TIMEOUT_SEC for this one call -- for
    most tools the default (30s) is well above anything they actually take, but
    `ingest_my_document` (mcp-knowledge) can run PDF text extraction through
    Azure Document Intelligence, whose own poller is allowed up to
    GOVERNANCE_DOCINTEL_TIMEOUT_SEC (default 60s) -- a caller doing that specific
    call needs a longer budget here or it gets cut off before DI's own timeout
    ever has a chance to fire.
    """
    url = backend_url(backend)
    effective_timeout = timeout if timeout is not None else _timeout_sec()

    async def _invoke() -> object:
        async with streamablehttp_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await session.call_tool(tool, arguments=args)

    try:
        result = await asyncio.wait_for(_invoke(), timeout=effective_timeout)
    except asyncio.TimeoutError as exc:
        raise BackendError(f"{backend}.{tool} timed out after {effective_timeout}s") from exc
    except Exception as exc:  # noqa: BLE001 -- normalize every failure to fail-closed
        raise BackendError(f"{backend}.{tool} call failed: {describe_error(exc)}") from exc

    if getattr(result, "isError", False):
        raise BackendError(f"{backend}.{tool} returned an error: {_extract_text(result)[:300]}")
    return _extract_text(result)
