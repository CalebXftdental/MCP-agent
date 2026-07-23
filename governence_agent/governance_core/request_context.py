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


def client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
