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
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import audit
import request_context as ctx

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


def _load_consumers() -> dict[str, dict]:
    """Build key -> {"name", "rate_limit_per_hour"} from env.

    Per-consumer: GOVERNANCE_KEY_<NAME>=<secret>, optionally paired with
    GOVERNANCE_RATE_LIMIT_<NAME>=<calls per hour> (defaults to
    GOVERNANCE_DEFAULT_RATE_LIMIT_PER_HOUR if omitted). Example:
        GOVERNANCE_KEY_CHATBOT=...        GOVERNANCE_RATE_LIMIT_CHATBOT=1000
        GOVERNANCE_KEY_EMAIL_BOT=...      GOVERNANCE_RATE_LIMIT_EMAIL_BOT=500

    GOVERNANCE_SHARED_SECRET is kept as a migration fallback -- a single key
    mapped to consumer "legacy" at the default rate limit. Retire it once
    every real consumer has its own GOVERNANCE_KEY_*.
    """
    consumers: dict[str, dict] = {}
    for env_name, value in os.environ.items():
        match = re.match(r"^GOVERNANCE_KEY_(.+)$", env_name)
        if not match or not value.strip():
            continue
        suffix = match.group(1)
        limit = int(os.getenv(f"GOVERNANCE_RATE_LIMIT_{suffix}") or _DEFAULT_RATE_LIMIT_PER_HOUR)
        consumers[value.strip()] = {"name": suffix.lower(), "rate_limit_per_hour": limit}

    legacy_secret = (os.getenv("GOVERNANCE_SHARED_SECRET") or "").strip()
    if legacy_secret and legacy_secret not in consumers:
        consumers[legacy_secret] = {"name": "legacy", "rate_limit_per_hour": _DEFAULT_RATE_LIMIT_PER_HOUR}

    return consumers


def _load_ip_allowlist() -> list:
    raw = (os.getenv("GOVERNANCE_IP_ALLOWLIST") or "").strip()
    if not raw:
        return []
    networks = []
    for entry in re.split(r"[,\s]+", raw):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            print(f"[edge] Ignoring invalid GOVERNANCE_IP_ALLOWLIST entry: {entry!r}")
    return networks


_CONSUMERS = _load_consumers()
_IP_ALLOWLIST = _load_ip_allowlist()

# consumer name -> {"window_start": ts, "count": n}
_rate_state: dict[str, dict] = {}


def ip_allowed(ip: str) -> bool:
    if not _IP_ALLOWLIST:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in _IP_ALLOWLIST)


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
    known_limits = {info["name"]: info["rate_limit_per_hour"] for info in _CONSUMERS.values()}
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
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        client_ip = ctx.client_ip(request)
        user_agent = request.headers.get("user-agent", "")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]

        if not _CONSUMERS:
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
        consumer_info = _CONSUMERS.get(token)
        if consumer_info is None:
            audit.log_auth_denied(path=request.url.path, client_ip=client_ip, user_agent=user_agent,
                                   reason="bad_or_missing_key")
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        consumer = consumer_info["name"]
        rate_limited_path = not request.url.path.startswith(_RATE_LIMIT_EXEMPT_PREFIXES)
        remaining = limit = consumer_info["rate_limit_per_hour"]
        if rate_limited_path:
            allowed, remaining, limit = check_rate_limit(consumer, consumer_info["rate_limit_per_hour"])
            if not allowed:
                audit.log_rate_limited(tool=request.url.path, consumer=consumer, client_ip=client_ip,
                                        user_agent=user_agent, limit=limit)
                return JSONResponse(
                    {"error": "rate limit exceeded", "limit_per_hour": limit},
                    status_code=429,
                    headers={"Retry-After": str(_RATE_LIMIT_WINDOW_SEC)},
                )

        tok_consumer = ctx.consumer_ctx.set(consumer)
        tok_ip = ctx.ip_ctx.set(client_ip)
        tok_ua = ctx.ua_ctx.set(user_agent)
        tok_rid = ctx.request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
            if response.status_code >= 500:
                # Auth/quota already passed (no auth_denied to log) and no
                # @mcp.tool() handler ran (no call/denied to log either) -- this
                # is the MCP transport itself (e.g. the SDK's own initialize
                # handling) failing beneath both layers. Without this, that
                # failure class never appears on /dashboard.
                audit.log_transport_error(path=request.url.path, consumer=consumer, client_ip=client_ip,
                                           user_agent=user_agent, status_code=response.status_code)
            response.headers["X-Request-Id"] = request_id
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            return response
        finally:
            ctx.consumer_ctx.reset(tok_consumer)
            ctx.ip_ctx.reset(tok_ip)
            ctx.ua_ctx.reset(tok_ua)
            ctx.request_id_ctx.reset(tok_rid)
