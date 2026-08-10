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
import os
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import request_context as ctx

_MAX_RECORDS = 1000
_records: deque[dict] = deque(maxlen=_MAX_RECORDS)

# ── Durable sink ──────────────────────────────────────────────────────────────
# The in-memory ring buffer above is fast but resets on restart and doesn't span
# replicas. Precedence mirrors store/__init__.py's get_store(): Cosmos (durable,
# multi-instance, same "governance" database/account as the policy store) if
# configured, else a local JSONL file (GOVERNANCE_AUDIT_FILE, single-instance),
# else stdout + ring buffer only (today's default when neither is set). The
# ring buffer is hydrated from whichever durable sink is active at startup, so
# the "who asked" monitor persists across deploys/restarts.
def _default_audit_file() -> str | None:
    """Pick a durable file sink by default so long-range analytics survive a
    restart without extra config. Explicit GOVERNANCE_AUDIT_FILE always wins. If
    Cosmos is configured it owns durability (return None -> file sink unused). In
    plain file-store mode (GOVERNANCE_STORE_FILE set, no Cosmos) we co-locate an
    audit.jsonl next to the policy store. With nothing persistent configured at
    all, stay ring-only (None) -- writing files unbidden in a stateless deploy
    would be the surprise, not the feature."""
    explicit = os.getenv("GOVERNANCE_AUDIT_FILE")
    if explicit:
        return explicit
    cosmos = os.getenv("GOVERNANCE_COSMOS_CONNECTION_STRING") or (
        os.getenv("GOVERNANCE_COSMOS_URL") and os.getenv("GOVERNANCE_COSMOS_KEY"))
    store_file = os.getenv("GOVERNANCE_STORE_FILE")
    if store_file and not cosmos:
        return str(Path(store_file).with_name("audit.jsonl"))
    return None


_AUDIT_FILE = _default_audit_file()
_AUDIT_TTL_DAYS = int(os.getenv("GOVERNANCE_AUDIT_TTL_DAYS") or "90")
# Cap the file sink so the default-on durable file can't grow without bound in
# production (Cosmos has a TTL; a plain file doesn't). At the cap we roll to a
# single ".1" backup and start fresh. Cosmos remains the choice for long-term /
# multi-instance retention; the file sink is single-node convenience.
_AUDIT_FILE_MAX_BYTES = int(os.getenv("GOVERNANCE_AUDIT_FILE_MAX_MB") or "50") * 1024 * 1024
_sink_lock = threading.Lock()

_cosmos_container = None
_cosmos_init_attempted = False


def _cosmos_audit_container():
    """Lazily connect to the `audit` container (PK /consumer, TTL "on, no
    default" -- see chatSessions' sibling note in store/cosmos.py) in the same
    Cosmos account/database the policy store uses. Returns None (falls back to
    the file/ring-buffer sinks) if Cosmos isn't configured or init fails --
    audit must never be the reason a deploy won't boot.

    No `_sink_lock` around the actual writes below: azure-cosmos's client is
    safe for concurrent use, and serializing every tool call behind one lock
    for the duration of a network round-trip would add real latency under
    concurrency for no correctness benefit (unlike the file sink, where the
    lock protects a shared local file handle from interleaved writes)."""
    global _cosmos_container, _cosmos_init_attempted
    if _cosmos_init_attempted:
        return _cosmos_container
    _cosmos_init_attempted = True
    conn = os.getenv("GOVERNANCE_COSMOS_CONNECTION_STRING")
    url = os.getenv("GOVERNANCE_COSMOS_URL")
    key = os.getenv("GOVERNANCE_COSMOS_KEY")
    if not (conn or (url and key)):
        return None
    try:
        from azure.cosmos import CosmosClient, PartitionKey
        client = CosmosClient.from_connection_string(conn) if conn else CosmosClient(url, credential=key)
        db = client.create_database_if_not_exists(os.getenv("GOVERNANCE_COSMOS_DATABASE", "governance"))
        _cosmos_container = db.create_container_if_not_exists("audit", PartitionKey(path="/consumer"))
    except Exception as exc:
        print(f"[audit] cosmos-sink init failed, falling back to file/ring-buffer: {exc}",
              file=sys.stderr, flush=True)
        _cosmos_container = None
    return _cosmos_container


