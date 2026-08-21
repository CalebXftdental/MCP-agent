"""The enforcement pipeline every governed tool call passes through.

Extracted from app.py so both callers can reach it without a circular import:
app.py's @mcp.tool definitions (the machine plane) and backend/workflow_api.py +
backend/session.py (the workflow runner and the dashboard playground).

_govern is the single choke point -- break-glass pause, PDP verdict, server-side
scope injection, backend call, redaction, audit. Nothing reaches a backend
without passing through it.
"""
from __future__ import annotations

from policy import manifest
from policy.decision import Scope
from policy.decision import decide
from policy.redaction import apply as apply_redaction
from policy.resolve import resolve as resolve_grant
from store import get_store
import audit
import mcp_clients
import schema_catalog
import json
import os
import request_context as ctx
import scope_store
import time

# Temporary kill switch for get_sales_price (ARSalesPrice): the pricing surface
# was wired up ahead of a full security review of that data, so it defaults to
# OFF here at the single gateway choke point (not in the backend) regardless of
# what any category/consumer grants. Flip ALLOW_PRICE_QUERY=true once reviewed
# -- no code change needed to re-enable.
_GATED_TOOLS_ENV_FLAGS = {"get_sales_price": "ALLOW_PRICE_QUERY"}

# "My Workflow" copilot authoring turns always use a session_id prefixed
# "workflow-chat" (backend/chat.py's _workflow_chat/_workflow_chat_stream),
# distinct from a real executed run's "workflow:"+run_id (workflow_graph_
# interpreter.py) and Home chat's "chat:" -- a zero-plumbing signal that a
# call is happening during graph/report authoring, not real execution.
#
# Refined 2026-08-21 after a real benchmark showed a blanket "clamp every
# build-mode page_size" + "silently truncate every build-mode list result"
# design (this comment used to describe that) actively breaks the copilot's
# actual job: once get_field_catalog exists, a tool that already has a real
# outputSchema doesn't need probing at all -- a real call to it during
# authoring is presumably the deliberate "fetch the actual data for this
# report" step, and clamping/truncating it just prevents the report from
# ever being built (confirmed: the model resorted to paging through a
# self-exhausting tool trying to reconstruct the truncated data, burning
# its whole turn budget without success). See STAGE2_PLAN.md §11.2/11.3.
#
# Current design: only force a small sample for tools that still lack a real
# schema (schema_catalog.get_output_schema returns None -- they may still be
# probed via real data, same risk as before get_field_catalog existed).
# Deliberate fetches to schema-known tools are never request-clamped. Either
# way, a result that's still too large for a build-mode turn gets an honest
# "too large, narrow your query" signal instead of silent truncation --
# never hand back partial data pretending to be complete.
_BUILD_MODE_SESSION_PREFIX = "workflow-chat"
_BUILD_MODE_SAMPLE_PAGE_SIZE = int(os.getenv("GOVERNANCE_BUILD_MODE_SAMPLE_PAGE_SIZE", "3"))
_BUILD_MODE_MAX_RESULT_ROWS = int(os.getenv("GOVERNANCE_BUILD_MODE_MAX_RESULT_ROWS", "75"))


def _tool_gated_off(canonical_tool: str) -> bool:
    env_var = _GATED_TOOLS_ENV_FLAGS.get(canonical_tool)
    if env_var is None:
        return False
    return (os.getenv(env_var) or "false").strip().lower() not in ("1", "true", "yes", "on")


def _refusal(verdict) -> str:
    return json.dumps({
        "source": "governance",
        "status": "missing_identifier" if verdict.reason == "missing_customer_scope" else "denied",
        "intent": verdict.intent,
        "message": verdict.message,
        "missingFields": verdict.missing_fields,
    })


def _error_result(intent: str, exc: Exception) -> str:
    return json.dumps({
        "source": "governance",
        "status": "error",
        "intent": intent,
        "errorCode": type(exc).__name__,
        "message": "The lookup could not be completed.",
    })


def _paused_result(reason: str, message: str) -> str:
    return json.dumps({"source": "governance", "status": "paused", "reason": reason, "message": message})


def _find_oversized_list_field(obj: object, limit: int) -> tuple[str, int] | None:
    """First top-level list-valued field over `limit` items, or None.

    Deliberately dumb (top-level only, first match wins) -- this is a safety
    backstop for the build-mode lane, not a general result-shaping feature."""
    if not isinstance(obj, dict):
        return None
    for key, value in obj.items():
        if isinstance(value, list) and len(value) > limit:
            return key, len(value)
    return None


