"""Layer 1 -- Governance Edge.

Answers "is this caller allowed to talk to the MCP server, and does the
traffic look safe?" -- BEFORE anything in app.py (Layer 2, "is this specific
tool call allowed") ever runs. Concretely:

  - agent authentication: each consumer gets its OWN API key (GOVERNANCE_KEY_*),
    not a self-reported header -- the key IS the identity from here on.
  - rate limits / quotas: a fixed-window call budget per consumer.
  - IP allowlist (optional): reject traffic from outside a configured CIDR set.
  - request size limit: reject oversized bodies before they reach a tool.
  - request-id: generate/propagate one per request for cross-log correlation.
  - traffic logging: every allow/deny decision here is audited, independent
    of whatever a tool call in Layer 2 later decides.

Deliberately NOT here: WAF-grade payload inspection (SQLi/XSS signatures) or
DDoS protection -- once this service sits behind a VNet Private Endpoint (or
in front of Azure API Management / Front Door), that belongs to the platform,
not hand-rolled here. What IS here still matters even with that in place --
identity, quotas, and audit are still this service's job.

In-memory rate-limit state is POC-appropriate: per-instance, resets on
restart. Move to Redis (or APIM's built-in quota policies) before running
more than one replica.
"""
from __future__ import annotations

import ipaddress
import os
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import audit
import request_context as ctx
from store import get_store

_DEFAULT_RATE_LIMIT_PER_HOUR = int(os.getenv("GOVERNANCE_DEFAULT_RATE_LIMIT_PER_HOUR") or "100")
_RATE_LIMIT_WINDOW_SEC = 3600
_MAX_BODY_BYTES = int(os.getenv("GOVERNANCE_MAX_BODY_BYTES") or str(64 * 1024))

# Paths that skip Layer 1 entirely: /health is an infra liveness probe, and
# /dashboard is just the static HTML shell (it calls /admin/* for data, which
# IS gated below).
PUBLIC_PATHS = {"/health", "/dashboard"}

# Read-only monitoring routes: still require a valid API key (below), but
# don't consume the caller's rate-limit quota. Without this, leaving the
# dashboard open with auto-refresh on (dashboard.html polls these every 3s)
# silently eats into the same budget real tool calls use, from whichever key
# was pasted in to view it -- including a production consumer's key.
_RATE_LIMIT_EXEMPT_PREFIXES = ("/admin/",)


def _load_ip_allowlist() -> list:
    raw = (os.getenv("GOVERNANCE_IP_ALLOWLIST") or "").strip()
    if not raw:
        return []
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            print(f"[edge] Ignoring invalid GOVERNANCE_IP_ALLOWLIST entry: {entry!r}")
    return networks


_IP_ALLOWLIST = _load_ip_allowlist()

# consumer name -> {"window_start": ts, "count": n}
_rate_state: dict[str, dict] = {}


def ip_allowed(ip: str) -> bool:
    # Global allowlist = env-configured networks + any set via the dashboard (store).
    networks = list(_IP_ALLOWLIST)
    try:
        networks += list(get_store().get_whitelist())
    except Exception:  # noqa: BLE001 -- store unavailable shouldn't harden-fail the env allowlist
        pass
    if not networks:
        return True
    return _ip_in(ip, networks)


