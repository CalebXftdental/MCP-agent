"""Ephemeral per-session cache of the last row-capped Home-chat bulk result.

Exists so `export_bulk_result_to_excel` (gateway/app.py) can hand the user
the FULL data behind a capped result without either (a) the LLM re-fetching
and re-specifying the original call, or (b) the full row set ever re-entering
the model's own token context a second time -- it's pulled straight from here,
in Python, the same "move bulk data without an LLM round-trip" property real
workflow-graph execution already relies on.

In-memory only, single-process Stage 1 shape -- same scope as scope_store.py's
session/customer_id binding and the in-memory rate-limit counters (see
startup.sh's "ONE INSTANCE ONLY" note: do not scale out until this moves to
Redis/Cosmos). A short TTL bounds memory growth from abandoned sessions
without needing an explicit eviction pass.
"""
from __future__ import annotations

import time

_TTL_SEC = 15 * 60  # generous enough for a "yes, export the rest" follow-up turn
_cache: dict[str, dict] = {}


def remember(session_id: str, field_name: str, full_result: dict, total_matched: int) -> None:
    """`full_result` is the FULL (untruncated, already-redacted) backend result
    dict -- the same one about to be truncated for display. `field_name` is
    which top-level key in it holds the oversized list."""
    _cache[session_id] = {
        "field_name": field_name, "result": full_result,
        "total_matched": total_matched, "ts": time.time(),
    }


def get(session_id: str) -> dict | None:
    entry = _cache.get(session_id)
    if entry is None:
        return None
    if time.time() - entry["ts"] > _TTL_SEC:
        _cache.pop(session_id, None)
        return None
    return entry


def clear(session_id: str) -> None:
    _cache.pop(session_id, None)
