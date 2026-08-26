"""In-memory tickets for signup email verification: a 6-digit code sent to the
address the user typed, gating the rest of their signup fields until they prove
the mailbox is theirs. Per-instance/non-durable -- same caveat as lockout.py; a
restart just means the user asks for a new code.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid

_TTL_SEC = int(os.getenv("GOVERNANCE_SIGNUP_CODE_TTL_SEC") or "600")          # 10 min
_MAX_ATTEMPTS = int(os.getenv("GOVERNANCE_SIGNUP_CODE_MAX_ATTEMPTS") or "5")

# ticket_id -> {"email", "payload", "code_hash", "expires_at", "attempts"}
_tickets: dict[str, dict] = {}


def _code_hash(ticket_id: str, code: str) -> str:
    return hashlib.sha256(f"{ticket_id}:{code}".encode("utf-8")).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def create_ticket(email: str, payload: dict) -> tuple[str, str]:
    """Start a new verification ticket for `email`; returns (ticket_id, code)."""
    ticket_id = uuid.uuid4().hex
    code = _new_code()
    _tickets[ticket_id] = {
        "email": email, "payload": payload,
        "code_hash": _code_hash(ticket_id, code),
        "expires_at": time.time() + _TTL_SEC,
        "attempts": 0,
    }
    return ticket_id, code


def verify(ticket_id: str, code: str) -> dict | None:
    """Consume the ticket on a correct code, returning its payload. Returns None
    (and, once attempts/expiry are exhausted, drops the ticket) on any failure --
    bad ticket, expired, too many attempts, or wrong code -- without distinguishing
    which, so a guesser learns nothing from the response shape."""
    t = _tickets.get(ticket_id)
    if not t or time.time() > t["expires_at"]:
        _tickets.pop(ticket_id, None)
        return None
    if t["attempts"] >= _MAX_ATTEMPTS:
        _tickets.pop(ticket_id, None)
        return None
    t["attempts"] += 1
    if not hmac.compare_digest(t["code_hash"], _code_hash(ticket_id, code)):
        return None
    _tickets.pop(ticket_id, None)
    return t["payload"]


def resend(ticket_id: str) -> tuple[str, str] | None:
    """Issue a fresh code for an existing, unexpired ticket, resetting its expiry
    and attempt count. Returns (code, email), or None if the ticket is gone/expired."""
    t = _tickets.get(ticket_id)
    if not t or time.time() > t["expires_at"]:
        _tickets.pop(ticket_id, None)
        return None
    code = _new_code()
    t["code_hash"] = _code_hash(ticket_id, code)
    t["expires_at"] = time.time() + _TTL_SEC
    t["attempts"] = 0
    return code, t["email"]
