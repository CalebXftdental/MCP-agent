"""Governed chat: turn handling, SSE streaming, transcripts, feedback, idle sweep."""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from store import get_store
import asyncio
import audit
import chat_log
import json
import llm_broker
import orchestrator
import request_context as ctx
import sys

from mcp_server import mcp
from .deps import _CHAT_IDLE_SEC, _CHAT_SWEEP_INTERVAL_SEC, _session, _unauthorized


async def _chat(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    requested_id = str(body.get("conversation_id") or f"chat:{record.consumer_id}")
    # Bind the principal so governed tool calls resolve + audit under this user
    # (session-internal: the human IS the principal; no API key on this path).
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set(ctx.client_ip(request))

    store = get_store()
    llm_complete = orchestrator.default_llm_complete()
    # If the caller's own conversation_id already went idle (or was closed by the
    # sweep, or belongs to someone else), this hands back a fresh id for the turn
    # below rather than resuming stale/foreign context.
    session_id = await chat_log.resolve_session_id(store, requested_id, record.consumer_id, _CHAT_IDLE_SEC, llm_complete)
    history = chat_log.history_for_llm(store, session_id, record.consumer_id)

    try:
        result = await orchestrator.run_chat(mcp, message, session_id, record,
                                             llm_complete=llm_complete, history=history,
                                             exclude_tools=orchestrator.WORKFLOW_ONLY_TOOLS)
    except llm_broker.LLMBusy as busy:
        return _busy_response(busy)
    except Exception as exc:  # noqa: BLE001 -- an LLM-side failure (auth, timeout,
        # connection refused, ...) must reach the client as a readable JSON error,
        # the same as _chat_stream's SSE error frame -- not an unhandled 500 with
        # no body, which is what this endpoint returned before this except existed.
        return JSONResponse({"error": f"assistant turn failed: {exc}"}, status_code=502)
    chat_log.record_turn(store, session_id, record.consumer_id, "user", message)
    chat_log.record_turn(store, session_id, record.consumer_id, "assistant", result.get("reply", ""),
                         tools_used=[t.get("tool") for t in result.get("tool_calls", [])])
    result["conversation_id"] = session_id
    return JSONResponse(result)


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _busy_payload(busy) -> dict:
    """A queue-full/queue-timeout is not a failure of the assistant, it is the
    shared model server being at capacity (see gateway/llm_broker.py) -- so it
    gets its own shape: 503 + Retry-After semantics + the numbers needed to tell
    someone something true, rather than a generic 502 'assistant turn failed'."""
    return {
        "error": str(busy),
        "code": "assistant_busy",
        "lane": getattr(busy, "lane", ""),
        "queued": getattr(busy, "queued", 0),
        "retryable": True,
    }


def _busy_response(busy):
    return JSONResponse(_busy_payload(busy), status_code=503, headers={"Retry-After": "10"})


async def _chat_stream(request):
    """Streaming (SSE) variant of /chat: the final answer streams token-by-token.
    Same governed path as /chat; the client falls back to /chat if this fails."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    requested_id = str(body.get("conversation_id") or f"chat:{record.consumer_id}")
    client_ip = ctx.client_ip(request)
    store = get_store()
    llm_complete = orchestrator.default_llm_complete()
    session_id = await chat_log.resolve_session_id(store, requested_id, record.consumer_id, _CHAT_IDLE_SEC, llm_complete)
    history = chat_log.history_for_llm(store, session_id, record.consumer_id)

    async def gen():
        # Bind the principal INSIDE the generator's context so governed tool calls
        # resolve + audit under this user while the stream is produced.
        ctx.consumer_ctx.set(record.name)
        ctx.consumer_record_ctx.set(record)
        ctx.ip_ctx.set(client_ip)
        yield _sse({"type": "meta", "conversation_id": session_id})
        parts: list[str] = []
        tools: list[dict] = []
        completed = False
        try:
            async for ev in orchestrator.run_chat_stream(mcp, message, session_id, record,
                                                          llm_complete=llm_complete, history=history,
                                                          exclude_tools=orchestrator.WORKFLOW_ONLY_TOOLS):
                t = ev.get("type")
                if t == "delta":
                    parts.append(ev.get("text", ""))
                elif t == "replace":
                    parts = [ev.get("text", "")]
                elif t == "done":
                    tools = ev.get("tool_calls", [])
                    completed = True
                yield _sse(ev)
        except llm_broker.LLMBusy as busy:
            yield _sse({"type": "error", **_busy_payload(busy)})
        except Exception as exc:  # noqa: BLE001 -- surface as an SSE error; client will fall back
            yield _sse({"type": "error", "message": str(exc)})
        # Persist the turn only on a clean finish -- otherwise the client falls back
        # to /chat, which records it, and we must not double-record.
        if completed:
            chat_log.record_turn(store, session_id, record.consumer_id, "user", message)
            chat_log.record_turn(store, session_id, record.consumer_id, "assistant", "".join(parts),
                                 tools_used=[t.get("tool") for t in tools])

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _workflow_chat(request):
    """Same governed loop as /chat, but for the 'My Workflow' builder's own
    copilot -- separate conversation (own session_id namespace, so it never
    shares history with the general assistant) and a dedicated system prompt
    (orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT) focused on discovering real
    fields and building a report, rather than general customer/order Q&A.
    Same chat_log session storage/history mechanics as _chat -- only the
    prompt and the default conversation_id prefix differ."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    requested_id = str(body.get("conversation_id") or f"workflow-chat:{record.consumer_id}")
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set(ctx.client_ip(request))

    store = get_store()
    llm_complete = orchestrator.default_llm_complete()
    session_id = await chat_log.resolve_session_id(store, requested_id, record.consumer_id, _CHAT_IDLE_SEC, llm_complete)
    history = chat_log.history_for_llm(store, session_id, record.consumer_id)

    try:
        result = await orchestrator.run_chat(mcp, message, session_id, record,
                                             llm_complete=llm_complete, history=history,
                                             system_prompt=orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT,
                                             max_turns=orchestrator.WORKFLOW_CHAT_MAX_TURNS)
    except llm_broker.LLMBusy as busy:
        return _busy_response(busy)
    except Exception as exc:  # noqa: BLE001 -- see _chat's identical handling
        return JSONResponse({"error": f"assistant turn failed: {exc}"}, status_code=502)
    chat_log.record_turn(store, session_id, record.consumer_id, "user", message)
    chat_log.record_turn(store, session_id, record.consumer_id, "assistant", result.get("reply", ""),
                         tools_used=[t.get("tool") for t in result.get("tool_calls", [])])
    result["conversation_id"] = session_id
    return JSONResponse(result)


async def _workflow_chat_stream(request):
    """Streaming counterpart to _workflow_chat -- see _chat_stream; identical
    except for the system prompt and the default conversation_id prefix."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    requested_id = str(body.get("conversation_id") or f"workflow-chat:{record.consumer_id}")
    client_ip = ctx.client_ip(request)
    store = get_store()
    llm_complete = orchestrator.default_llm_complete()
    session_id = await chat_log.resolve_session_id(store, requested_id, record.consumer_id, _CHAT_IDLE_SEC, llm_complete)
    history = chat_log.history_for_llm(store, session_id, record.consumer_id)

    async def gen():
        ctx.consumer_ctx.set(record.name)
        ctx.consumer_record_ctx.set(record)
        ctx.ip_ctx.set(client_ip)
        yield _sse({"type": "meta", "conversation_id": session_id})
        parts: list[str] = []
        tools: list[dict] = []
        completed = False
        try:
            async for ev in orchestrator.run_chat_stream(mcp, message, session_id, record,
                                                          llm_complete=llm_complete, history=history,
                                                          system_prompt=orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT,
                                                          max_turns=orchestrator.WORKFLOW_CHAT_MAX_TURNS):
                t = ev.get("type")
                if t == "delta":
                    parts.append(ev.get("text", ""))
                elif t == "replace":
                    parts = [ev.get("text", "")]
                elif t == "done":
                    tools = ev.get("tool_calls", [])
                    completed = True
                yield _sse(ev)
        except llm_broker.LLMBusy as busy:
            yield _sse({"type": "error", **_busy_payload(busy)})
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(exc)})
        if completed:
            chat_log.record_turn(store, session_id, record.consumer_id, "user", message)
            chat_log.record_turn(store, session_id, record.consumer_id, "assistant", "".join(parts),
                                 tools_used=[t.get("tool") for t in tools])

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _chat_history(request):
    """This user's own past sessions (list view); admins may pass ?consumer_id= to
    view someone else's, same role-scoping as /admin/calls."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    consumer_id = request.query_params.get("consumer_id") or claims["sub"]
    if consumer_id != claims["sub"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    return JSONResponse({"sessions": chat_log.list_history_for(get_store(), consumer_id)})


def _session_for_viewer(store, sid: str, claims: dict):
    """Resolve a chat session for whoever's asking, WITHOUT ever letting a
    same-id collision from a different owner leak through.

    Looks up scoped to the caller's OWN identity first (a partition-scoped
    point read on Cosmos -- cannot possibly return a different consumer's
    document even if they happen to share this exact session_id string, e.g.
    from the client-side sessionStorage collision fixed 2026-07: two accounts
    tested in the same browser tab could end up sending the same
    conversation_id). Only falls back to the broader "owner unknown"
    cross-partition lookup (get_transcript) for an admin, who legitimately may
    look up any user's session by id; that fallback is the ONLY place an
    arbitrary same-id match across owners could still occur, and it's gated to
    admins whose ownership check is bypassed anyway."""
    session = store.get_chat_session_for(sid, claims["sub"])
    if session is None and claims.get("role") == "admin":
        session = chat_log.get_transcript(store, sid)
    return session


async def _chat_transcript(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    session = _session_for_viewer(get_store(), request.path_params["sid"], claims)
    if session is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if session.consumer_id != claims["sub"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    return JSONResponse({
        "session_id": session.session_id, "status": session.status,
        "created_at": session.created_at, "last_active_at": session.last_active_at,
        "closed_at": session.closed_at, "summary": session.summary,
        "messages": [{"role": m.role, "content": m.content, "ts": m.ts, "tools_used": m.tools_used}
                    for m in session.messages],
    })


async def _chat_resume(request):
    """'Continue this conversation' from the History panel. An open session is
    already resumable under its own id (just tell the caller to keep using it);
    a closed one is cloned into a fresh open session pre-seeded with its messages
    (see chat_log.resume_session -- we never reopen a closed id)."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    store = get_store()
    session = _session_for_viewer(store, request.path_params["sid"], claims)
    if session is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if session.consumer_id != claims["sub"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    if session.status == "open":
        return JSONResponse({"conversation_id": session.session_id, "cloned": False})
    new_id = chat_log.resume_session(store, session)
    return JSONResponse({"conversation_id": new_id, "cloned": True})


async def _chat_feedback(request):
    """Thumbs up/down on an assistant reply -- recorded as a policy_change event
    so it lands in the audit trail for later tuning."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        body = {}
    rating = str(body.get("rating") or "").strip()
    if rating not in ("up", "down"):
        return JSONResponse({"error": "rating must be 'up' or 'down'"}, status_code=400)
    note = str(body.get("note") or "")[:300]
    audit.log_policy_change(actor=claims["name"], action="chat_feedback",
                            target=str(body.get("conversation_id") or ""),
                            detail=f"{rating}{(': ' + note) if note else ''}")
    return JSONResponse({"ok": True})


async def _chat_sweep_loop() -> None:
    """Background guarantee: close + summarize any chat session idle past the
    cutoff, even if its user never sends another message to trigger the lazy
    check in _chat(). Single-instance, in-process -- consistent with the rest of
    this app's Stage-1 model (audit ring, rate limits, scope_store)."""
    store = get_store()
    while True:
        await asyncio.sleep(_CHAT_SWEEP_INTERVAL_SEC)
        try:
            llm_complete = orchestrator.default_llm_complete()
            closed = await chat_log.close_idle_sessions(store, _CHAT_IDLE_SEC, llm_complete)
            if closed:
                print(f"[chat-sweep] closed {closed} idle session(s)", flush=True)
        except Exception as exc:  # the sweep must never crash the process
            print(f"[chat-sweep] failed: {exc}", file=sys.stderr, flush=True)
