"""Per-session account scoping, owned by the governance agent.

This is the server-side replacement for the chatbot's old identity_context.py
globals. Account-scoped tools (orders, order total, contacts, addresses,
shipment-by-number) must never trust a customer_id argument at face value from
an untrusted caller for THOSE tools directly -- they resolve it from here,
keyed by session_id.

Current trust model mirrors what the chatbot did before this split (see
identity_context.py's "[US-REMOVAL]" note on the chatbot side): whichever
customer_id was last supplied for a session is trusted and remembered for the
rest of that session. That is a real, pre-existing relaxation, not something
introduced by this refactor -- flagging it because centralizing credential
custody here is exactly what makes it easy to tighten later (e.g. requiring a
live HubSpot existence check before trusting a customer_id for the first time
in a session) without touching every consumer.

In-memory only for the POC -- state resets on restart and does not share
across multiple instances. Swap for Redis/Cosmos before running more than one
replica.
"""
from __future__ import annotations

import time

# session_id -> {"customer_id", "first_seen", "last_seen", "consumer"}
_sessions: dict[str, dict] = {}


def note_customer_id(session_id: str | None, customer_id: str | None, *, consumer: str = "unknown") -> None:
    if not (session_id and customer_id):
        return
    now = time.time()
    record = _sessions.setdefault(session_id, {"first_seen": now})
    record["customer_id"] = customer_id.strip()
    record["last_seen"] = now
    record["consumer"] = consumer


def touch(session_id: str | None, *, consumer: str = "unknown") -> None:
    """Record activity on a session without changing its account scope."""
    if not session_id:
        return
    now = time.time()
    record = _sessions.setdefault(session_id, {"first_seen": now})
    record["last_seen"] = now
    record.setdefault("customer_id", None)
    record["consumer"] = consumer


def customer_id_for_session(session_id: str | None) -> str | None:
    if not session_id:
        return None
    record = _sessions.get(session_id)
    return record.get("customer_id") if record else None


def is_verified(session_id: str | None) -> bool:
    return bool(customer_id_for_session(session_id))


def snapshot(limit: int = 200) -> list[dict]:
    """Active sessions, most recently active first -- for the dashboard."""
    items = [
        {"session_id": sid, **record}
        for sid, record in _sessions.items()
    ]
    items.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
    return items[:limit]
