"""Structured audit logging for every governance-agent request.

Every credentialed operation funnels through this one service, which makes a
single audit chokepoint cheap. Records go to two places:
  - stdout, as structured JSON lines (captured by the App Service log stream
    / any log aggregator) -- the durable record.
  - an in-memory ring buffer (`recent()`), which powers the /admin/calls
    endpoint and the /dashboard monitoring page. This is a POC convenience,
    not a source of truth: it resets on restart and does not span replicas.
    Swap `_emit`/`recent()` for a real sink (Cosmos, Log Analytics, etc.)
    without touching call sites.

Event types share one buffer/schema (`type` distinguishes them):
  - "call"          a tool call that reached a Layer 2 handler (ok or error)
  - "denied"        a tool call rejected by Layer 2 policy (e.g. missing scope)
  - "auth_denied"   a Layer 1 request rejected before reaching any tool --
                    bad/missing key, IP not allowlisted, oversized body, or a
                    misconfigured server. The signal that matters most for
                    "is someone hitting this service who shouldn't be".
  - "rate_limited"  a Layer 1 request from a known, authenticated consumer
                    that exceeded its quota.
"""
from __future__ import annotations

import json
import time
from collections import deque

import request_context as ctx

_MAX_RECORDS = 1000
_records: deque[dict] = deque(maxlen=_MAX_RECORDS)


def _emit(record: dict) -> None:
    _records.append(record)
    print(json.dumps(record, default=str), flush=True)


def recent(limit: int = 200) -> list[dict]:
    """Most recent events first, capped at `limit`."""
    items = list(_records)[-limit:]
    items.reverse()
    return items


def summarize_args(args: dict, *, max_len: int = 60) -> str:
    """Compact, human-scannable view of tool args for the audit log.

    Drops empty values (most args are optional and blank) and truncates long
    ones -- this is for debugging "why did this call behave that way" from the
    dashboard, not a full payload dump.
    """
    parts = []
    for key, value in (args or {}).items():
        if key == "session_id" or value in (None, "", []):
            continue
        text = str(value)
        if len(text) > max_len:
            text = text[:max_len] + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _request_id() -> str | None:
    return ctx.request_id_ctx.get() or None


def log_call(
    *,
    tool: str,
    session_id: str | None,
    status: str,
    latency_ms: float,
    consumer: str = "unknown",
    client_ip: str | None = None,
    user_agent: str | None = None,
    args_summary: str = "",
    detail: str | None = None,
) -> None:
    _emit({
        "ts": time.time(),
        "type": "call",
        "request_id": _request_id(),
        "consumer": consumer,
        "client_ip": client_ip,
        "user_agent": user_agent,
        "tool": tool,
        "session_id": session_id,
        "args": args_summary,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "detail": detail,
    })


def log_denied(
    *,
    tool: str,
    session_id: str | None,
    reason: str,
    consumer: str = "unknown",
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    _emit({
        "ts": time.time(),
        "type": "denied",
        "request_id": _request_id(),
        "consumer": consumer,
        "client_ip": client_ip,
        "user_agent": user_agent,
        "tool": tool,
        "session_id": session_id,
        "args": "",
        "status": "denied",
        "latency_ms": 0.0,
        "detail": reason,
    })


def log_auth_denied(*, path: str, client_ip: str | None, user_agent: str | None, reason: str) -> None:
    _emit({
        "ts": time.time(),
        "type": "auth_denied",
        "request_id": None,
        "consumer": "unauthenticated",
        "client_ip": client_ip,
        "user_agent": user_agent,
        "tool": path,
        "session_id": None,
        "args": "",
        "status": "unauthorized",
        "latency_ms": 0.0,
        "detail": reason,
    })


def log_rate_limited(*, tool: str, consumer: str, client_ip: str | None, user_agent: str | None, limit: int) -> None:
    _emit({
        "ts": time.time(),
        "type": "rate_limited",
        "request_id": None,
        "consumer": consumer,
        "client_ip": client_ip,
        "user_agent": user_agent,
        "tool": tool,
        "session_id": None,
        "args": "",
        "status": "rate_limited",
        "latency_ms": 0.0,
        "detail": f"exceeded {limit}/hour",
    })
