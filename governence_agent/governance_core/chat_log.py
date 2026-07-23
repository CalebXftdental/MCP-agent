"""Chat session persistence + idle-expiry + summarization.

A "session" here is one conversation (one browser tab's worth), keyed by the
`conversation_id` the dashboard chat sends -- the same id already used to scope
account lookups in scope_store.py. This module is the thin layer between the
gateway's /chat handler and the PolicyStore's chat_sessions read/write surface
(store/base.py + Local/File/Cosmos implementations).

Idle model: a session idle for GOVERNANCE_CHAT_IDLE_SEC (default 1h) is closed and
summarized. Two paths do that, both funneling through `_close_and_summarize`:
  - the background sweep (gateway/app.py's periodic task) -- the general guarantee,
    catches sessions the user never returns to.
  - `resolve_session_id`, called at the top of every /chat request -- if the
    request's own session already outlived the idle window before the sweep got to
    it, close it right there and hand back a NEW session id for this turn, so a
    stale hour-old conversation never bleeds into what looks like a fresh one.

`history_for_llm` intentionally returns only user/assistant turns (no raw tool-call
wire messages) -- enough for conversational continuity without re-feeding a new
turn's tool loop the previous turn's tool mechanics.
"""
from __future__ import annotations

import time
import uuid
from typing import Callable

from store.models import ChatMessage, ChatSession

_MAX_SUMMARY_INPUT_MESSAGES = 40
_MAX_SUMMARY_INPUT_CHARS = 6000

_SUMMARY_SYSTEM_PROMPT = (
    "Summarize the following internal conversation between a Frontier Dental "
    "employee and the data assistant in 1-2 plain sentences: what the employee "
    "asked about and what was found or done. No preamble, no bullet points."
)


def _new_session(session_id: str, consumer_id: str, now: float) -> ChatSession:
    return ChatSession(session_id=session_id, consumer_id=consumer_id,
                       status="open", created_at=now, last_active_at=now)


def _fork_id(session_id: str) -> str:
    return f"{session_id}:{uuid.uuid4().hex[:8]}"


def _summarize(session: ChatSession, llm_complete: Callable | None) -> str:
    if not session.messages:
        return "(empty session)"
    if llm_complete is None:
        return f"({len(session.messages)} messages — summary unavailable, assistant not configured)"
    transcript_lines = []
    for m in session.messages[-_MAX_SUMMARY_INPUT_MESSAGES:]:
        transcript_lines.append(f"{m.role}: {m.content}")
    transcript = "\n".join(transcript_lines)[-_MAX_SUMMARY_INPUT_CHARS:]
    try:
        msg = llm_complete(
            [{"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
             {"role": "user", "content": transcript}],
            None,
        )
        summary = (getattr(msg, "content", "") or "").strip()
        return summary or "(summary unavailable)"
    except Exception as exc:  # summarization must never break the request/sweep path
        return f"(summary failed: {exc})"


def _close_and_summarize(store, session: ChatSession, llm_complete: Callable | None, now: float) -> None:
    closed = ChatSession(
        session_id=session.session_id, consumer_id=session.consumer_id, status="closed",
        created_at=session.created_at, last_active_at=session.last_active_at, closed_at=now,
        summary=_summarize(session, llm_complete), messages=session.messages,
    )
    store.upsert_chat_session(closed)


def resolve_session_id(store, requested_id: str, consumer_id: str, idle_seconds: int,
                       llm_complete: Callable | None) -> str:
    """The session id THIS turn should use: `requested_id` continued, or a fresh
    fork if it's already closed or idle-expired.

    Uses get_chat_session_for (owner-scoped), not get_chat_session -- on Cosmos
    that's a partition-key point read (cheap regardless of how much history
    exists), and a session that turns out to belong to someone else (client id
    collision -- astronomically unlikely, not cryptographically random) reads back
    as "missing" and this consumer just starts a fresh one under the same id. On
    Cosmos that's genuinely safe (consumer_id is the partition key, so the two
    coexist as separate documents); on the Local/File backends (single flat dict
    keyed by session_id only) it would overwrite the other owner's record, which
    is an accepted, negligible-probability gap rather than a designed guarantee."""
    if not requested_id:
        return _fork_id("chat")
    existing = store.get_chat_session_for(requested_id, consumer_id)
    if existing is None:
        return requested_id
    if existing.status == "closed":
        return _fork_id(requested_id)
    if (time.time() - existing.last_active_at) > idle_seconds:
        _close_and_summarize(store, existing, llm_complete, time.time())
        return _fork_id(requested_id)
    return requested_id


def history_for_llm(store, session_id: str, consumer_id: str) -> list[dict]:
    session = store.get_chat_session_for(session_id, consumer_id)
    if session is None:
        return []
    return [{"role": m.role, "content": m.content} for m in session.messages]


def record_turn(store, session_id: str, consumer_id: str, role: str, content: str,
                tools_used: list[str] | None = None) -> None:
    now = time.time()
    session = store.get_chat_session_for(session_id, consumer_id) or _new_session(session_id, consumer_id, now)
    messages = list(session.messages) + [ChatMessage(role=role, content=content, ts=now,
                                                      tools_used=list(tools_used or []))]
    updated = ChatSession(
        session_id=session.session_id, consumer_id=session.consumer_id, status="open",
        created_at=session.created_at, last_active_at=now, closed_at=None,
        summary=session.summary, messages=messages,
    )
    store.upsert_chat_session(updated)


def close_idle_sessions(store, idle_seconds: int, llm_complete: Callable | None) -> int:
    """The background sweep: close + summarize every open session idle past the
    cutoff. Returns how many it closed (for logging)."""
    now = time.time()
    closed = 0
    for session in store.list_open_chat_sessions():
        if (now - session.last_active_at) > idle_seconds:
            _close_and_summarize(store, session, llm_complete, now)
            closed += 1
    return closed


def list_history_for(store, consumer_id: str) -> list[dict]:
    """Compact, newest-first view for the History panel / API -- one row per
    session (both open and closed), no message bodies."""
    return [
        {
            "session_id": s.session_id, "status": s.status,
            "created_at": s.created_at, "last_active_at": s.last_active_at,
            "closed_at": s.closed_at, "summary": s.summary,
            "message_count": len(s.messages),
        }
        for s in store.list_chat_sessions_for(consumer_id)
    ]


def get_transcript(store, session_id: str) -> ChatSession | None:
    """Full session incl. messages. Caller is responsible for the ownership check
    (compare .consumer_id against the requester) before returning it over the API."""
    return store.get_chat_session(session_id)


def resume_session(store, session: ChatSession) -> str:
    """'Continue this conversation' from the History panel. An OPEN session is
    already resumable as-is (its own id, unchanged) -- the caller should just reuse
    session.session_id directly and never call this for that case. A CLOSED
    session is never reopened under its own id (resolve_session_id would just fork
    it again on the next message anyway, per its own docstring) -- instead this
    starts a fresh OPEN session pre-seeded with the closed one's messages, so the
    next real turn has full continuity even though the old id stays permanently
    closed with its summary intact as history."""
    new_id = _fork_id(session.session_id)
    now = time.time()
    store.upsert_chat_session(ChatSession(
        session_id=new_id, consumer_id=session.consumer_id, status="open",
        created_at=now, last_active_at=now, messages=list(session.messages),
    ))
    return new_id