def _append_durable(record: dict) -> None:
    """Append one record to the durable sink. Never raises into the hot path."""
    container = _cosmos_audit_container()
    if container is not None:
        try:
            doc = {**record, "id": uuid.uuid4().hex}
            # policy_change (who-changed-what) is compliance-relevant and low
            # volume -- kept indefinitely. Everything else (routine call/deny/
            # rate-limit noise, the bulk of the volume) expires after the TTL;
            # the container's own TTL is opt-in-per-item, so omitting this
            # field entirely is what makes policy_change permanent.
            if record.get("type") != "policy_change":
                doc["ttl"] = _AUDIT_TTL_DAYS * 86400
            container.upsert_item(doc)
        except Exception as exc:  # audit must never break the request it is recording
            print(f"[audit] cosmos-sink write failed: {exc}", file=sys.stderr, flush=True)
        return

    if not _AUDIT_FILE:
        return
    try:
        with _sink_lock:
            path = Path(_AUDIT_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            if _AUDIT_FILE_MAX_BYTES and path.exists() and path.stat().st_size >= _AUDIT_FILE_MAX_BYTES:
                path.replace(path.with_name(path.name + ".1"))  # roll to a single backup, start fresh
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        print(f"[audit] durable-sink write failed: {exc}", file=sys.stderr, flush=True)


_COSMOS_SYSTEM_FIELDS = ("id", "ttl", "_rid", "_self", "_etag", "_attachments", "_ts")


def _hydrate_from_sink() -> None:
    """Load the tail of the durable sink into the ring buffer at startup so the
    dashboard shows history after a restart. Malformed/unreadable entries are
    skipped -- hydration failing must never block boot."""
    container = _cosmos_audit_container()
    if container is not None:
        try:
            items = container.query_items(
                query=f"SELECT TOP {_MAX_RECORDS} * FROM c ORDER BY c.ts DESC",
                enable_cross_partition_query=True,
            )
            records = [dict(d) for d in items]
            records.reverse()  # oldest-first, matching _records' natural append order
            for r in records:
                for f in _COSMOS_SYSTEM_FIELDS:
                    r.pop(f, None)
                _records.append(r)
        except Exception as exc:
            print(f"[audit] cosmos-sink hydrate failed: {exc}", file=sys.stderr, flush=True)
        return

    if not _AUDIT_FILE:
        return
    try:
        path = Path(_AUDIT_FILE)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()[-_MAX_RECORDS:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                _records.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    except Exception as exc:
        print(f"[audit] durable-sink hydrate failed: {exc}", file=sys.stderr, flush=True)


def _emit(record: dict) -> None:
    _records.append(record)
    _append_durable(record)
    print(json.dumps(record, default=str), flush=True)


_hydrate_from_sink()


def recent(limit: int = 200) -> list[dict]:
    """Most recent events first, capped at `limit`."""
    items = list(_records)[-limit:]
    items.reverse()
    return items


# Bound on how many rows analytics will pull for a long window, so a 30-day query
# over a busy durable sink can't blow up memory/latency.
_ANALYTICS_MAX = int(os.getenv("GOVERNANCE_AUDIT_ANALYTICS_MAX") or "20000")


def read_since(since_ts: float, limit: int = _ANALYTICS_MAX) -> list[dict]:
    """Events at/after `since_ts`, most-recent-first, for analytics over windows
    longer than the in-memory ring.

    Reads the durable sink (Cosmos, else the JSONL file) when one is configured,
    so 7-/30-day rollups aren't truncated to the last 1000 events. Falls back to
    the ring buffer (filtered to the window) when there's no durable sink, or if
    the durable read fails -- analytics degrading to "recent only" is always
    preferable to erroring.
    """
    container = _cosmos_audit_container()
    if container is not None:
        try:
            items = container.query_items(
                query=("SELECT * FROM c WHERE c.ts >= @since ORDER BY c.ts DESC "
                       "OFFSET 0 LIMIT @lim"),
                parameters=[{"name": "@since", "value": since_ts}, {"name": "@lim", "value": limit}],
                enable_cross_partition_query=True,
            )
            out = []
            for d in items:
                r = dict(d)
                for f in _COSMOS_SYSTEM_FIELDS:
                    r.pop(f, None)
                out.append(r)
            return out
        except Exception as exc:
            print(f"[audit] cosmos-sink read_since failed, falling back to ring: {exc}",
                  file=sys.stderr, flush=True)

    if _AUDIT_FILE:
        try:
            path = Path(_AUDIT_FILE)
            if path.exists():
                # Single pass with a bounded deque: memory stays O(limit) no matter
                # how large the audit file has grown (the file is append-only /
                # chronological, so the newest matches are the tail we keep).
                matched: deque[dict] = deque(maxlen=limit)
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if (rec.get("ts") or 0) >= since_ts:
                            matched.append(rec)
                recent_slice = list(matched)
                recent_slice.reverse()  # most-recent-first
                return recent_slice
        except Exception as exc:
            print(f"[audit] durable-sink read_since failed, falling back to ring: {exc}",
                  file=sys.stderr, flush=True)

    # No durable sink (or it failed): the ring buffer is all we have.
    return [r for r in recent(_MAX_RECORDS) if (r.get("ts") or 0) >= since_ts][:limit]


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
    customer_id: str | None = None,
    rows: int | None = None,
) -> None:
    # `rows` = number of records the (redacted) backend result carried, when it
    # could be counted -- the primary data-exfiltration signal (call count alone
    # misses "8 calls, 12k records"). None when the result wasn't countable
    # (unstructured payload) or on error/denied paths.
    _emit({
        "ts": time.time(),
        "type": "call",
        "request_id": _request_id(),
        "consumer": consumer,
        "client_ip": client_ip,
        "user_agent": user_agent,
        "tool": tool,
        "session_id": session_id,
        "customer_id": customer_id,
        "args": args_summary,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "detail": detail,
        "rows": rows,
    })


def log_denied(
    *,
    tool: str,
    session_id: str | None,
    reason: str,
    consumer: str = "unknown",
    client_ip: str | None = None,
    user_agent: str | None = None,
    customer_id: str | None = None,
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
        "customer_id": customer_id,
        "args": "",
        "status": "denied",
        "latency_ms": 0.0,
        "detail": reason,
    })