def _ip_in(ip: str, networks) -> bool:
    """True if ip falls in any of `networks` (ip_network objects or CIDR/host strings)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        try:
            network = net if isinstance(net, (ipaddress.IPv4Network, ipaddress.IPv6Network)) \
                else ipaddress.ip_network(net, strict=False)
        except ValueError:
            continue
        if addr in network:
            return True
    return False


def check_rate_limit(consumer: str, limit_per_hour: int) -> tuple[bool, int, int]:
    """Returns (allowed, remaining_after_this_call, limit)."""
    now = time.time()
    state = _rate_state.setdefault(consumer, {"window_start": now, "count": 0})
    if now - state["window_start"] >= _RATE_LIMIT_WINDOW_SEC:
        state["window_start"] = now
        state["count"] = 0
    if state["count"] >= limit_per_hour:
        return False, 0, limit_per_hour
    state["count"] += 1
    return True, limit_per_hour - state["count"], limit_per_hour


def rate_limit_snapshot() -> list[dict]:
    """Current usage per consumer -- for the dashboard."""
    now = time.time()
    items = []
    known_limits = {c.name: c.rate_limit_per_hour for c in get_store().consumers()}
    for consumer, state in _rate_state.items():
        limit = known_limits.get(consumer, _DEFAULT_RATE_LIMIT_PER_HOUR)
        elapsed = now - state["window_start"]
        items.append({
            "consumer": consumer,
            "used": state["count"],
            "limit": limit,
            "window_resets_in_sec": max(0, round(_RATE_LIMIT_WINDOW_SEC - elapsed)),
        })
    items.sort(key=lambda r: r["used"] / max(r["limit"], 1), reverse=True)
    return items


class EdgeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Layer 1 (API-key auth + rate limit) guards ONLY the MCP endpoint -- machines.
        # The human dashboard surface (/dashboard*, /admin/*) authenticates with a
        # session cookie inside its own route handlers; /health is an open probe.
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)

        client_ip = ctx.client_ip(request)
        user_agent = request.headers.get("user-agent", "")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]

        store = get_store()
        if not store.consumers():
            audit.log_auth_denied(path=request.url.path, client_ip=client_ip, user_agent=user_agent,
                                   reason="server_misconfigured_no_keys")
            return JSONResponse({"error": "no governance API keys are configured on the server"}, status_code=500)

        if not ip_allowed(client_ip):
            audit.log_auth_denied(path=request.url.path, client_ip=client_ip, user_agent=user_agent,
                                   reason="ip_not_allowlisted")
            return JSONResponse({"error": "forbidden"}, status_code=403)

        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > _MAX_BODY_BYTES:
            audit.log_auth_denied(path=request.url.path, client_ip=client_ip, user_agent=user_agent,
                                   reason="request_too_large")
            return JSONResponse({"error": "request too large"}, status_code=413)

        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").removeprefix("bearer ").strip() or request.headers.get("x-governance-key", "")
        record = store.get_by_api_key(token)
        if record is None:
            audit.log_auth_denied(path=request.url.path, client_ip=client_ip, user_agent=user_agent,
                                   reason="bad_or_missing_key")
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        consumer = record.name

        # Per-principal IP allowlist (in addition to the global one), when set.
        if record.ip_allowlist and not _ip_in(client_ip, record.ip_allowlist):
            audit.log_auth_denied(path=request.url.path, client_ip=client_ip, user_agent=user_agent,
                                   reason="ip_not_allowlisted_for_consumer")
            return JSONResponse({"error": "forbidden"}, status_code=403)

        effective_limit = record.rate_limit_per_hour or _DEFAULT_RATE_LIMIT_PER_HOUR
        rate_limited_path = not request.url.path.startswith(_RATE_LIMIT_EXEMPT_PREFIXES)
        remaining = limit = effective_limit
        if rate_limited_path:
            allowed, remaining, limit = check_rate_limit(consumer, effective_limit)
            if not allowed:
                audit.log_rate_limited(tool=request.url.path, consumer=consumer, client_ip=client_ip,
                                        user_agent=user_agent, limit=limit)
                return JSONResponse(
                    {"error": "rate limit exceeded", "limit_per_hour": limit},
                    status_code=429,
                    headers={"Retry-After": str(_RATE_LIMIT_WINDOW_SEC)},
                )

        tok_consumer = ctx.consumer_ctx.set(consumer)
        tok_record = ctx.consumer_record_ctx.set(record)
        tok_ip = ctx.ip_ctx.set(client_ip)
        tok_ua = ctx.ua_ctx.set(user_agent)
        tok_rid = ctx.request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = request_id
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            return response
        finally:
            ctx.consumer_ctx.reset(tok_consumer)
            ctx.consumer_record_ctx.reset(tok_record)
            ctx.ip_ctx.reset(tok_ip)
            ctx.ua_ctx.reset(tok_ua)
            ctx.request_id_ctx.reset(tok_rid)