def _too_large_result(intent: str, field: str, actual: int, limit: int) -> str:
    return json.dumps({
        "source": "governance", "status": "too_large_for_context", "intent": intent,
        "message": (
            f"This call matched {actual} rows in '{field}' -- too many to hand to you in one "
            f"turn while authoring (cap: {limit}). Narrow your criteria (a tighter date range, "
            "a higher amount threshold, a specific vendor/customer) and try again, rather than "
            "paging through -- the same cap applies to every page."
        ),
        "matchedCount": actual, "limit": limit,
    })


def _resolve_scope(session_id: str, customer_id: str) -> str | None:
    """Bind/resolve the account for this session, server-side.

    A supplied customer_id is remembered for the session; if omitted, the one
    already bound to the session (if any) is used. This is the single place the
    gateway trusts a customer_id -- backends receive only the resolved value.
    """
    supplied = (customer_id or "").strip()
    if supplied:
        scope_store.note_customer_id(session_id, supplied, consumer=ctx.consumer_ctx.get())
        return supplied
    return scope_store.customer_id_for_session(session_id)


async def _govern(canonical_tool: str, session_id: str, customer_id: str, backend_args: dict) -> str:
    """The enforcement pipeline: PDP -> scope -> backend -> redact -> audit."""
    consumer = ctx.consumer_ctx.get()
    record = ctx.consumer_record_ctx.get()
    grant = resolve_grant(record, get_store().get_category, get_store().get_department) if record is not None else None
    scope_store.touch(session_id, consumer=consumer)
    resolved_customer = _resolve_scope(session_id, customer_id)
    policy = manifest.get(canonical_tool)
    namespaced = manifest.namespaced(canonical_tool)

    # Break-glass: a global pause blocks agent (API-key) callers and/or specific
    # backends at this single choke point (chat + /mcp both flow through here).
    controls = get_store().get_controls()
    if record is not None and record.consumer_id in (controls.get("paused_consumers") or []):
        audit.log_denied(tool=namespaced, session_id=session_id, reason="paused_consumer",
                         consumer=consumer, client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(),
                         customer_id=resolved_customer)
        return _paused_result("paused_consumer", "This consumer's access is temporarily paused by an administrator.")

    # A lighter overlay than the full-consumer pause above: temporarily treats
    # specific categories as off for THIS consumer only, without touching their
    # permanent `categories` grant (see store/base.py's get_controls docstring).
    # Unconditional like the other break-glass checks -- if the called tool
    # falls under a paused category for this consumer, it's blocked even if
    # some other category they hold would also have granted it, so a pause
    # here always does what it looks like it does.
    paused_categories = record is not None and (controls.get("paused_categories") or {}).get(record.consumer_id)
    if paused_categories and policy is not None:
        for category_id in paused_categories:
            category = get_store().get_category(category_id)
            if category is not None and category.backend == policy.backend and (
                category.tools == "*" or canonical_tool in category.tools
            ):
                audit.log_denied(tool=namespaced, session_id=session_id, reason="paused_category",
                                 consumer=consumer, client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(),
                                 customer_id=resolved_customer)
                return _paused_result(
                    "paused_category",
                    f"Access to the {category.display_name} category is temporarily paused by an administrator.",
                )

    if controls.get("paused_agents") and record is not None and record.type == "agent":
        audit.log_denied(tool=namespaced, session_id=session_id, reason="paused_agents",
                         consumer=consumer, client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(),
                         customer_id=resolved_customer)
        return _paused_result("paused_agents", "Agent access is temporarily paused by an administrator.")
    if policy is not None and policy.backend in (controls.get("paused_backends") or []):
        audit.log_denied(tool=namespaced, session_id=session_id, reason="backend_paused",
                         consumer=consumer, client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(),
                         customer_id=resolved_customer)
        return _paused_result("backend_paused", f"The {policy.backend} backend is temporarily paused.")

    if _tool_gated_off(canonical_tool):
        audit.log_denied(tool=namespaced, session_id=session_id, reason="feature_disabled",
                         consumer=consumer, client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(),
                         customer_id=resolved_customer)
        return _paused_result(
            "feature_disabled",
            f"{namespaced} is temporarily disabled pending a full security review.",
        )

    verdict = decide(consumer, canonical_tool, backend_args, Scope(customer_id=resolved_customer), grant=grant)
    if not verdict.allowed:
        audit.log_denied(
            tool=namespaced, session_id=session_id, reason=verdict.reason,
            consumer=consumer, client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(),
            customer_id=resolved_customer,
        )
        return _refusal(verdict)

    # Inject the trusted, server-resolved customer_id for account-scoped tools.
    args = dict(backend_args)
    if policy.account_scoped:
        args["customer_id"] = resolved_customer
    if session_id.startswith(_BUILD_MODE_SESSION_PREFIX) and isinstance(args.get("page_size"), int):
        # Only force a small sample for tools that don't have a real schema
        # yet -- those may still be probed via real data, same as before
        # get_field_catalog existed. A tool with a known schema needs no
        # probing, so a real call to it here is presumably deliberate; leave
        # its page_size alone (the response-side oversized check below is
        # still the backstop if that deliberate fetch turns out too big).
        schema = await schema_catalog.get_output_schema(policy.backend, canonical_tool)
        if schema is None:
            args["page_size"] = min(args["page_size"], _BUILD_MODE_SAMPLE_PAGE_SIZE)

    start = time.time()
    args_summary = audit.summarize_args({**args, "session_id": session_id})
    try:
        raw = await mcp_clients.call(policy.backend, canonical_tool, args)
    except mcp_clients.BackendError as exc:
        audit.log_call(
            tool=namespaced, session_id=session_id, status="error", consumer=consumer,
            client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(), args_summary=args_summary,
            latency_ms=(time.time() - start) * 1000, detail=str(exc), customer_id=resolved_customer,
        )
        return _error_result(verdict.intent, exc)

    # Redact per the PDP plan. If the backend returned non-JSON (shouldn't), pass
    # it through unredacted but flag it in the audit detail.
    redactions: list[str] = []
    detail = None
    rows = None
    try:
        parsed = json.loads(raw)
        redacted, redactions = apply_redaction(verdict.redaction_plan, parsed)
        if session_id.startswith(_BUILD_MODE_SESSION_PREFIX):
            oversized = _find_oversized_list_field(redacted, _BUILD_MODE_MAX_RESULT_ROWS)
            if oversized is not None:
                field_name, actual_len = oversized
                audit.log_call(
                    tool=namespaced, session_id=session_id, status="ok", consumer=consumer,
                    client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(), args_summary=args_summary,
                    latency_ms=(time.time() - start) * 1000,
                    detail=f"too_large_for_context: {field_name} had {actual_len} rows (build-mode cap {_BUILD_MODE_MAX_RESULT_ROWS})",
                    customer_id=resolved_customer, rows=actual_len,
                )
                return _too_large_result(verdict.intent, field_name, actual_len, _BUILD_MODE_MAX_RESULT_ROWS)
        rows = _result_rows(redacted)
        out = json.dumps(redacted)
    except (TypeError, ValueError):
        out = raw
        detail = "unstructured_backend_result_not_redacted"

    # Flag (audit-only, non-blocking here) when an export-risk tool's raw result
    # crosses its declared max_rows_without_approval (manifest.py §7.3). This is
    # NOT an approval gate itself -- direct chat/playground calls aren't a workflow
    # run and have no approval object to pause against -- it just gives the
    # exfiltration-detection analytics (safety.py, §13.8) a per-tool broad-export
    # signal instead of only a raw row count. The workflow engine (see
    # _workflow_requires_broad_export_approval) is what actually pauses a run.
    over_threshold = (
        verdict.max_rows_without_approval is not None
        and rows is not None
        and rows > verdict.max_rows_without_approval
    )
    detail_parts = [detail] if detail else []
    if redactions:
        detail_parts.append(f"redacted: {', '.join(redactions)}")
    if over_threshold:
        detail_parts.append(f"over_risk_threshold: {rows} rows > {verdict.max_rows_without_approval} ({verdict.risk})")

    audit.log_call(
        tool=namespaced, session_id=session_id, status="ok", consumer=consumer,
        client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(), args_summary=args_summary,
        latency_ms=(time.time() - start) * 1000,
        detail="; ".join(detail_parts) or None,
        customer_id=resolved_customer, rows=rows,
    )
    return out


def _result_rows(obj) -> int | None:
    """Best-effort count of records in a backend result, for the exfil metric.

    A bare list is its own length; the common {"<entity>": [...]} envelope is the
    length of its first list value; a single record object counts as 1. Returns
    None when nothing list-shaped is found and it isn't an obvious single record.
    """
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list):
                return len(v)
        return 1
    return None