def log_auth_denied(*, path: str, client_ip: str | None, user_agent: str | None, reason: str,
                     consumer: str = "unauthenticated") -> None:
    # `consumer` defaults to "unauthenticated" for denials before any key is
    # checked (bad/missing key, no keys configured). Callers that already
    # resolved a valid key/record before denying (e.g. a per-consumer IP
    # allowlist miss) should pass the real consumer name -- otherwise the
    # dashboard can't tell you WHICH consumer's key got IP-blocked, only that
    # someone did.
    _emit({
        "ts": time.time(),
        "type": "auth_denied",
        "request_id": None,
        "consumer": consumer,
        "client_ip": client_ip,
        "user_agent": user_agent,
        "tool": path,
        "session_id": None,
        "args": "",
        "status": "unauthorized",
        "latency_ms": 0.0,
        "detail": reason,
    })


def log_transport_error(*, path: str, consumer: str, client_ip: str | None, user_agent: str | None,
                         status_code: int) -> None:
    """A request that passed Layer 1 auth but failed inside the MCP transport
    itself (e.g. the `mcp` SDK's own initialize/session handling) -- before any
    @mcp.tool() handler or _govern() ever ran, so log_call/log_denied never
    fire for it. Without this, that failure class is invisible to /dashboard:
    not an auth_denied (auth succeeded) and not a call/denied (no tool ran)."""
    _emit({
        "ts": time.time(),
        "type": "transport_error",
        "request_id": _request_id(),
        "consumer": consumer,
        "client_ip": client_ip,
        "user_agent": user_agent,
        "tool": path,
        "session_id": None,
        "args": "",
        "status": "transport_error",
        "latency_ms": 0.0,
        "detail": f"HTTP {status_code} from MCP transport before any tool handler ran",
    })


def log_policy_change(*, actor: str, action: str, target: str, detail: str | None = None) -> None:
    """Record an admin change to policy (who / what / when). Separate `type` so it can
    be filtered out from the tool-call monitor and shown in its own audit view."""
    _emit({
        "ts": time.time(),
        "type": "policy_change",
        "request_id": _request_id(),
        "consumer": actor,
        "client_ip": ctx.ip_ctx.get(),
        "user_agent": None,
        "tool": f"{action} {target}",
        "session_id": None,
        "args": "",
        "status": "changed",
        "latency_ms": 0.0,
        "detail": detail,
    })


def recent_policy_changes(limit: int = 200) -> list[dict]:
    return [r for r in recent(1000) if r.get("type") == "policy_change"][:limit]


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
