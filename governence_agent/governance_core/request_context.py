"""Per-request identity, set by edge.py's EdgeMiddleware (Layer 1) and read
by app.py's tool handlers and audit calls (Layer 2). Shared module so neither
layer imports the other.
"""
from __future__ import annotations

from contextvars import ContextVar

consumer_ctx: ContextVar[str] = ContextVar("governance_consumer", default="unknown")
ip_ctx: ContextVar[str] = ContextVar("governance_client_ip", default="unknown")
ua_ctx: ContextVar[str] = ContextVar("governance_user_agent", default="")
request_id_ctx: ContextVar[str] = ContextVar("governance_request_id", default="")
# The resolved ConsumerRecord for this request (set by EdgeMiddleware after auth),
# so the PDP can read the principal's entitlements without a second store lookup.
consumer_record_ctx: ContextVar = ContextVar("governance_consumer_record", default=None)


def _strip_port(value: str) -> str:
    """Azure's own X-Forwarded-For (and some proxies) append ":<port>" to the
    client IP -- e.g. "99.213.5.186:64497". Left in place, that string never
    matches any CIDR/host entry in an IP allowlist (ipaddress.ip_address()
    raises on it), so every allowlist check silently fails closed no matter
    what's configured. Bare IPv6 has 2+ colons and no brackets here (this
    runs before any "[addr]:port" bracketing would be added), so a lone colon
    reliably means "IPv4:port"."""
    value = value.strip()
    if value.startswith("["):
        return value[1:].split("]")[0]
    if value.count(":") == 1:
        host, _, port = value.rpartition(":")
        if port.isdigit():
            return host
    return value


def client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return _strip_port(forwarded.split(",")[0].strip())
    return request.client.host if request.client else "unknown"
