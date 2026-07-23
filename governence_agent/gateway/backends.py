"""MCP client pool -- the gateway's connection to backend MCP servers.

The gateway is an MCP *client* here: for each governed tool call it opens a
Streamable HTTP MCP session to the owning backend (mcp-minierp-orders,
-accounts, -shipments, or -finance), calls the canonical tool, and returns the
backend's raw text result (a JSON string).

Backends run stateless_http, so a fresh connect + initialize per call is correct
and cheap enough for Stage 1. Backend URLs come from env, e.g.:
    MINIERP_ORDERS_MCP_URL     default http://localhost:8021/mcp
    MINIERP_FINANCE_MCP_URL    default http://localhost:8022/mcp
    MINIERP_ACCOUNTS_MCP_URL   default http://localhost:8023/mcp
    MINIERP_SHIPMENTS_MCP_URL  default http://localhost:8024/mcp

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
        return {"ok": False, "latency_ms": None, "error": str(exc)}


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


async def call(backend: str, tool: str, args: dict) -> str:
    """Call `tool` on `backend` with `args`; return its raw text result."""
    url = backend_url(backend)

    async def _invoke() -> object:
        async with streamablehttp_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await session.call_tool(tool, arguments=args)

    try:
        result = await asyncio.wait_for(_invoke(), timeout=_timeout_sec())
    except asyncio.TimeoutError as exc:
        raise BackendError(f"{backend}.{tool} timed out after {_timeout_sec()}s") from exc
    except Exception as exc:  # noqa: BLE001 -- normalize every failure to fail-closed
        raise BackendError(f"{backend}.{tool} call failed: {exc}") from exc

    if getattr(result, "isError", False):
        raise BackendError(f"{backend}.{tool} returned an error: {_extract_text(result)[:300]}")
    return _extract_text(result)
