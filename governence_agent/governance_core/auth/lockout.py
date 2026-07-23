"""In-memory brute-force limiter for the login route.

Keyed by "<username>|<ip>": after MAX_FAILURES failed attempts within WINDOW, the
key is locked for LOCKOUT. A success resets it. POC/in-memory (per-instance, resets
on restart) -- move to Redis alongside the rate-limit counters before running >1
replica (same note as edge.py).
"""
from __future__ import annotations

import os
import time

_MAX_FAILURES = int(os.getenv("GOVERNANCE_LOGIN_MAX_FAILURES") or "5")
_WINDOW_SEC = int(os.getenv("GOVERNANCE_LOGIN_WINDOW_SEC") or "900")       # 15 min
_LOCKOUT_SEC = int(os.getenv("GOVERNANCE_LOGIN_LOCKOUT_SEC") or "900")     # 15 min

# key -> {"count": n, "first": ts, "locked_until": ts}
_state: dict[str, dict] = {}


def is_locked(key: str) -> bool:
    st = _state.get(key)
    if not st:
        return False
    return st.get("locked_until", 0) > time.time()


def record_failure(key: str) -> bool:
    """Record a failed attempt; return True if this trips a lockout."""
    now = time.time()
    st = _state.get(key)
    if not st or now - st.get("first", now) > _WINDOW_SEC:
        st = {"count": 0, "first": now, "locked_until": 0}
    st["count"] += 1
    if st["count"] >= _MAX_FAILURES:
        st["locked_until"] = now + _LOCKOUT_SEC
    _state[key] = st
    return st["locked_until"] > now


def reset(key: str) -> None:
    _state.pop(key, None)
