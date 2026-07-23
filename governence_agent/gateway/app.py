"""Governance Gateway (PEP) -- the single MCP endpoint for all callers.

Every AI agent and user talks ONLY to this server. It authenticates the caller
(edge.py / EdgeMiddleware, Layer 1), and for each tool call it runs the
deterministic PDP (policy.decision.decide), resolves session scope server-side,
forwards to the owning backend MCP server as an MCP client (backends.call),
applies the PDP's redaction plan to the result, and audits the outcome.

Backends (mcp-minierp-orders, -accounts, -shipments, -finance) hold the
credentials and do the data access; this process holds none. Tools are
exposed namespaced as "<backend>_<canonical>" (e.g.
minierp_orders_get_customer_orders) so multiple backends can be federated
behind one surface without name collisions.

This is Stage 1 of governence_agent/design_plan.md: gateway + the miniERP
domain backends.

Run:
    pip install -r requirements.txt
    python app.py            # serves MCP on GATEWAY_PORT (default 8020) at /mcp
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("GATEWAY_ENV_FILE") or ".env.local")

# governance_core holds the shared plane (edge/audit/scope_store) + the policy
# package. Put it on sys.path so its modules import flatly, as they did when they
# lived in one service.
_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

import analytics
import agent_store
import approval_store
import audit
import automation_store
import calendar_send_store
import code_plan_store
import document_review_store
import artifact_store
import artifact_share_store
import chat_log
import edge
import email_send_store
import knowledge_store
import request_context as ctx
import safety
import template_store
import workflows
import workflow_graph_store
from workflow_graph_models import GraphEdge, GraphNode
import workflow_graph_interpreter
import scope_store
import backends
import orchestrator
from auth import lockout
from auth.passwords import hash_password, verify_password
from auth.session import issue_session, login_enabled, verify_session
from store import get_store
from store.keys import generate_api_key, hash_api_key
from store.models import ConsumerRecord
from policy import manifest
from departments import Department
from policy.categories import Category
from policy.decision import Scope, decide
from policy.redaction import apply as apply_redaction
from policy.resolve import resolve as resolve_grant


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ MCP server + transport security (same host-allowlist rationale as backend) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

def _allowed_hosts() -> list[str]:
    raw = (os.getenv("GATEWAY_ALLOWED_HOSTS") or os.getenv("GOVERNANCE_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-governance-gateway",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in _ALLOWED_HOSTS if ":" not in h and not h.startswith(("localhost", "127."))]
        + ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


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

    start = time.time()
    args_summary = audit.summarize_args({**args, "session_id": session_id})
    try:
        raw = await backends.call(policy.backend, canonical_tool, args)
    except backends.BackendError as exc:
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


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Federated miniERP tools (namespaced per domain backend) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬
# Explicit signatures give callers a proper input schema. Each just marshals its
# args and defers all policy to _govern. Tool names are namespaced
# "<backend>_<canonical>" (policy.manifest.namespaced) -- the backend prefix
# here MUST match each tool's ToolPolicy.backend in policy/manifest.py.

@mcp.tool(name="minierp_orders_get_customer_orders")
async def minierp_orders_get_customer_orders(
    session_id: str,
    customer_id: str = "",
    start_date: str = "",
    end_date: str = "",
    order_status: str = "",
    min_total: float | None = None,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List a customer's sales orders, optionally filtered by date range, status, or minimum total.

    customer_id is only needed the first time in a session; once supplied it is
    remembered for this session_id and later calls may omit it."""
    return await _govern("get_customer_orders", session_id, customer_id, {
        "start_date": start_date, "end_date": end_date, "order_status": order_status,
        "min_total": min_total, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_orders_get_customer_order_total")
async def minierp_orders_get_customer_order_total(
    session_id: str, customer_id: str = "", start_date: str = "", end_date: str = "",
) -> str:
    """Calculate a customer's total spend across completed orders, optionally within a date range."""
    return await _govern("get_customer_order_total", session_id, customer_id, {
        "start_date": start_date, "end_date": end_date,
    })


@mcp.tool(name="minierp_orders_get_order_details")
async def minierp_orders_get_order_details(session_id: str, order_number: str, company_id: int | None = None) -> str:
    """Get header-level details (status, total, date) for one sales order by order number."""
    return await _govern("get_order_details", session_id, "", {
        "order_number": order_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_orders_get_product_details_in_order")
async def minierp_orders_get_product_details_in_order(
    session_id: str, order_number: str, company_id: int | None = None, page: int = 1, page_size: int = 10,
) -> str:
    """List the line items (products, quantities, prices) inside one sales order."""
    return await _govern("get_product_details_in_order", session_id, "", {
        "order_number": order_number, "company_id": company_id, "page": page, "page_size": page_size,
    })


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Federated accounts tools (namespaced, backend "minierp_accounts") ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

@mcp.tool(name="minierp_accounts_get_customer_profile")
async def minierp_accounts_get_customer_profile(session_id: str, customer_id: str = "") -> str:
    """Get a customer's billing/credit profile: credit limit, terms, default payment method, statement cycle."""
    return await _govern("get_customer_profile", session_id, customer_id, {})


@mcp.tool(name="minierp_accounts_get_contacts")
async def minierp_accounts_get_contacts(session_id: str, customer_id: str = "", page: int = 1, page_size: int = 10) -> str:
    """List contacts (name, role, phone, email) on a customer's account."""
    return await _govern("get_contacts", session_id, customer_id, {"page": page, "page_size": page_size})


@mcp.tool(name="minierp_accounts_get_addresses")
async def minierp_accounts_get_addresses(session_id: str, customer_id: str = "", page: int = 1, page_size: int = 10) -> str:
    """List addresses on file for a customer's account."""
    return await _govern("get_addresses", session_id, customer_id, {"page": page, "page_size": page_size})


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Federated resolver tools (namespaced, backend "minierp_accounts") ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬
# Entry points: turn a human handle into the canonical id the account-scoped
# tools need. NOT account-scoped -- they are how a customer_id is discovered.

@mcp.tool(name="minierp_accounts_find_customer")
async def minierp_accounts_find_customer(
    session_id: str, query: str, by: str = "auto", company_id: str = "", page: int = 1, page_size: int = 10,
) -> str:
    """Find candidate customers by name, email, phone, or customer id (acctCd).

    Call this FIRST whenever the user names a customer by anything other than the
    internal customer id; use a returned candidate's customerId for follow-up
    account lookups. `by` may be auto|name|email|phone|acctCd."""
    return await _govern("find_customer", session_id, "", {
        "query": query, "by": by, "company_id": company_id, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_accounts_resolve_contact")
async def minierp_accounts_resolve_contact(
    session_id: str, query: str, by: str = "auto", page: int = 1, page_size: int = 10,
) -> str:
    """Find the people (contacts) behind an email, phone, or name: name, title, and their account."""
    return await _govern("resolve_contact", session_id, "", {
        "query": query, "by": by, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_accounts_get_order_addresses")
async def minierp_accounts_get_order_addresses(session_id: str, order_number: str) -> str:
    """Get the ship-to and bill-to addresses for a sales order by order number."""
    return await _govern("get_order_addresses", session_id, "", {"order_number": order_number})


@mcp.tool(name="minierp_accounts_get_customer_overview")
async def minierp_accounts_get_customer_overview(session_id: str, customer_id: str = "") -> str:
    """One-call 360 view of a customer: profile, primary contact, addresses, recent
    orders, and total spend. Prefer this over separate profile/contacts/orders calls."""
    return await _govern("get_customer_overview", session_id, customer_id, {})


@mcp.tool(name="minierp_accounts_get_order_overview")
async def minierp_accounts_get_order_overview(session_id: str, order_number: str, company_id: int | None = None) -> str:
    """One-call 360 view of an order: header, line items, ship/bill addresses, and
    shipment/tracking. Prefer this over separate order/product/shipping calls."""
    return await _govern("get_order_overview", session_id, "", {
        "order_number": order_number, "company_id": company_id,
    })


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Federated quaternary tools (aggregations + reverse lookups) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

@mcp.tool(name="minierp_orders_get_customer_order_summary")
async def minierp_orders_get_customer_order_summary(
    session_id: str, customer_id: str = "", start_date: str = "", end_date: str = "",
) -> str:
    """Aggregate a customer's orders: counts by status, total/average spend, first/last
    order date, and month-by-month buckets over a date range."""
    return await _govern("get_customer_order_summary", session_id, customer_id, {
        "start_date": start_date, "end_date": end_date,
    })


@mcp.tool(name="minierp_orders_get_product_sales")
async def minierp_orders_get_product_sales(
    session_id: str, inventory_id: str, start_date: str = "", end_date: str = "",
) -> str:
    """How a product is selling: total quantity, revenue, distinct orders/customers."""
    return await _govern("get_product_sales", session_id, "", {
        "inventory_id": inventory_id, "start_date": start_date, "end_date": end_date,
    })


@mcp.tool(name="minierp_orders_get_orders_by_product")
async def minierp_orders_get_orders_by_product(
    session_id: str, inventory_id: str, page: int = 1, page_size: int = 25,
) -> str:
    """Which orders (and customers) bought a given product, by inventory id."""
    return await _govern("get_orders_by_product", session_id, "", {
        "inventory_id": inventory_id, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_accounts_get_customers_by_region")
async def minierp_accounts_get_customers_by_region(
    session_id: str, country: str = "", state: str = "", city: str = "", page: int = 1, page_size: int = 25,
) -> str:
    """Customers in a territory, filtered by country, state, and/or city."""
    return await _govern("get_customers_by_region", session_id, "", {
        "country": country, "state": state, "city": city, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_shipments_get_customer_shipment_status")
async def minierp_shipments_get_customer_shipment_status(
    session_id: str, customer_id: str = "", max_orders: int = 5,
) -> str:
    """Shipment/tracking status of a customer's most recent orders."""
    return await _govern("get_customer_shipment_status", session_id, customer_id, {
        "max_orders": max_orders,
    })


@mcp.tool(name="minierp_analytics_get_top_customers_by_spend")
async def minierp_analytics_get_top_customers_by_spend(
    session_id: str, start_date: str = "", end_date: str = "", limit: int = 10,
) -> str:
    """Rank customers by total spend over a period (cross-customer analytics; requires
    the analytics entitlement)."""
    return await _govern("get_top_customers_by_spend", session_id, "", {
        "start_date": start_date, "end_date": end_date, "limit": limit,
    })


# Office artifact tools (namespaced, backend "office"). The model supplies the
# artifact content; the gateway injects the trusted owner so generated files are
# scoped to the current principal and never to a model-provided identity.

@mcp.tool(name="office_create_excel_report")
async def office_create_excel_report(
    session_id: str,
    title: str,
    tables: list[dict],
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create an XLSX artifact from structured table data and return its artifact id/download URL."""
    return await _govern("create_excel_report", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "title": title,
        "tables": tables,
        "classification": classification or [manifest.INTERNAL],
        "filename": filename,
    })


@mcp.tool(name="office_create_powerpoint_deck")
async def office_create_powerpoint_deck(
    session_id: str,
    title: str,
    sections: list[dict],
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a PPTX artifact from structured slide sections and return its artifact id/download URL."""
    return await _govern("create_powerpoint_deck", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "title": title,
        "sections": sections,
        "classification": classification or [manifest.INTERNAL],
        "filename": filename,
    })


@mcp.tool(name="office_create_word_report")
async def office_create_word_report(
    session_id: str,
    title: str,
    sections: list[dict],
    tables: list[dict] | None = None,
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a DOCX artifact from structured sections/tables and return its artifact id/download URL."""
    return await _govern("create_word_report", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "title": title,
        "sections": sections,
        "tables": tables or [],
        "classification": classification or [manifest.INTERNAL],
        "filename": filename,
    })


@mcp.tool(name="office_create_pdf_packet")
async def office_create_pdf_packet(
    session_id: str,
    title: str,
    sections: list[dict],
    tables: list[dict] | None = None,
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a PDF review packet from structured sections/tables and return its artifact id/download URL."""
    return await _govern("create_pdf_packet", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "title": title,
        "sections": sections,
        "tables": tables or [],
        "classification": classification or [manifest.INTERNAL],
        "filename": filename,
    })


@mcp.tool(name="office_convert_artifact")
async def office_convert_artifact(session_id: str, artifact_id: str, target_format: str = "txt", title: str = "") -> str:
    """Convert a governed artifact to TXT or PDF."""
    return await _govern("convert_artifact", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "artifact_id": artifact_id,
        "target_format": target_format,
        "title": title,
    })


@mcp.tool(name="office_extract_tables_from_document")
async def office_extract_tables_from_document(session_id: str, artifact_id: str, create_json_artifact: bool = False) -> str:
    """Extract table-like data from a governed XLSX/DOCX artifact."""
    return await _govern("extract_tables_from_document", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "artifact_id": artifact_id,
        "create_json_artifact": create_json_artifact,
    })

# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Federated shipments tools (namespaced, backend "minierp_shipments") ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

# Email tools (namespaced, backend "email"). Draft creation is enabled; send is
# intentionally approval-gated and returns an approval_required result for now.

@mcp.tool(name="email_create_email_draft")
async def email_create_email_draft(
    session_id: str,
    to: list[str],
    cc: list[str] | None = None,
    subject: str = "",
    body_markdown: str = "",
    attachment_artifact_ids: list[str] | None = None,
    classification: list[str] | None = None,
) -> str:
    """Create a durable email draft artifact. This does not send email."""
    return await _govern("create_email_draft", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "to": to,
        "cc": cc or [],
        "subject": subject,
        "body_markdown": body_markdown,
        "attachment_artifact_ids": attachment_artifact_ids or [],
        "classification": classification or [manifest.INTERNAL],
    })


@mcp.tool(name="email_send_email_draft")
async def email_send_email_draft(session_id: str, draft_id: str, approval_id: str = "") -> str:
    """Request delivery for an email draft. External sending is approval-gated in this scaffold."""
    return await _govern("send_email_draft", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "draft_id": draft_id,
        "approval_id": approval_id,
    })

# Calendar tools (namespaced, backend "calendar"). Invite drafts are artifacts;
# external event creation is approval-gated and connector-backed.

@mcp.tool(name="calendar_draft_calendar_invite")
async def calendar_draft_calendar_invite(
    session_id: str,
    title: str,
    start: str,
    end: str,
    attendees: list[str],
    timezone_name: str = "UTC",
    location: str = "",
    description: str = "",
    classification: list[str] | None = None,
) -> str:
    """Create a durable .ics calendar invite draft. Does not create an external event."""
    return await _govern("draft_calendar_invite", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "title": title,
        "start": start,
        "end": end,
        "attendees": attendees,
        "timezone_name": timezone_name,
        "location": location,
        "description": description,
        "classification": classification or [manifest.INTERNAL, manifest.PII],
    })


@mcp.tool(name="calendar_send_calendar_invite")
async def calendar_send_calendar_invite(session_id: str, draft_id: str, approval_id: str = "") -> str:
    """Queue an approved calendar invite for connector-backed event creation."""
    return await _govern("send_calendar_invite", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "draft_id": draft_id,
        "approval_id": approval_id,
    })


@mcp.tool(name="calendar_create_meeting_brief")
async def calendar_create_meeting_brief(
    session_id: str,
    title: str,
    meeting_time: str = "",
    attendees: list[str] | None = None,
    sections: list[dict] | None = None,
    source_artifact_ids: list[str] | None = None,
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a meeting-prep brief from sections you've already assembled from other
    governed tool calls (e.g. a customer overview + recent orders/shipments)."""
    return await _govern("create_meeting_brief", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "title": title,
        "meeting_time": meeting_time,
        "attendees": attendees or [],
        "sections": sections or [],
        "source_artifact_ids": source_artifact_ids or [],
        "classification": classification or [manifest.INTERNAL, manifest.PII],
        "filename": filename,
    })


@mcp.tool(name="calendar_list_upcoming_meetings")
async def calendar_list_upcoming_meetings(session_id: str, within_days: int = 14, limit: int = 20) -> str:
    """List this user's drafted calendar invites starting within the next N days."""
    return await _govern("list_upcoming_meetings", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "within_days": within_days,
        "limit": limit,
    })

# Knowledge tools (namespaced, backend "knowledge"). Read-only: this backend
# proxies the SAME Azure Blob/AI Search knowledge base AraTestEnvBE's ragAgent
# owns (via KNOWLEDGE_RETRIEVAL_BASE_URL) -- no ingest tools are exposed here.
# New documents go through AraTestEnvBE's own ingestion pipeline, not this gateway.

@mcp.tool(name="knowledge_search_knowledge")
async def knowledge_search_knowledge(session_id: str, query: str, limit: int = 5, document_id: str = "") -> str:
    """Search indexed local knowledge chunks visible to this consumer."""
    return await _govern("search_knowledge", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "query": query,
        "limit": limit,
        "document_id": document_id,
    })


@mcp.tool(name="knowledge_answer_from_knowledge")
async def knowledge_answer_from_knowledge(session_id: str, query: str, limit: int = 5, document_id: str = "") -> str:
    """Return a citation-backed extractive answer from indexed local documents."""
    return await _govern("answer_from_knowledge", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "query": query,
        "limit": limit,
        "document_id": document_id,
    })

# Code automation planning tools (namespaced, backend "code"). These are
# intentionally read-only: they produce audited plans and template drafts, but no
# file writes, shell execution, or opencode edit runs happen through this lane.

@mcp.tool(name="code_opencode_plan_change")
async def code_opencode_plan_change(session_id: str, request: str, target_area: str = "", risk_level: str = "medium") -> str:
    """Create a read-only opencode implementation plan for a requested codebase change."""
    return await _govern("opencode_plan_change", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "request": request,
        "target_area": target_area,
        "risk_level": risk_level,
    })


@mcp.tool(name="code_opencode_review_repo")
async def code_opencode_review_repo(session_id: str, focus: str = "", paths: list[str] | None = None) -> str:
    """Create a read-only repository review plan."""
    return await _govern("opencode_review_repo", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "focus": focus,
        "paths": paths or [],
    })


@mcp.tool(name="code_opencode_generate_template")
async def code_opencode_generate_template(session_id: str, goal: str, template_type: str = "workflow") -> str:
    """Generate a governed workflow/template draft without applying code changes."""
    return await _govern("opencode_generate_template", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "goal": goal,
        "template_type": template_type,
    })

@mcp.tool(name="minierp_shipments_get_shipping_by_shipment")
async def minierp_shipments_get_shipping_by_shipment(session_id: str, shipment_number: str, customer_id: str = "") -> str:
    """Get tracking/invoice details for a shipment by shipment number.

    Requires a customer_id in scope -- the shipment is only released if it is
    linked to that customer's orders."""
    return await _govern("get_shipping_by_shipment", session_id, customer_id, {
        "shipment_number": shipment_number,
    })


@mcp.tool(name="minierp_shipments_get_shipping_by_order")
async def minierp_shipments_get_shipping_by_order(session_id: str, order_number: str, company_id: int | None = None) -> str:
    """Get shipment/tracking/invoice numbers linked to a sales order by order number."""
    return await _govern("get_shipping_by_order", session_id, "", {
        "order_number": order_number, "company_id": company_id,
    })


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Federated finance tools (namespaced, backend "minierp_finance") ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

@mcp.tool(name="minierp_finance_get_invoice_details")
async def minierp_finance_get_invoice_details(session_id: str, invoice_number: str, company_id: int | None = None) -> str:
    """Get header-level details (total, tax, unpaid balance, terms) for one AR invoice by invoice/reference number.

    Not customer-ownership-gated (ARInvoice has no customer link field in this
    schema) -- same trust model as minierp_orders_get_order_details."""
    return await _govern("get_invoice_details", session_id, "", {
        "invoice_number": invoice_number, "company_id": company_id,
    })

@mcp.tool(name="minierp_finance_get_vendor_details")
async def minierp_finance_get_vendor_details(session_id: str, vendor_code: str) -> str:
    """Get a vendor's profile: class, terms, currency, default payment method, 1099 flag."""
    return await _govern("get_vendor_details", session_id, "", {"vendor_code": vendor_code})


@mcp.tool(name="minierp_finance_get_vendor_ap_invoices")
async def minierp_finance_get_vendor_ap_invoices(
    session_id: str, vendor_code: str, page: int = 1, page_size: int = 10,
) -> str:
    """List AP invoices/bills for a vendor by vendor code."""
    return await _govern("get_vendor_ap_invoices", session_id, "", {
        "vendor_code": vendor_code, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_finance_get_ap_invoice_details")
async def minierp_finance_get_ap_invoice_details(
    session_id: str, invoice_number: str, company_id: int | None = None,
) -> str:
    """Get header-level details (total, tax, due date, paid status) for one AP invoice by invoice/reference number."""
    return await _govern("get_ap_invoice_details", session_id, "", {
        "invoice_number": invoice_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_finance_get_po_order_status")
async def minierp_finance_get_po_order_status(
    session_id: str, po_number: str, company_id: int | None = None,
) -> str:
    """Get header-level status (status, total, vendor, ship-via, hold) for one purchase order by PO number."""
    return await _govern("get_po_order_status", session_id, "", {
        "po_number": po_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_finance_get_gl_account_transactions")
async def minierp_finance_get_gl_account_transactions(
    session_id: str,
    account_cd: str,
    start_date: str = "",
    end_date: str = "",
    company_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List GL transactions for one account code, optionally filtered by date range, with a net debit/credit movement for the returned page."""
    return await _govern("get_gl_account_transactions", session_id, "", {
        "account_cd": account_cd, "start_date": start_date, "end_date": end_date,
        "company_id": company_id, "page": page, "page_size": page_size,
    })


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ ASGI app: MCP handler + Layer 1 edge + monitoring ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

_DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"
_DASHBOARD_HTML = _DASHBOARD_PATH.read_text(encoding="utf-8") if _DASHBOARD_PATH.exists() else "<h1>governance gateway</h1>"
_ADMIN_PATH = Path(__file__).parent / "static" / "admin.html"
_ADMIN_HTML = _ADMIN_PATH.read_text(encoding="utf-8") if _ADMIN_PATH.exists() else "<h1>governance admin</h1>"


def _static(name: str, fallback: str) -> str:
    p = Path(__file__).parent / "static" / name
    return p.read_text(encoding="utf-8") if p.exists() else fallback


_ACCOUNT_HTML = _static("account.html", "<h1>my access</h1>")
_SIGNUP_HTML = _static("signup.html", "<h1>sign up</h1>")
_CHAT_HTML = _static("chat.html", "<h1>assistant</h1>")
_APP_HTML = _static("app.html", "<h1>governance</h1>")

_LOGO_PATH = Path(__file__).parent / "static" / "frontier-logo.png"
_LOGO_BYTES = _LOGO_PATH.read_bytes() if _LOGO_PATH.exists() else b""


_COOKIE = "gov_session"
_COOKIE_SECURE = (os.getenv("GOVERNANCE_COOKIE_SECURE") or "true").lower() != "false"
_SESSION_TTL = int(os.getenv("GOVERNANCE_SESSION_TTL_SEC") or str(60 * 60))

# Chat history: a conversation idle this long is closed + summarized (chat_log.py),
# checked both lazily (on the next message to that conversation_id) and by the
# background sweep below (the general guarantee -- catches abandoned tabs).
_CHAT_IDLE_SEC = int(os.getenv("GOVERNANCE_CHAT_IDLE_SEC") or str(60 * 60))
_CHAT_SWEEP_INTERVAL_SEC = int(os.getenv("GOVERNANCE_CHAT_SWEEP_INTERVAL_SEC") or "300")

_LOGIN_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Frontier Governance ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â Sign in</title>
<style>
:root{--highlight:#2FC7BA;--highlight-darker:#2ab3a7;--gray900:#212121;--gray700:#A1A1A1;
--gray200:#E7E7E7;--gray50:#F6F7F8;--error:#f44336}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
background:linear-gradient(135deg,#eafaf8,var(--gray50));font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--gray900)}
.card{background:#fff;border:1px solid var(--gray200);border-radius:16px;padding:2rem 1.9rem;width:22rem;
box-shadow:0 10px 40px -12px rgba(33,33,33,.22)}
.logo{width:46px;height:46px;border-radius:12px;background:linear-gradient(135deg,var(--highlight),#5ad6cb);
display:grid;place-items:center;color:#fff;font-weight:700;font-size:1.4rem;margin-bottom:1rem}
h2{margin:0 0 .15rem;font-size:1.25rem}p.sub{margin:0 0 1.4rem;color:var(--gray700);font-size:.85rem}
label{display:block;font-size:.78rem;font-weight:600;color:var(--gray700);margin:.7rem 0 .25rem}
input{width:100%;padding:.6rem .7rem;border:1px solid var(--gray200);border-radius:9px;font:inherit}
input:focus{outline:2px solid var(--highlight);border-color:transparent}
button{width:100%;margin-top:1.2rem;padding:.65rem;border:0;border-radius:9px;background:var(--highlight);
color:#fff;font-weight:650;font-size:.95rem;cursor:pointer}button:hover{background:var(--highlight-darker)}
#e{color:var(--error);font-size:.83rem;min-height:1.1rem;margin:.6rem 0 0;text-align:center}
.foot{margin:1rem 0 0;text-align:center;font-size:.82rem;color:var(--gray700)}
.foot a{color:var(--highlight);text-decoration:none;font-weight:600}
</style></head><body>
<form class=card id=f>
<div class=logo>F</div>
<h2>Frontier Governance</h2><p class=sub>Sign in to the control plane.</p>
<label for=u>Username</label><input id=u autofocus autocomplete=username>
<label for=p>Password</label><input id=p type=password autocomplete=current-password>
<button>Sign in</button><p id=e></p>
<p class=foot>No account? <a href=/dashboard/signup>Create one</a></p>
</form>
<script>f.onsubmit=async e=>{e.preventDefault();const r=await fetch('/dashboard/login',
{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});
if(r.ok){location='/dashboard'}else{const j=await r.json().catch(()=>({}));document.getElementById('e').textContent=j.error||'Sign in failed'}}</script>
</body></html>"""


def _session(request) -> dict | None:
    return verify_session(request.cookies.get(_COOKIE))


def _unauthorized(is_admin: bool = False):
    return JSONResponse({"error": "forbidden" if is_admin else "unauthorized"},
                        status_code=403 if is_admin else 401)


async def _login(request):
    if not login_enabled():
        return JSONResponse({"error": "login is not configured"}, status_code=500)
    client_ip = ctx.client_ip(request)
    ua = request.headers.get("user-agent", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    lock_key = f"{username}|{client_ip}"

    if lockout.is_locked(lock_key):
        audit.log_auth_denied(path="/dashboard/login", client_ip=client_ip, user_agent=ua, reason="locked_out")
        return JSONResponse({"error": "too many attempts; try again later"}, status_code=429)

    record = get_store().get_by_username(username)
    if record is None or not verify_password(password, record.login_password_hash):
        tripped = lockout.record_failure(lock_key)
        audit.log_auth_denied(path="/dashboard/login", client_ip=client_ip, user_agent=ua,
                               reason="locked_out" if tripped else "bad_credentials")
        return JSONResponse({"error": "invalid username or password"}, status_code=401)

    lockout.reset(lock_key)
    token = issue_session(record.consumer_id, record.name, record.role, _SESSION_TTL)
    resp = JSONResponse({"ok": True, "name": record.name, "role": record.role})
    resp.set_cookie(_COOKIE, token, max_age=_SESSION_TTL, httponly=True,
                    secure=_COOKIE_SECURE, samesite="strict", path="/")
    return resp


async def _logout(_request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE, path="/")
    return resp


async def _me(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    return JSONResponse({"name": claims["name"], "role": claims["role"]})


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Account lifecycle (self-service) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

def _tool_info(names) -> list[dict]:
    """Non-technical view of a set of canonical tool names, for any UI a normal
    user (not an admin) looks at -- name + a plain-English description, falling
    back to the raw name if a tool somehow has none."""
    out = []
    for n in sorted(names):
        policy = manifest.get(n)
        out.append({
            "name": n,
            "description": (policy.description if policy else "") or n,
            "risk": policy.risk if policy else None,
            "approvalRequired": bool(policy.approval_required) if policy else False,
        })
    return out


def _effective_access_view(record) -> dict:
    """Human-readable view of a principal's resolved grant (for 'My Access')."""
    grant = resolve_grant(record, get_store().get_category, get_store().get_department)
    view = {}
    if grant.all_tools:
        for b in manifest.backends():
            view[b] = {"tools": _tool_info(manifest.tools_for_backend(b)), "levels": sorted(grant.levels_for(b))}
    else:
        for b, tools in grant.tools_by_backend.items():
            view[b] = {"tools": _tool_info(tools), "levels": sorted(grant.levels_for(b))}
    return view


def _effective_category_ids(store, record) -> set[str]:
    """Category ids this principal already effectively holds -- its own `categories`
    plus its department's CURRENT ones, if any (live, same as policy/resolve.py).
    Used to grey out "already granted" options in the self-service request-access
    picker; not a security boundary (that's still resolve()/decide())."""
    ids = set(record.categories or [])
    if record.department:
        dept = store.get_department(record.department)
        if dept:
            ids |= set(dept.categories)
    return ids


async def _categories_catalog(request):
    """Self-service (any logged-in user, not admin-only): the full category catalog,
    each with its actual tool list, for the 'My Access' request-access picker --
    category groups the picker (and supplies backend + redaction levels at approval
    time), but the user selects individual TOOLS within it, not the whole category."""
    if not _session(request):
        return _unauthorized()
    out = []
    for c in get_store().categories():
        names = manifest.tools_for_backend(c.backend) if c.tools == "*" else c.tools
        out.append({
            "id": c.id, "display_name": c.display_name, "backend": c.backend,
            "tools": _tool_info(names),
            # Gateway-level permission markers (files/workflow_runner/workflow_admin/
            # agent_admin) have no MCP tools of their own -- data_domains is the only
            # explanation the picker can show for what holding them actually does.
            "data_domains": list(getattr(c, "data_domains", None) or []),
        })
    return JSONResponse({"categories": out})


async def _departments(_request):
    """Public: the signup form's department options (id/display_name/current categories).
    Reads the live, admin-editable store (not the code seed), so an admin's edits to a
    department show up here immediately too."""
    return JSONResponse({"departments": [
        {"id": d.id, "display_name": d.display_name, "categories": list(d.categories)}
        for d in get_store().departments()
    ]})


async def _signup(request):
    store, err = _writable_or_error()
    if err:
        return err
    if not login_enabled():
        return JSONResponse({"error": "signup is not configured"}, status_code=500)
    body = await request.json()
    full_name = str(body.get("full_name") or "").strip()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    department_id = str(body.get("department") or "").strip()
    if not full_name or not username or not password or not department_id:
        return JSONResponse(
            {"error": "full name, username, password, and department are required"}, status_code=400)
    department = store.get_department(department_id)
    if department is None:
        return JSONResponse(
            {"error": f"unknown department {department_id!r}; one of: "
                      f"{sorted(d.id for d in store.departments())}"},
            status_code=400)
    consumer_id = f"user:{username}"
    if store.get_by_username(username) or store.get_consumer(consumer_id):
        return JSONResponse({"error": "that username is taken"}, status_code=409)
    # categories stays empty -- the department's CURRENT categories are resolved live
    # on every request (policy/resolve.py), not copied here. Editing the department
    # later reaches this user automatically; no per-user field to keep in sync.
    store.upsert_consumer(ConsumerRecord(
        consumer_id=consumer_id, name=username, key_hash="", status="active",
        role="user", type="user", categories=[],
        login_password_hash=hash_password(password),
        full_name=full_name, department=department.id,
    ))
    audit.log_policy_change(actor=username, action="signup", target=consumer_id,
                            detail=f"department={department.id}")

    token = issue_session(consumer_id, username, "user", _SESSION_TTL)
    resp = JSONResponse({"ok": True, "status": "active", "name": username}, status_code=201)
    resp.set_cookie(_COOKIE, token, max_age=_SESSION_TTL, httponly=True,
                    secure=_COOKIE_SECURE, samesite="strict", path="/")
    return resp


async def _my_access(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record:
        return JSONResponse({"name": claims["name"], "status": "unknown"})
    active = record.status == "active"
    my_requests = [r for r in get_store().list_access_requests() if r.get("consumer_id") == record.consumer_id]
    return JSONResponse({
        "name": record.name, "full_name": record.full_name, "department": record.department,
        "status": record.status, "role": record.role,
        "type": record.type, "categories": record.categories,
        "effective_categories": sorted(_effective_category_ids(get_store(), record)),
        "has_key": bool(record.key_hash),
        "access": _effective_access_view(record) if active else {},
        "requests": my_requests,
    })


async def _my_key_rotate(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    store, err = _writable_or_error()
    if err:
        return err
    record = store.get_consumer(claims["sub"])
    if not record:
        return JSONResponse({"error": "not found"}, status_code=404)
    if record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    api_key = generate_api_key()
    _now = time.time()
    store.upsert_consumer(replace(record, key_hash=hash_api_key(api_key),
                                  key_created_at=record.key_created_at or _now, key_rotated_at=_now))
    audit.log_policy_change(actor=record.name, action="rotate_own_key", target=record.consumer_id)
    return JSONResponse({"api_key": api_key})


async def _request_access(request):
    """Self-service: request specific TOOLS, grouped/picked by category (category
    supplies the backend + redaction levels so a granted tool isn't just returned
    masked to nothing -- a normal user shouldn't have to reason about PUBLIC/
    INTERNAL/PII/SENSITIVE directly). Admin reviews via /admin/requests; approving
    merges the requested tools (+ that category's levels) into the consumer's
    `overrides` for that backend (see _admin_request_approve) -- categories/
    department stay untouched, so this is purely additive on top of them."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    store, err = _writable_or_error()
    if err:
        return err
    record = store.get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    body = await request.json()
    raw_selections = body.get("selections") or []
    if not raw_selections:
        return JSONResponse({"error": "at least one category with tools is required"}, status_code=400)

    grant = resolve_grant(record, store.get_category, store.get_department)
    selections = []
    for sel in raw_selections:
        cat_id = str(sel.get("category") or "").strip()
        category = store.get_category(cat_id)
        if category is None:
            return JSONResponse({"error": f"unknown category {cat_id!r}"}, status_code=400)
        available = set(manifest.tools_for_backend(category.backend)) if category.tools == "*" else set(category.tools)
        requested_tools = [str(t).strip() for t in (sel.get("tools") or []) if str(t).strip()]
        invalid = sorted(set(requested_tools) - available)
        if invalid:
            return JSONResponse(
                {"error": f"{cat_id}: not part of this category: {', '.join(invalid)}"}, status_code=400)
        new_tools = sorted({t for t in requested_tools if not grant.allows_tool(category.backend, t)})
        if new_tools:
            selections.append({"category": cat_id, "backend": category.backend, "tools": new_tools})
    if not selections:
        return JSONResponse({"error": "you already have all of the selected tools"}, status_code=400)

    req = {
        "id": uuid.uuid4().hex[:12], "kind": "access", "consumer_id": record.consumer_id,
        "username": record.name, "selections": selections,
        "justification": str(body.get("justification") or ""), "status": "pending",
        "created_at": time.time(),
    }
    store.add_access_request(req)
    audit.log_policy_change(
        actor=record.name, action="request_access", target=record.consumer_id,
        detail="; ".join(f"{s['category']}: {', '.join(s['tools'])}" for s in selections),
    )
    return JSONResponse({"ok": True, "id": req["id"]}, status_code=201)




# Reusable template registry. Admins manage versioned templates; users browse
# active templates that workflows and artifact generators can consume.

async def _templates(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        include_disabled = claims.get("role") == "admin" and request.query_params.get("include_disabled") == "1"
        records = template_store.list_templates(
            template_type=request.query_params.get("type"),
            include_disabled=include_disabled,
        )
        return JSONResponse({"templates": [r.public_dict(include_versions=False, include_content=False) for r in records]})
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json()
        record = template_store.create_template(
            display_name=str(body.get("display_name") or body.get("displayName") or "Template"),
            template_type=str(body.get("template_type") or body.get("templateType") or "generic"),
            content=body.get("content") or {},
            created_by=claims["name"],
            description=str(body.get("description") or ""),
            classification=list(body.get("classification") or [manifest.INTERNAL]),
            tags=list(body.get("tags") or []),
            allowed_workflow_ids=list(body.get("allowed_workflow_ids") or body.get("allowedWorkflowIds") or []),
            notes=str(body.get("notes") or ""),
            template_id=str(body.get("template_id") or body.get("templateId") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - validation to client
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="create_template", target=record.template_id, detail=record.template_type)
    return JSONResponse(record.public_dict(include_versions=True, include_content=True), status_code=201)


async def _template_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = template_store.get_template(request.path_params["tid"])
    if record is None or (record.status == "disabled" and claims.get("role") != "admin"):
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "GET":
        include_content = request.query_params.get("content") == "1" or claims.get("role") == "admin"
        return JSONResponse(record.public_dict(include_versions=True, include_content=include_content))
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json()
        updated = template_store.update_template(
            record.template_id,
            display_name=body.get("display_name", body.get("displayName", record.display_name)),
            description=body.get("description", record.description),
            classification=body.get("classification", record.classification),
            tags=body.get("tags", record.tags),
            allowed_workflow_ids=body.get("allowed_workflow_ids", body.get("allowedWorkflowIds", record.allowed_workflow_ids)),
            status=body.get("status", record.status),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="update_template", target=record.template_id)
    return JSONResponse(updated.public_dict(include_versions=True, include_content=True))


async def _template_versions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    record = template_store.get_template(request.path_params["tid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
        updated = template_store.add_version(record.template_id, content=body.get("content") or {}, created_by=claims["name"], notes=str(body.get("notes") or ""))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="version_template", target=record.template_id, detail=f"v{updated.current_version}")
    return JSONResponse(updated.public_dict(include_versions=True, include_content=True), status_code=201)


async def _template_disable(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    updated = template_store.disable_template(request.path_params["tid"])
    if updated is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    audit.log_policy_change(actor=claims["name"], action="disable_template", target=updated.template_id)
    return JSONResponse(updated.public_dict(include_versions=True, include_content=True))


# Email send queue API. Actual external delivery is intentionally adapter-backed;
# the local path proves approval validation and durable queueing.

async def _email_sends(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    records = [r.public_dict() for r in email_send_store.list_sends(owner=owner, limit=limit)]
    return JSONResponse({"sends": records})


async def _email_send_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = email_send_store.get_send(request.path_params["sid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if record.owner != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    return JSONResponse(record.public_dict())

# Calendar send queue API mirrors email: external event creation is connector-backed
# and requires an approved invite artifact before it leaves the system.

async def _calendar_sends(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    records = [r.public_dict() for r in calendar_send_store.list_sends(owner=owner, limit=limit)]
    return JSONResponse({"sends": records})


async def _calendar_send_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = calendar_send_store.get_send(request.path_params["sid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if record.owner != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    return JSONResponse(record.public_dict())

def _knowledge_allowed(claims: dict) -> bool:
    if claims.get("role") == "admin":
        return True
    record = get_store().get_consumer(claims.get("sub", ""))
    return bool(record and "knowledge" in _effective_category_ids(get_store(), record))


async def _knowledge_documents(request):
    """Read-only: lists whatever local documents already exist (e.g. from before
    this backend was switched to read-only, or local dev/test seeding). There is
    no POST -- new documents are never added through this gateway; they go
    through AraTestEnvBE's own ingestion pipeline into the shared knowledge base."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    owner = None if claims["role"] == "admin" and request.query_params.get("all") == "1" else claims["name"]
    docs = knowledge_store.list_documents(owner=owner)
    return JSONResponse({"documents": [d.public_dict() for d in docs]})


async def _knowledge_document_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    doc = knowledge_store.get_document(request.path_params["did"])
    if doc is None or (claims["role"] != "admin" and doc.owner != claims["name"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "DELETE":
        knowledge_store.delete_document(doc.document_id, None if claims["role"] == "admin" else claims["name"])
        audit.log_policy_change(actor=claims["name"], action="delete_knowledge", target=doc.document_id, detail=doc.filename)
        return JSONResponse({"ok": True})
    return JSONResponse({"document": doc.public_dict()})


async def _knowledge_search(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    body = await request.json()
    query = str(body.get("query") or "")
    limit = int(body.get("limit") or 5)
    document_id = str(body.get("document_id") or body.get("documentId") or "")
    try:
        raw = await backends.call("knowledge", "search_knowledge", {"owner": claims["name"], "query": query, "limit": limit, "document_id": document_id})
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="search_knowledge", target=document_id or "all", detail=query[:120])
    return JSONResponse(payload)


async def _knowledge_answer(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    body = await request.json()
    query = str(body.get("query") or "")
    limit = int(body.get("limit") or 5)
    document_id = str(body.get("document_id") or body.get("documentId") or "")
    try:
        raw = await backends.call("knowledge", "answer_from_knowledge", {"owner": claims["name"], "query": query, "limit": limit, "document_id": document_id})
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="answer_knowledge", target=document_id or "all", detail=query[:120])
    return JSONResponse(payload)


# Approval queue API. Users can request approvals; admins decide them.

async def _approvals(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
        include_decided = request.query_params.get("include_decided", "1").lower() not in ("0", "false", "no")
        approvals = approval_store.list_approvals(requested_by=owner, include_decided=include_decided)
        return JSONResponse({"approvals": [a.public_dict() for a in approvals]})
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason") or "Approval requested").strip()
    approval = approval_store.create_approval(
        requested_by=claims["name"],
        reason=reason,
        risk_level=str(body.get("risk_level") or "medium"),
        artifact_ids=list(body.get("artifact_ids") or []),
        workflow_run_id=str(body.get("workflow_run_id") or ""),
    )
    audit.log_policy_change(actor=claims["name"], action="request_approval", target=approval.approval_id, detail=reason)
    return JSONResponse(approval.public_dict(), status_code=201)


async def _approval_decide(request):
    claims, err = _require_admin(request)
    if err:
        return err
    action = request.path_params["action"]
    status = "approved" if action == "approve" else "denied" if action == "deny" else ""
    if not status:
        return JSONResponse({"error": "action must be approve or deny"}, status_code=400)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    approval = approval_store.decide_approval(
        request.path_params["aid"], approver=claims["name"], status=status, note=str(body.get("note") or ""),
    )
    if approval is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if approval.workflow_run_id:
        run = workflows.get_run_record(approval.workflow_run_id)
        if run and run.status == "approval_required":
            steps = []
            for step in run.steps:
                if step.type == "approval" and step.status in ("pending", "running") and (not step.outputs.get("approvalId") or step.outputs.get("approvalId") == approval.approval_id):
                    outputs = dict(step.outputs)
                    outputs.update({"approvalId": approval.approval_id, "approvalStatus": approval.status, "approver": approval.approver})
                    steps.append(replace(step, status=("completed" if approval.status == "approved" else "failed"), outputs=outputs, error=(approval.decision_note if approval.status == "denied" else None)))
                else:
                    steps.append(step)
            workflows.update_run(run, steps=steps, status=("approval_required" if approval.status == "approved" else "failed"), error=(approval.decision_note if approval.status == "denied" else None))
    audit.log_policy_change(actor=claims["name"], action=f"{status}_approval", target=approval.approval_id)
    return JSONResponse(approval.public_dict())



# Autonomous agent profiles. Agents are backing ConsumerRecords plus a narrower
# profile that constrains which workflow templates scheduled jobs may run.

async def _admin_agents(request):
    claims, err = _require_admin_or_category(request, "agent_admin")
    if err:
        return err
    if request.method == "GET":
        include_disabled = request.query_params.get("includeDisabled", "1") != "0"
        return JSONResponse({"agents": [a.public_dict() for a in agent_store.list_agents(include_disabled=include_disabled)]})
    store, err = _writable_or_error()
    if err:
        return err
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    display_name = str(body.get("display_name") or body.get("displayName") or "Office automation agent").strip()
    consumer_id = str(body.get("consumer_id") or body.get("consumerId") or display_name.lower().replace(" ", "_")).strip()
    if not consumer_id:
        return JSONResponse({"error": "consumer_id is required"}, status_code=400)
    if store.get_consumer(consumer_id) or agent_store.get_agent_by_consumer(consumer_id):
        return JSONResponse({"error": f"agent consumer {consumer_id} already exists"}, status_code=409)
    allowed = [str(x).strip() for x in list(body.get("allowed_template_ids") or body.get("allowedTemplateIds") or []) if str(x).strip()]
    invalid = [tid for tid in allowed if workflows.get_template(tid) is None]
    if invalid:
        return JSONResponse({"error": "unknown workflow template", "templateIds": invalid}, status_code=400)
    categories = [str(x).strip() for x in list(body.get("categories") or []) if str(x).strip()]
    api_key = generate_api_key()
    now = time.time()
    consumer = ConsumerRecord(
        consumer_id=consumer_id,
        name=consumer_id,
        key_hash=hash_api_key(api_key),
        status="active",
        role="user",
        type="agent",
        categories=categories,
        key_created_at=now,
        key_rotated_at=now,
    )
    store.upsert_consumer(consumer)
    try:
        max_runs_per_day = int(body.get("max_runs_per_day") or body.get("maxRunsPerDay") or 24)
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_runs_per_day must be an integer"}, status_code=400)
    profile = agent_store.create_agent(
        display_name=display_name,
        consumer_id=consumer_id,
        description=str(body.get("description") or ""),
        categories=categories,
        allowed_template_ids=allowed,
        max_runs_per_day=max_runs_per_day,
        created_by=claims["name"],
    )
    audit.log_policy_change(actor=claims["name"], action="create_agent", target=profile.agent_id, detail=consumer_id)
    return JSONResponse({"agent": profile.public_dict(), "api_key": api_key}, status_code=201)


async def _admin_agent_item(request):
    claims, err = _require_admin_or_category(request, "agent_admin")
    if err:
        return err
    profile = agent_store.get_agent(request.path_params["aid"])
    if profile is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "GET":
        return JSONResponse(profile.public_dict())
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    changes = {}
    if "display_name" in body or "displayName" in body:
        changes["display_name"] = str(body.get("display_name") or body.get("displayName") or profile.display_name).strip()
    if "description" in body:
        changes["description"] = str(body.get("description") or "")
    if "categories" in body:
        categories = [str(x).strip() for x in list(body.get("categories") or []) if str(x).strip()]
        changes["categories"] = categories
        store, store_err = _writable_or_error()
        if store_err:
            return store_err
        consumer = store.get_consumer(profile.consumer_id)
        if consumer:
            store.upsert_consumer(replace(consumer, categories=categories))
    if "allowed_template_ids" in body or "allowedTemplateIds" in body:
        allowed = [str(x).strip() for x in list(body.get("allowed_template_ids") or body.get("allowedTemplateIds") or []) if str(x).strip()]
        invalid = [tid for tid in allowed if workflows.get_template(tid) is None]
        if invalid:
            return JSONResponse({"error": "unknown workflow template", "templateIds": invalid}, status_code=400)
        changes["allowed_template_ids"] = allowed
    if "max_runs_per_day" in body or "maxRunsPerDay" in body:
        try:
            changes["max_runs_per_day"] = max(1, int(body.get("max_runs_per_day") or body.get("maxRunsPerDay") or profile.max_runs_per_day))
        except (TypeError, ValueError):
            return JSONResponse({"error": "max_runs_per_day must be an integer"}, status_code=400)
    if "status" in body:
        status = str(body.get("status") or "").strip().lower()
        if status not in ("active", "disabled"):
            return JSONResponse({"error": "status must be active or disabled"}, status_code=400)
        changes["status"] = status
        store, store_err = _writable_or_error()
        if store_err:
            return store_err
        consumer = store.get_consumer(profile.consumer_id)
        if consumer:
            store.upsert_consumer(replace(consumer, status="active" if status == "active" else "disabled"))
    updated = agent_store.update_agent(profile.agent_id, **changes) if changes else profile
    audit.log_policy_change(actor=claims["name"], action="update_agent", target=profile.agent_id, detail=updated.status)
    return JSONResponse(updated.public_dict())


# Automation schedules. The local runner is deterministic: POST /automations/run-due
# executes due jobs once, which is enough for smoke tests and can be called by a
# cron/Timer trigger in production.

async def _execute_workflow_for_record(record, template_id: str, body: dict, source: str = "manual") -> dict:
    template = workflows.get_template(template_id)
    if template is None:
        return {"error": "unknown workflow template", "status_code": 404, "status": "failed"}
    preflight = _workflow_preflight_for(record, template_id, body)
    if not preflight.get("ready"):
        blockers = list(preflight.get("blockers") or [])
        status_code = 400 if preflight.get("missingInputs") and len(blockers) == 1 else 403
        audit.log_policy_change(actor=record.name, action="block_workflow_preflight", target=template_id, detail="; ".join(blockers))
        return {"error": "; ".join(blockers) or "workflow is not ready", "status_code": status_code, "status": "failed", "preflight": preflight}
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    run = workflows.new_run(template_id, record.name, body)
    audit.log_policy_change(actor=record.name, action="start_workflow", target=run.run_id, detail=f"{template_id}; source={source}")
    runners = {
        "customer_360_report": _run_customer_360_workflow,
        "shipment_exception_report": _run_shipment_exception_workflow,
        "vendor_ap_summary": _run_vendor_ap_summary_workflow,
        "customer_email_draft": _run_customer_email_draft_workflow,
        "weekly_executive_brief": _run_weekly_executive_brief_workflow,
    }
    runner = runners.get(template_id)
    if runner is not None:
        result = await runner(run, body)
    else:
        graph = workflow_graph_store.get_active_graph(template_id)
        if graph is None:
            run = workflows.update_run(run, status="failed", error="workflow template is not executable yet")
            return {"error": "workflow template is not executable yet", **workflows.run_dict(run)}
        # Pin this run to the exact graph version it started with -- if the owner
        # publishes a newer version while this run is paused mid-graph, resume must
        # keep walking the version the run began against, not whatever is newest.
        run = workflows.update_run(run, inputs={**run.inputs, "__graph_version": graph.published_version})
        result = await workflow_graph_interpreter.run_graph(run, graph, body)
    if result.get("error") and result.get("status_code"):
        workflows.update_run(run, status="failed", error=result["error"])
        return {"error": result["error"], "runId": run.run_id, "status_code": result["status_code"]}
    action = "workflow_requires_approval" if result.get("status") == "approval_required" else "complete_workflow"
    audit.log_policy_change(actor=record.name, action=action, target=result.get("runId", run.run_id), detail=f"{template_id}; source={source}")
    return result


async def _automations(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        owner = None if claims.get("role") == "admin" and request.query_params.get("all", "1") != "0" else claims["name"]
        records = [a.public_dict() for a in automation_store.list_automations(owner=owner)]
        return JSONResponse({"automations": records})
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    template_id = str(body.get("template_id") or body.get("templateId") or "").strip()
    template = workflows.get_template(template_id)
    if template is None:
        return JSONResponse({"error": "unknown workflow template"}, status_code=400)
    if template.get("status") != "active":
        return JSONResponse({"error": "workflow template is disabled"}, status_code=403)
    inputs = body.get("inputs") or {}
    if not isinstance(inputs, dict):
        return JSONResponse({"error": "inputs must be an object"}, status_code=400)
    try:
        interval_sec = int(body.get("interval_sec") or body.get("intervalSec") or 86400)
        next_run_at = body.get("next_run_at", body.get("nextRunAt"))
        next_run_at = float(next_run_at) if next_run_at is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid interval or next_run_at"}, status_code=400)
    agent_id = str(body.get("agent_id") or body.get("agentId") or "").strip()
    actor_type = "user"
    owner_name = record.name
    if agent_id:
        if claims.get("role") != "admin":
            return _unauthorized(is_admin=True)
        profile = agent_store.get_agent(agent_id)
        if profile is None:
            return JSONResponse({"error": "unknown agent"}, status_code=400)
        if profile.status != "active":
            return JSONResponse({"error": "agent is disabled"}, status_code=403)
        if not agent_store.allowed_for_template(profile, template_id):
            return JSONResponse({"error": "agent is not allowed to run this workflow"}, status_code=403)
        owner_name = profile.consumer_id
        actor_type = "agent"
    automation = automation_store.create_automation(
        owner=owner_name,
        template_id=template_id,
        display_name=str(body.get("display_name") or body.get("displayName") or template_id),
        inputs=inputs,
        interval_sec=interval_sec,
        next_run_at=next_run_at,
        actor_type=actor_type,
        agent_id=agent_id,
    )
    audit.log_policy_change(actor=claims["name"], action="create_automation", target=automation.automation_id, detail=f"{template_id}; actor={owner_name}")
    return JSONResponse(automation.public_dict(), status_code=201)


async def _automation_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    automation = automation_store.get_automation(request.path_params["aid"])
    if automation is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if automation.owner != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    if request.method == "DELETE":
        automation_store.delete_automation(automation.automation_id)
        audit.log_policy_change(actor=claims["name"], action="delete_automation", target=automation.automation_id)
        return JSONResponse({"ok": True})
    return JSONResponse(automation.public_dict())


async def _automation_run_due(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    now = float(body.get("now") or time.time())
    owner = str(body.get("owner") or "").strip() or None
    due = automation_store.due_automations(now=now, owner=owner)
    results = []
    store = get_store()
    controls = store.get_controls() if hasattr(store, "get_controls") else {}
    for auto in due:
        template = workflows.get_template(auto.template_id)
        if template is None or template.get("status") != "active":
            automation_store.mark_run(auto.automation_id, run_id="", status="template_disabled", now=now)
            results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "template_disabled"})
            continue
        agent = agent_store.get_agent(auto.agent_id) if auto.agent_id else None
        if auto.actor_type == "agent":
            if controls.get("paused_agents"):
                automation_store.mark_run(auto.automation_id, run_id="", status="agents_paused", now=now)
                results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "agents_paused"})
                continue
            if agent is None:
                automation_store.mark_run(auto.automation_id, run_id="", status="agent_missing", now=now)
                results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "agent_missing"})
                continue
            if not agent_store.allowed_for_template(agent, auto.template_id):
                automation_store.mark_run(auto.automation_id, run_id="", status="agent_not_allowed", now=now)
                results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "agent_not_allowed"})
                continue
        consumer = next((c for c in store.consumers() if c.name == auto.owner), None)
        if not consumer or consumer.status != "active":
            automation_store.mark_run(auto.automation_id, run_id="", status="owner_inactive", now=now)
            results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": "owner_inactive"})
            continue
        result = await _execute_workflow_for_record(consumer, auto.template_id, dict(auto.inputs), source=("agent:" + auto.agent_id if auto.agent_id else "automation:" + auto.automation_id))
        status = result.get("status") or "failed"
        automation_store.mark_run(auto.automation_id, run_id=result.get("runId", ""), status=status, now=now)
        if agent and status == "completed":
            agent_store.mark_run(agent.agent_id, run_id=result.get("runId", ""), now=now)
        results.append({"automationId": auto.automation_id, "agentId": auto.agent_id, "status": status, "run": result})
    return JSONResponse({"ran": len(results), "results": results})

# Workflow catalog and execution API. Customer 360 is executable now; the other
# templates are visible as draft roadmap items while their step runners land.
async def _admin_workflow_health(request):
    claims, err = _require_admin_or_category(request, "workflow_admin")
    if err:
        return err
    try:
        stuck_after = float(request.query_params.get("stuck_after_sec") or request.query_params.get("stuckAfterSec") or 1800)
    except (TypeError, ValueError):
        stuck_after = 1800
    summary = workflows.workflow_health(stuck_after_sec=stuck_after)
    return JSONResponse(summary)

async def _workflows(request):
    if not _session(request):
        return _unauthorized()
    return JSONResponse({"workflows": workflows.list_templates()})


async def _workflow_template(request):
    if not _session(request):
        return _unauthorized()
    template = workflows.get_template(request.path_params["tid"])
    if template is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(template)


async def _admin_workflow_template_status(request):
    claims, err = _require_admin_or_category(request, "workflow_admin")
    if err:
        return err
    template_id = request.path_params["tid"]
    template = workflows.get_template(template_id)
    if template is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    action = request.path_params["action"]
    status = "disabled" if action == "disable" else "active" if action == "enable" else ""
    if not status:
        return JSONResponse({"error": "unknown action"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    updated = workflows.set_template_status(template_id, status=status, actor=claims["name"], reason=str(body.get("reason") or ""))
    audit.log_policy_change(actor=claims["name"], action=f"{action}_workflow_template", target=template_id, detail=str(body.get("reason") or ""))
    return JSONResponse(updated)
def _workflow_input_requirements(template_id: str) -> list[dict]:
    base = {
        "customer_360_report": [
            {"name": "customer_id", "label": "Customer ID", "sampleDefault": "SAMPLE100", "requiredWhenSampleFalse": True},
        ],
        "shipment_exception_report": [
            {"name": "customer_id", "label": "Customer ID", "sampleDefault": "SAMPLE100", "requiredWhenSampleFalse": True},
        ],
        "vendor_ap_summary": [
            {"name": "vendor_code", "label": "Vendor Code", "sampleDefault": "VEND100", "requiredWhenSampleFalse": True},
        ],
        "customer_email_draft": [
            {"name": "customer_id", "label": "Customer ID", "sampleDefault": "SAMPLE100", "requiredWhenSampleFalse": True},
            {"name": "recipient", "label": "Email recipient", "optional": True},
            {"name": "topic", "label": "Email topic", "optional": True},
        ],
        "weekly_executive_brief": [
            {"name": "start_date", "label": "Start date", "optional": True},
            {"name": "end_date", "label": "End date", "optional": True},
        ],
    }
    if template_id in base:
        return [dict(item) for item in base[template_id]]
    # Graph-backed workflow: the trigger node carries its own input schema in the
    # same {name,label,requiredWhenSampleFalse} shape (workflow_graph_models.py).
    # get_published_graph (not get_active_graph) so this still resolves for a
    # disabled graph -- preflight's own "workflow template is disabled" blocker is
    # what actually stops execution, matching the hardcoded templates' behavior.
    graph = workflow_graph_store.get_published_graph(template_id)
    if graph is None:
        return []
    version = graph.version_record(graph.published_version)
    trigger = next((n for n in (version.nodes if version else []) if n.kind == "trigger"), None)
    return [dict(item) for item in (trigger.config.get("inputs") or [])] if trigger else []


def _workflow_preflight_for(record, template_id: str, inputs: dict | None = None) -> dict:
    template = workflows.get_template(template_id)
    if template is None:
        return {
            "ready": False,
            "templateId": template_id,
            "error": "unknown workflow template",
            "blockers": ["unknown workflow template"],
            "warnings": [],
        }
    inputs = dict(inputs or {})
    sample = bool(inputs.get("sample", True))
    store = get_store()
    grant = resolve_grant(record, store.get_category, store.get_department)
    effective_categories = sorted(_effective_category_ids(store, record))
    required_categories = list(template.get("requiredCategories") or template.get("required_categories") or [])
    required_set = set(required_categories)
    if workflows.is_graph_backed(template_id):
        # A graph's derived requiredCategories is a UNION across tool_call nodes,
        # and for a tool grantable by more than one category (e.g. send_email_draft
        # via EITHER email_send_internal or email_send_external) that union can list
        # alternatives that are not all individually required -- a flat AND-of-
        # categories check would wrongly block an owner who holds just one of them.
        # Check actual per-tool access instead (the same EffectiveGrant.allows_tool
        # check validate_graph already uses), which is exact rather than approximate.
        graph = workflow_graph_store.get_graph(template_id)
        version = graph.version_record(graph.published_version) if graph else None
        missing_categories = [] if grant.all_tools else workflow_graph_store.missing_tool_access(version, grant)
    else:
        missing_categories = [] if grant.all_tools else sorted(required_set - set(effective_categories))
    # Opt-in blanket gate (expansion.md §7.1 "workflow_runner"), off by default so
    # existing per-template requiredCategories keep working unchanged for
    # deployments that haven't explicitly turned this on.
    if (
        _workflow_runner_category_required()
        and not grant.all_tools
        and "workflow_runner" not in effective_categories
    ):
        missing_categories = sorted(set(missing_categories) | {"workflow_runner"})
    required_inputs = _workflow_input_requirements(template_id)
    missing_inputs = [
        item["name"] for item in required_inputs
        if item.get("requiredWhenSampleFalse") and not sample and not str(inputs.get(item["name"]) or "").strip()
    ]
    blockers: list[str] = []
    if template.get("status") != "active":
        blockers.append("workflow template is disabled")
    if missing_categories:
        blockers.append("missing required access categories: " + ", ".join(missing_categories))
    if missing_inputs:
        blockers.append("missing required inputs: " + ", ".join(missing_inputs))
    controls = store.get_controls() if hasattr(store, "get_controls") else {}
    paused_backends = set(controls.get("paused_backends") or [])
    connectors = []
    configured_backends = set()
    for category_id in required_categories:
        category = store.get_category(category_id)
        if category is None:
            connectors.append({"category": category_id, "backend": "", "configured": False, "error": "unknown category"})
            blockers.append(f"unknown required category: {category_id}")
            continue
        if category.backend in configured_backends:
            continue
        configured_backends.add(category.backend)
        try:
            url = backends.backend_url(category.backend)
            paused = category.backend in paused_backends
            connectors.append({"category": category_id, "backend": category.backend, "configured": True, "paused": paused, "url": url})
            if paused:
                blockers.append(f"backend is paused: {category.backend}")
        except Exception as exc:  # noqa: BLE001 - normalize configuration issues for UI
            connectors.append({"category": category_id, "backend": category.backend, "configured": False, "paused": category.backend in paused_backends, "error": str(exc)})
            blockers.append(f"backend is not configured: {category.backend}")
    warnings: list[str] = []
    if sample:
        warnings.append("sample mode is enabled; generated outputs are local test artifacts")
    if template_id == "customer_email_draft" and not str(inputs.get("recipient") or "").strip():
        warnings.append("no recipient supplied; draft will use manager@example.com")
    if template_id == "customer_email_draft" and bool(inputs.get("include_packet", True)):
        warnings.append("PDF packet will be generated with the email draft")
    approval_gates = []
    if template_id == "customer_email_draft":
        approval_gates.append({"id": "send_approval", "label": "Send approval", "enabled": bool(inputs.get("request_send_approval"))})
    if template_id == "weekly_executive_brief":
        approval_gates.append({"id": "review_approval", "label": "Review approval", "enabled": bool(inputs.get("request_approval"))})
    return {
        "ready": not blockers,
        "template": template,
        "templateId": template_id,
        "requiredCategories": required_categories,
        "effectiveCategories": effective_categories,
        "legacyAllowAll": bool(grant.all_tools),
        "pausedBackends": sorted(paused_backends & {c.get("backend") for c in connectors if c.get("backend")}),
        "missingCategories": missing_categories,
        "requiredInputs": required_inputs,
        "missingInputs": missing_inputs,
        "approvalGates": approval_gates,
        "connectors": connectors,
        "blockers": blockers,
        "warnings": warnings,
    }

async def _workflow_preflight(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.method == "POST" and request.headers.get("content-length") else {}
    except Exception:
        body = {}
    result = _workflow_preflight_for(record, request.path_params["tid"], body.get("inputs") if isinstance(body.get("inputs"), dict) else body)
    status = 404 if result.get("error") == "unknown workflow template" else 200
    audit.log_policy_change(actor=claims["name"], action="preflight_workflow", target=request.path_params["tid"], detail="ready" if result.get("ready") else "; ".join(result.get("blockers") or []))
    return JSONResponse(result, status_code=status)

async def _workflow_suggestions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    message = str(body.get("message") or body.get("prompt") or "").strip()
    try:
        limit = int(body.get("limit") or 4)
    except (TypeError, ValueError):
        limit = 4
    suggestions = workflows.workflow_suggestions(message, limit=limit)
    audit.log_policy_change(actor=claims["name"], action="suggest_workflows", target="assistant", detail=message[:120])
    return JSONResponse({"suggestions": suggestions})


async def _workflow_suggestion_launch(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    template_id = str(body.get("template_id") or body.get("templateId") or "").strip()
    inputs = body.get("inputs") or {}
    if not isinstance(inputs, dict):
        return JSONResponse({"error": "inputs must be an object"}, status_code=400)
    ctx.ip_ctx.set(ctx.client_ip(request))
    result = await _execute_workflow_for_record(record, template_id, inputs, source="assistant_suggestion")
    if result.get("error") and result.get("status_code"):
        return JSONResponse({"error": result["error"], "runId": result.get("runId")}, status_code=result["status_code"])
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result, status_code=201)

def _workflow_run_timeline(run: dict) -> list[dict]:
    run_id = run.get("runId", "")
    events: list[dict] = []
    for step in run.get("steps") or []:
        events.append({
            "kind": "step",
            "ts": run.get("updatedAt") or run.get("createdAt"),
            "status": step.get("status"),
            "title": step.get("title") or step.get("stepId"),
            "tool": step.get("tool") or "",
            "detail": step.get("error") or "",
            "step": step,
        })
    session_id = "workflow:" + run_id
    for record in audit.recent(1000):
        if record.get("session_id") == session_id:
            events.append({
                "kind": "audit",
                "ts": record.get("ts"),
                "status": record.get("status"),
                "title": record.get("tool"),
                "tool": record.get("tool"),
                "detail": record.get("detail") or record.get("args") or "",
                "audit": record,
            })
        elif record.get("type") == "policy_change" and run_id and str(record.get("tool") or "").endswith(" " + run_id):
            events.append({
                "kind": "policy",
                "ts": record.get("ts"),
                "status": record.get("status"),
                "title": record.get("tool"),
                "tool": record.get("tool"),
                "detail": record.get("detail") or "",
                "audit": record,
            })
    for artifact_id in run.get("artifactIds") or []:
        artifact = artifact_store.get_artifact(artifact_id)
        events.append({
            "kind": "artifact",
            "ts": artifact.created_at if artifact else run.get("updatedAt"),
            "status": "ready" if artifact else "missing",
            "title": artifact.filename if artifact else artifact_id,
            "tool": "artifact",
            "detail": ", ".join(artifact.classification) if artifact else "artifact metadata not found",
            "artifact": artifact.public_dict() if artifact else {"artifactId": artifact_id},
        })
    for approval_id in run.get("approvalIds") or []:
        approval = approval_store.get_approval(approval_id)
        events.append({
            "kind": "approval",
            "ts": approval.created_at if approval else run.get("updatedAt"),
            "status": approval.status if approval else "missing",
            "title": approval.reason if approval else approval_id,
            "tool": "approval",
            "detail": approval.note if approval else "approval metadata not found",
            "approval": approval.public_dict() if approval else {"approvalId": approval_id},
        })
    events.sort(key=lambda e: (e.get("ts") or 0, e.get("kind") or ""))
    return events

async def _workflow_runs(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
    return JSONResponse({"runs": workflows.list_runs(requested_by=owner)})


async def _workflow_run_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    if request.query_params.get("timeline") == "1" or request.query_params.get("include") == "timeline":
        return JSONResponse({**run, "timeline": _workflow_run_timeline(run)})
    return JSONResponse(run)


async def _workflow_run_export_evidence(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    timeline = _workflow_run_timeline(run)
    source_artifact_ids = list(run.get("artifactIds") or [])
    source_artifacts = [a.public_dict() for a in (artifact_store.get_artifact(aid) for aid in source_artifact_ids) if a]
    classification = sorted({label for artifact in source_artifacts for label in (artifact.get("classification") or [])} or {manifest.INTERNAL})
    payload = {
        "kind": "workflow_evidence_packet",
        "schemaVersion": 1,
        "exportedAt": time.time(),
        "exportedBy": claims["name"],
        "run": run,
        "timeline": timeline,
        "artifacts": source_artifacts,
        "approvals": [
            approval.public_dict() for approval in (approval_store.get_approval(aid) for aid in (run.get("approvalIds") or [])) if approval
        ],
    }
    safe_tid = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in str(run.get("templateId") or "workflow"))
    filename = f"workflow-evidence-{safe_tid}-{run['runId']}.json"
    record = artifact_store.create_artifact(
        owner=run.get("requestedBy") or claims["name"],
        title=f"Workflow Evidence - {run.get('templateId')}",
        filename=filename,
        payload=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        artifact_type="json",
        mime_type="application/json",
        classification=classification,
        source_workflow_run_id=run["runId"],
        source_tool_calls=["workflow_evidence_export"],
        source_artifact_ids=source_artifact_ids,
        retention_days=365,
    )
    audit.log_policy_change(actor=claims["name"], action="export_workflow_evidence", target=run["runId"], detail=record.artifact_id)
    return JSONResponse({"artifact": record.public_dict(), "timelineEvents": len(timeline)}, status_code=201)

async def _workflow_run_cancel(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    updated = workflows.cancel_run(run["runId"], actor=claims["name"], reason=str(body.get("reason") or "cancelled"))
    audit.log_policy_change(actor=claims["name"], action="cancel_workflow", target=run["runId"], detail=str(body.get("reason") or ""))
    return JSONResponse(workflows.run_dict(updated))


async def _workflow_run_resume(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    run = workflows.get_run(request.path_params["rid"])
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if run.get("requestedBy") != claims["name"] and claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    if run.get("status") != "approval_required":
        return JSONResponse(run)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    approval_id = str(body.get("approval_id") or body.get("approvalId") or "").strip()
    approvals = [approval_store.get_approval(aid) for aid in (run.get("approvalIds") or [])]
    approvals = [a for a in approvals if a is not None]
    if approval_id:
        approvals = [a for a in approvals if a.approval_id == approval_id]
    else:
        # A run can accumulate MORE than one approval over its lifetime (a graph run
        # can pause at an explicit approval_gate node, then later at a generalized
        # export-risk auto-pause on a downstream node) -- run.approvalIds never prunes
        # old ones, so without this filter "find any approved approval" could match a
        # stale, already-resolved approval from an earlier gate instead of the one
        # actually blocking the run right now. Restrict to approvals still referenced
        # by a pending/running "approval" step. No-op for the 5 hardcoded workflows,
        # which never have more than one live approval per run.
        pending_ids = {
            s.get("outputs", {}).get("approvalId")
            for s in (run.get("steps") or [])
            if s.get("type") == "approval" and s.get("status") in ("pending", "running")
        }
        pending_ids.discard(None)
        if pending_ids:
            approvals = [a for a in approvals if a.approval_id in pending_ids]
    if not approvals:
        return JSONResponse({"error": "approval is required before resume"}, status_code=409)
    if any(a.status == "denied" for a in approvals):
        record = workflows.get_run_record(run["runId"])
        updated = workflows.update_run(record, status="failed", error="approval denied") if record else None
        return JSONResponse(workflows.run_dict(updated), status_code=409)
    approved = next((a for a in approvals if a.status == "approved"), None)
    if not approved:
        return JSONResponse({"error": "approval is still pending"}, status_code=409)
    if workflows.is_graph_backed(run["templateId"]):
        graph = workflow_graph_store.get_graph(run["templateId"])
        record = workflows.get_run_record(run["runId"])
        result = await workflow_graph_interpreter.resume_graph(record, graph, actor=claims["name"])
        audit.log_policy_change(actor=claims["name"], action="resume_workflow", target=run["runId"], detail=approved.approval_id)
        return JSONResponse(result)
    updated = workflows.resume_run(run["runId"], approval_id=approved.approval_id, actor=claims["name"])
    audit.log_policy_change(actor=claims["name"], action="resume_workflow", target=run["runId"], detail=approved.approval_id)
    return JSONResponse(workflows.run_dict(updated))


# ── "My Workflow" -- user-buildable graph CRUD ────────────────────────────────
# Self-service (any active user, no admin gate): building/running your OWN graph
# needs no new category -- every tool_call node is still gated by the tools the
# owner already has access to (validate_graph at save time, _govern's live decide()
# at run time), so this is safe by construction. Only the owner (or an admin) may
# read/version/publish a given graph.

def _graph_node_from_wire(d: dict) -> dict:
    return {
        "node_id": d.get("node_id") or d.get("nodeId") or "",
        "kind": d.get("kind") or "",
        "title": d.get("title") or "",
        "tool": d.get("tool") or "",
        "config": d.get("config") or {},
        "input_bindings": d.get("input_bindings") or d.get("inputBindings") or {},
        "position": d.get("position") or {},
    }


def _graph_edge_from_wire(d: dict) -> dict:
    return {
        "edge_id": d.get("edge_id") or d.get("edgeId") or "",
        "source_node_id": d.get("source_node_id") or d.get("sourceNodeId") or "",
        "target_node_id": d.get("target_node_id") or d.get("targetNodeId") or "",
    }


async def _workflow_graphs(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if request.method == "GET":
        owner = None if claims.get("role") == "admin" and request.query_params.get("all") == "1" else claims["name"]
        records = workflow_graph_store.list_graphs(owner=owner)
        return JSONResponse({"graphs": [r.public_dict() for r in records]})
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    try:
        body = await request.json()
        new_graph = workflow_graph_store.create_graph(
            display_name=str(body.get("display_name") or body.get("displayName") or "My Workflow"),
            description=str(body.get("description") or ""),
            owner_record=record, get_category=store.get_category, get_department=store.get_department,
            nodes=[_graph_node_from_wire(n) for n in (body.get("nodes") or [])],
            edges=[_graph_edge_from_wire(e) for e in (body.get("edges") or [])],
            created_by=claims["name"], notes=str(body.get("notes") or ""),
            graph_id=str(body.get("graph_id") or body.get("graphId") or ""),
            reserved_ids=set(workflows.TEMPLATES.keys()),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="create_workflow_graph", target=new_graph.graph_id)
    return JSONResponse(new_graph.public_dict(include_nodes=True), status_code=201)


def _require_graph_owner(claims, gid: str):
    """Return (graph, None) or (None, error_response)."""
    graph = workflow_graph_store.get_graph(gid)
    if graph is None:
        return None, JSONResponse({"error": "not found"}, status_code=404)
    if graph.owner != claims["name"] and claims.get("role") != "admin":
        return None, _unauthorized(is_admin=True)
    return graph, None


async def _workflow_graph_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    return JSONResponse(graph.public_dict(include_nodes=True))


async def _workflow_graph_versions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None:
        return JSONResponse({"error": "account not found"}, status_code=403)
    try:
        body = await request.json()
        updated = workflow_graph_store.add_graph_version(
            graph.graph_id,
            nodes=[_graph_node_from_wire(n) for n in (body.get("nodes") or [])],
            edges=[_graph_edge_from_wire(e) for e in (body.get("edges") or [])],
            owner_record=record, get_category=store.get_category, get_department=store.get_department,
            created_by=claims["name"], notes=str(body.get("notes") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="version_workflow_graph", target=graph.graph_id, detail=f"v{updated.current_version}")
    return JSONResponse(updated.public_dict(include_nodes=True), status_code=201)


async def _workflow_graph_publish(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    version = body.get("version")
    try:
        updated = workflow_graph_store.publish_graph(graph.graph_id, version=int(version) if version is not None else None, actor=claims["name"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="publish_workflow_graph", target=updated.graph_id, detail=f"v{updated.published_version}")
    return JSONResponse(updated.public_dict())


async def _workflow_graph_validate(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    graph, err = _require_graph_owner(claims, request.path_params["gid"])
    if err:
        return err
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None:
        return JSONResponse({"error": "account not found"}, status_code=403)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    if body.get("nodes") is not None or body.get("edges") is not None:
        nodes = [_graph_node_from_wire(n) for n in (body.get("nodes") or [])]
        edges = [_graph_edge_from_wire(e) for e in (body.get("edges") or [])]
    else:
        version = graph.version_record()
        nodes = [_graph_node_from_wire(n.public_dict()) for n in (version.nodes if version else [])]
        edges = [_graph_edge_from_wire(e.public_dict()) for e in (version.edges if version else [])]
    owner_grant = resolve_grant(record, store.get_category, store.get_department)
    blockers = workflow_graph_store.validate_graph(nodes, edges, owner_grant=owner_grant)
    return JSONResponse({"ready": not blockers, "blockers": blockers, "warnings": []})


async def _workflow_graph_catalog(request):
    """Self-service tool palette for the graph builder -- any active user, filtered
    to THEIR own grant (drives the greyed-out/locked palette state; not itself a
    security check -- _govern's live decide() is what actually enforces access)."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is None:
        return JSONResponse({"error": "account not found"}, status_code=403)
    grant = resolve_grant(record, store.get_category, store.get_department)
    tools = await mcp.list_tools()
    specs = orchestrator.build_tool_specs(tools, None)
    out = []
    for spec in specs:
        fn = spec["function"]
        canonical = manifest.canonical(fn["name"]) or fn["name"]
        policy = manifest.get(canonical)
        granted = grant.all_tools if grant is not None else True
        if not granted and policy is not None:
            granted = grant.allows_tool(policy.backend, canonical)
        out.append({
            "name": fn["name"],
            "canonical": canonical,
            "description": fn["description"],
            "riskLevel": policy.risk if policy else None,
            "approvalRequired": bool(policy.approval_required) if policy else False,
            "backend": policy.backend if policy else None,
            "granted": granted,
            "parameters": fn["parameters"],
            "outputFields": sorted((policy.fields if policy else {}).keys()),
        })
    return JSONResponse({"tools": out})


def _workflow_runner_category_required() -> bool:
    return (os.getenv("GOVERNANCE_REQUIRE_WORKFLOW_RUNNER_CATEGORY") or "").strip().lower() in ("1", "true", "yes", "on")


def _workflow_table_row_count(tables: list[dict] | None) -> int:
    return sum(len(t.get("rows") or []) for t in (tables or []))


def _workflow_broad_export_threshold(canonical_tool: str = "create_excel_report") -> int:
    """Row-count gate for one export-risk tool. An explicit
    GOVERNANCE_BROAD_EXPORT_APPROVAL_ROWS env var always wins (operator override,
    e.g. for tests/tuning); otherwise fall back to the tool's own declared
    ToolPolicy.max_rows_without_approval (manifest.py §7.3), then a hardcoded 100 --
    so the declarative per-tool field is load-bearing wherever an operator hasn't
    overridden it."""
    env_value = os.getenv("GOVERNANCE_BROAD_EXPORT_APPROVAL_ROWS")
    if env_value is not None:
        try:
            return max(1, int(env_value))
        except (TypeError, ValueError):
            pass
    policy = manifest.get(canonical_tool)
    if policy is not None and policy.max_rows_without_approval is not None:
        return max(1, int(policy.max_rows_without_approval))
    return 100


def _workflow_requires_broad_export_approval(
    tables: list[dict] | None, classification: list[str] | None, body: dict,
    canonical_tool: str = "create_excel_report",
) -> tuple[bool, int, int]:
    if body.get("request_approval") or body.get("requestApproval"):
        return False, _workflow_table_row_count(tables), _workflow_broad_export_threshold(canonical_tool)
    labels = set(classification or [])
    row_count = _workflow_table_row_count(tables)
    threshold = _workflow_broad_export_threshold(canonical_tool)
    sensitive = bool(labels & {manifest.PII, manifest.SENSITIVE})
    return sensitive and row_count >= threshold, row_count, threshold


def _workflow_pause_for_broad_export(run, *, artifact_ids: list[str], row_count: int, threshold: int, step_id: str = "request_broad_export_approval"):
    """`step_id` defaults to the original hardcoded id (unchanged for the 5 hardcoded
    workflows' 3 existing call sites); the graph interpreter passes a per-node id
    (`"<node_id>__export_approval"`) so multiple export-risk nodes in one graph run
    each get their own distinguishable pause step instead of colliding on one id."""
    run = workflows.add_step(run, workflows.WorkflowStep(
        step_id=step_id,
        type="approval",
        status="running",
        tool="approval_store.create_approval",
        title="Request broad export approval",
        inputs={"rowCount": row_count, "threshold": threshold},
    ))
    approval = approval_store.create_approval(
        requested_by=run.requested_by,
        reason=f"Review broad sensitive export before distribution ({row_count} rows, threshold {threshold})",
        risk_level="high",
        artifact_ids=artifact_ids,
        workflow_run_id=run.run_id,
    )
    audit.log_policy_change(actor=run.requested_by, action="request_broad_export_approval", target=approval.approval_id, detail=run.run_id)
    run = workflows.update_run(run, steps=[
        replace(s, status="pending", outputs={"approvalId": approval.approval_id, "status": approval.status, "rowCount": row_count, "threshold": threshold})
        if s.step_id == step_id else s for s in run.steps
    ])
    run = workflows.mark_approval_required(run.run_id, approval_id=approval.approval_id, artifact_ids=artifact_ids) or run
    return run, approval

def _parse_tool_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"status": "error", "raw": raw}


async def _run_customer_360_workflow(run, body: dict) -> dict:
    customer_id = str(body.get("customer_id") or body.get("customerId") or "").strip()
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_customer_payload(customer_id or "SAMPLE100")
    else:
        if not customer_id:
            return {"error": "customer_id is required unless sample=true", "status_code": 400}
        session_id = "workflow:" + run.run_id
        payload = {"customer_id": customer_id}
        for step_id, title, canonical, args in [
            ("fetch_overview", "Fetch customer overview", "get_customer_overview", {}),
            ("fetch_order_summary", "Fetch order summary", "get_customer_order_summary", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
            }),
            ("fetch_orders", "Fetch recent orders", "get_customer_orders", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
                "page_size": int(body.get("page_size") or 25),
            }),
            ("fetch_shipments", "Fetch shipment status", "get_customer_shipment_status", {
                "max_orders": int(body.get("max_orders") or 5),
            }),
        ]:
            run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool=canonical, title=title))
            raw = await _govern(canonical, session_id, customer_id, args)
            result = _parse_tool_json(raw)
            run = workflows.complete_step(run, step_id, {"status": result.get("status"), "intent": result.get("intent")})
            key = {"fetch_overview": "overview", "fetch_order_summary": "order_summary", "fetch_orders": "orders", "fetch_shipments": "shipments"}[step_id]
            payload[key] = result

    tables = workflows.customer_360_tables(payload)
    sections = workflows.customer_360_sections(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.PII, manifest.SENSITIVE])
    artifacts = []
    for step_id, title, canonical, args in [
        ("build_excel", "Build XLSX appendix", "create_excel_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Customer 360 - {payload.get('customer_id', 'customer')}",
            "tables": tables,
            "classification": classification,
            "filename": f"customer-360-{payload.get('customer_id', 'customer')}.xlsx",
        }),
        ("build_deck", "Build PPTX deck", "create_powerpoint_deck", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Customer 360 - {payload.get('customer_id', 'customer')}",
            "sections": sections,
            "classification": classification,
            "filename": f"customer-360-{payload.get('customer_id', 'customer')}.pptx",
        }),
    ]:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="artifact", status="running", tool=canonical, title=title))
        raw = await _govern(canonical, "workflow:" + run.run_id, "", args)
        result = _parse_tool_json(raw)
        if result.get("status") != "success":
            run = workflows.fail_step(run, step_id, result.get("message") or result.get("errorCode") or "artifact generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, step_id, {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
        artifacts.append(result)
    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    needs_approval, row_count, threshold = _workflow_requires_broad_export_approval(tables, classification, body)
    if needs_approval:
        run, approval = _workflow_pause_for_broad_export(run, artifact_ids=artifact_ids, row_count=row_count, threshold=threshold)
        return {**workflows.run_dict(run), "artifacts": artifacts, "approval": approval.public_dict()}
    run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    return {**workflows.run_dict(run), "artifacts": artifacts}


async def _run_shipment_exception_workflow(run, body: dict) -> dict:
    customer_id = str(body.get("customer_id") or body.get("customerId") or "").strip()
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_shipment_payload(customer_id or "SAMPLE100")
    else:
        if not customer_id:
            return {"error": "customer_id is required unless sample=true", "status_code": 400}
        step_id = "fetch_shipments"
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool="get_customer_shipment_status", title="Fetch shipment status"))
        raw = await _govern("get_customer_shipment_status", "workflow:" + run.run_id, customer_id, {"max_orders": int(body.get("max_orders") or 20)})
        shipments = _parse_tool_json(raw)
        run = workflows.complete_step(run, step_id, {"status": shipments.get("status"), "intent": shipments.get("intent")})
        payload = {"customer_id": customer_id, "shipments": shipments}
    tables = workflows.shipment_exception_tables(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.SENSITIVE])
    args = {
        "owner": ctx.consumer_ctx.get() or run.requested_by,
        "title": f"Shipment Exceptions - {payload.get('customer_id', 'customer')}",
        "tables": tables,
        "classification": classification,
        "filename": f"shipment-exceptions-{payload.get('customer_id', 'customer')}.xlsx",
    }
    run = workflows.add_step(run, workflows.WorkflowStep(step_id="build_excel", type="artifact", status="running", tool="create_excel_report", title="Build shipment exception workbook"))
    result = _parse_tool_json(await _govern("create_excel_report", "workflow:" + run.run_id, "", args))
    if result.get("status") != "success":
        run = workflows.fail_step(run, "build_excel", result.get("message") or result.get("errorCode") or "artifact generation failed")
        return workflows.run_dict(run)
    run = workflows.complete_step(run, "build_excel", {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
    artifact_ids = [result.get("artifactId")]
    needs_approval, row_count, threshold = _workflow_requires_broad_export_approval(tables, classification, body)
    if needs_approval:
        run, approval = _workflow_pause_for_broad_export(run, artifact_ids=artifact_ids, row_count=row_count, threshold=threshold)
        return {**workflows.run_dict(run), "artifacts": [result], "approval": approval.public_dict()}
    run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    return {**workflows.run_dict(run), "artifacts": [result]}


async def _run_vendor_ap_summary_workflow(run, body: dict) -> dict:
    vendor_code = str(body.get("vendor_code") or body.get("vendorCode") or "").strip()
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_vendor_payload(vendor_code or "VEND100")
    else:
        if not vendor_code:
            return {"error": "vendor_code is required unless sample=true", "status_code": 400}
        session_id = "workflow:" + run.run_id
        payload = {"vendor_code": vendor_code}
        for step_id, title, canonical, args in [
            ("fetch_vendor", "Fetch vendor profile", "get_vendor_details", {"vendor_code": vendor_code}),
            ("fetch_ap", "Fetch AP invoices", "get_vendor_ap_invoices", {"vendor_code": vendor_code, "page": 1, "page_size": int(body.get("page_size") or 25)}),
        ]:
            run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool=canonical, title=title))
            result = _parse_tool_json(await _govern(canonical, session_id, "", args))
            run = workflows.complete_step(run, step_id, {"status": result.get("status"), "intent": result.get("intent")})
            payload["vendor" if step_id == "fetch_vendor" else "ap_invoices"] = result
    tables = workflows.vendor_ap_tables(payload)
    sections = workflows.vendor_ap_sections(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.SENSITIVE])
    artifacts = []
    for step_id, title, canonical, args in [
        ("build_excel", "Build AP workbook", "create_excel_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Vendor AP Summary - {payload.get('vendor_code', 'vendor')}",
            "tables": tables,
            "classification": classification,
            "filename": f"vendor-ap-summary-{payload.get('vendor_code', 'vendor')}.xlsx",
        }),
        ("build_doc", "Build AP narrative report", "create_word_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Vendor AP Summary - {payload.get('vendor_code', 'vendor')}",
            "sections": sections,
            "tables": tables,
            "classification": classification,
            "filename": f"vendor-ap-summary-{payload.get('vendor_code', 'vendor')}.docx",
        }),
    ]:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="artifact", status="running", tool=canonical, title=title))
        result = _parse_tool_json(await _govern(canonical, "workflow:" + run.run_id, "", args))
        if result.get("status") != "success":
            run = workflows.fail_step(run, step_id, result.get("message") or result.get("errorCode") or "artifact generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, step_id, {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
        artifacts.append(result)
    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    needs_approval, row_count, threshold = _workflow_requires_broad_export_approval(tables, classification, body)
    if needs_approval:
        run, approval = _workflow_pause_for_broad_export(run, artifact_ids=artifact_ids, row_count=row_count, threshold=threshold)
        return {**workflows.run_dict(run), "artifacts": artifacts, "approval": approval.public_dict()}
    run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    return {**workflows.run_dict(run), "artifacts": artifacts}




async def _run_customer_email_draft_workflow(run, body: dict) -> dict:
    customer_id = str(body.get("customer_id") or body.get("customerId") or "").strip()
    topic = str(body.get("topic") or body.get("email_topic") or body.get("emailTopic") or "account follow-up").strip()
    tone = str(body.get("tone") or "professional").strip() or "professional"
    recipient_raw = body.get("to") or body.get("recipients") or body.get("recipient") or body.get("recipient_email") or body.get("recipientEmail") or []
    if isinstance(recipient_raw, str):
        recipients = [x.strip() for x in recipient_raw.split(",") if x.strip()]
    else:
        recipients = [str(x).strip() for x in (recipient_raw or []) if str(x).strip()]
    if not recipients:
        recipients = ["manager@example.com"]

    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_customer_payload(customer_id or "SAMPLE100")
    else:
        if not customer_id:
            return {"error": "customer_id is required unless sample=true", "status_code": 400}
        session_id = "workflow:" + run.run_id
        payload = {"customer_id": customer_id}
        for step_id, title, canonical, args in [
            ("fetch_overview", "Fetch customer overview", "get_customer_overview", {}),
            ("fetch_order_summary", "Fetch order summary", "get_customer_order_summary", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
            }),
            ("fetch_orders", "Fetch recent orders", "get_customer_orders", {
                "start_date": str(body.get("start_date") or ""), "end_date": str(body.get("end_date") or ""),
                "page_size": int(body.get("page_size") or 10),
            }),
            ("fetch_shipments", "Fetch shipment status", "get_customer_shipment_status", {
                "max_orders": int(body.get("max_orders") or 5),
            }),
        ]:
            run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="tool_call", status="running", tool=canonical, title=title))
            result = _parse_tool_json(await _govern(canonical, session_id, customer_id, args))
            run = workflows.complete_step(run, step_id, {"status": result.get("status"), "intent": result.get("intent")})
            payload[{"fetch_overview": "overview", "fetch_order_summary": "order_summary", "fetch_orders": "orders", "fetch_shipments": "shipments"}[step_id]] = result

    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.PII, manifest.SENSITIVE])
    artifacts = []
    attachment_ids: list[str] = []
    include_packet = body.get("include_packet", body.get("includePacket", True)) not in (False, "false", "0", 0)
    if include_packet:
        packet_args = {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Customer Email Packet - {payload.get('customer_id', 'customer')}",
            "sections": workflows.customer_360_sections(payload),
            "tables": workflows.customer_360_tables(payload),
            "classification": classification,
            "filename": f"customer-email-packet-{payload.get('customer_id', 'customer')}.pdf",
        }
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="build_packet", type="artifact", status="running", tool="create_pdf_packet", title="Build PDF review packet"))
        packet = _parse_tool_json(await _govern("create_pdf_packet", "workflow:" + run.run_id, "", packet_args))
        if packet.get("status") != "success":
            run = workflows.fail_step(run, "build_packet", packet.get("message") or packet.get("errorCode") or "packet generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, "build_packet", {"artifactId": packet.get("artifactId"), "filename": packet.get("filename")})
        artifacts.append(packet)
        if packet.get("artifactId"):
            attachment_ids.append(packet["artifactId"])

    subject = str(body.get("subject") or workflows.customer_email_subject(payload, topic))
    body_markdown = str(body.get("body_markdown") or body.get("bodyMarkdown") or workflows.customer_email_body(payload, topic, tone))
    draft_args = {
        "owner": ctx.consumer_ctx.get() or run.requested_by,
        "to": recipients,
        "cc": list(body.get("cc") or []),
        "subject": subject,
        "body_markdown": body_markdown,
        "attachment_artifact_ids": attachment_ids,
        "classification": classification,
    }
    run = workflows.add_step(run, workflows.WorkflowStep(step_id="create_draft", type="artifact", status="running", tool="create_email_draft", title="Create governed email draft"))
    draft = _parse_tool_json(await _govern("create_email_draft", "workflow:" + run.run_id, "", draft_args))
    if draft.get("status") != "success":
        run = workflows.fail_step(run, "create_draft", draft.get("message") or draft.get("errorCode") or "email draft generation failed")
        return workflows.run_dict(run)
    run = workflows.complete_step(run, "create_draft", {"artifactId": draft.get("artifactId"), "draftId": draft.get("draftId"), "filename": draft.get("filename")})
    artifacts.append(draft)

    approval = None
    if body.get("request_send_approval") or body.get("requestSendApproval") or body.get("send"):
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="request_send_approval", type="approval", status="running", tool="approval_store.create_approval", title="Request send approval"))
        approval = approval_store.create_approval(
            requested_by=run.requested_by,
            reason=str(body.get("approval_reason") or body.get("approvalReason") or f"Approve external send for {subject}"),
            risk_level="high" if any(x in classification for x in (manifest.PII, manifest.SENSITIVE)) else "medium",
            artifact_ids=[draft.get("artifactId") or draft.get("draftId")],
            workflow_run_id=run.run_id,
        )
        audit.log_policy_change(actor=run.requested_by, action="request_workflow_send_approval", target=approval.approval_id, detail=run.run_id)
        run = workflows.update_run(run, steps=[replace(s, status="pending", outputs={"approvalId": approval.approval_id, "status": approval.status}) if s.step_id == "request_send_approval" else s for s in run.steps])

    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    if approval:
        run = workflows.mark_approval_required(run.run_id, approval_id=approval.approval_id, artifact_ids=artifact_ids) or run
    else:
        run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    out = {**workflows.run_dict(run), "artifacts": artifacts}
    if approval:
        out["approval"] = approval.public_dict()
    return out



async def _run_weekly_executive_brief_workflow(run, body: dict) -> dict:
    start_date = str(body.get("start_date") or body.get("startDate") or "").strip()
    end_date = str(body.get("end_date") or body.get("endDate") or "").strip()
    limit = int(body.get("limit") or 5)
    if body.get("sample") or body.get("use_sample_data") or body.get("offline"):
        payload = workflows.sample_executive_payload(start_date or "2026-07-15", end_date or "2026-07-22")
    else:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="fetch_top_customers", type="tool_call", status="running", tool="get_top_customers_by_spend", title="Fetch top customers by spend"))
        top = _parse_tool_json(await _govern("get_top_customers_by_spend", "workflow:" + run.run_id, "", {
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        }))
        run = workflows.complete_step(run, "fetch_top_customers", {"status": top.get("status"), "intent": top.get("intent")})
        records = list(top.get("records") or top.get("customers") or [])
        total = sum(float(r.get("totalSpend") or r.get("spend") or r.get("grandTotal") or 0) for r in records)
        payload = {
            "start_date": start_date,
            "end_date": end_date,
            "top_customers": {"status": top.get("status"), "records": records, "totalSpend": round(total, 2)},
            "exceptions": [
                {"Area": "Analytics", "Signal": "Review concentration in top customer spend", "Owner": "Management", "Priority": "Medium"},
            ],
            "kpis": {"Revenue in brief": round(total, 2), "Top customer count": len(records), "Open executive risks": 1, "High priority risks": 0},
        }

    tables = workflows.executive_brief_tables(payload)
    sections = workflows.executive_brief_sections(payload)
    classification = list(body.get("classification") or [manifest.INTERNAL, manifest.SENSITIVE, manifest.PII])
    artifacts = []
    for step_id, title, canonical, args in [
        ("build_excel", "Build executive KPI workbook", "create_excel_report", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Weekly Executive Brief - {payload.get('end_date', 'week')}",
            "tables": tables,
            "classification": classification,
            "filename": f"weekly-executive-brief-{payload.get('end_date', 'week')}.xlsx",
        }),
        ("build_deck", "Build executive briefing deck", "create_powerpoint_deck", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Weekly Executive Brief - {payload.get('end_date', 'week')}",
            "sections": sections,
            "classification": classification,
            "filename": f"weekly-executive-brief-{payload.get('end_date', 'week')}.pptx",
        }),
        ("build_pdf", "Build executive PDF packet", "create_pdf_packet", {
            "owner": ctx.consumer_ctx.get() or run.requested_by,
            "title": f"Weekly Executive Brief - {payload.get('end_date', 'week')}",
            "sections": sections,
            "tables": tables,
            "classification": classification,
            "filename": f"weekly-executive-brief-{payload.get('end_date', 'week')}.pdf",
        }),
    ]:
        run = workflows.add_step(run, workflows.WorkflowStep(step_id=step_id, type="artifact", status="running", tool=canonical, title=title))
        result = _parse_tool_json(await _govern(canonical, "workflow:" + run.run_id, "", args))
        if result.get("status") != "success":
            run = workflows.fail_step(run, step_id, result.get("message") or result.get("errorCode") or "artifact generation failed")
            return workflows.run_dict(run)
        run = workflows.complete_step(run, step_id, {"artifactId": result.get("artifactId"), "filename": result.get("filename")})
        artifacts.append(result)

    approval = None
    if body.get("request_approval") or body.get("requestApproval"):
        run = workflows.add_step(run, workflows.WorkflowStep(step_id="request_review_approval", type="approval", status="running", tool="approval_store.create_approval", title="Request executive review approval"))
        approval = approval_store.create_approval(
            requested_by=run.requested_by,
            reason=str(body.get("approval_reason") or body.get("approvalReason") or "Review weekly executive brief before distribution"),
            risk_level="high",
            artifact_ids=[a.get("artifactId") for a in artifacts if a.get("artifactId")],
            workflow_run_id=run.run_id,
        )
        audit.log_policy_change(actor=run.requested_by, action="request_executive_brief_approval", target=approval.approval_id, detail=run.run_id)
        run = workflows.update_run(run, steps=[replace(s, status="pending", outputs={"approvalId": approval.approval_id, "status": approval.status}) if s.step_id == "request_review_approval" else s for s in run.steps])

    artifact_ids = [a.get("artifactId") for a in artifacts if a.get("artifactId")]
    if approval:
        run = workflows.mark_approval_required(run.run_id, approval_id=approval.approval_id, artifact_ids=artifact_ids) or run
    else:
        run = workflows.update_run(run, status="completed", artifact_ids=artifact_ids)
    out = {**workflows.run_dict(run), "artifacts": artifacts}
    if approval:
        out["approval"] = approval.public_dict()
    return out

async def _workflow_run_start(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active"}, status_code=403)
    template_id = request.path_params["tid"]
    template = workflows.get_template(template_id)
    if template is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    ctx.ip_ctx.set(ctx.client_ip(request))
    result = await _execute_workflow_for_record(record, template_id, body)
    if result.get("error") and result.get("status_code"):
        return JSONResponse({"error": result["error"], "runId": result.get("runId")}, status_code=result["status_code"])
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result, status_code=201)



def _artifact_expired(record) -> bool:
    return bool(record and record.expires_at is not None and record.expires_at <= time.time())


def _artifact_allowed(record, claims, permission: str = "view") -> bool:
    if not record or not claims:
        return False
    if record.owner == claims.get("name") or claims.get("role") == "admin":
        return True
    return artifact_share_store.active_share(record.artifact_id, claims.get("name", ""), permission) is not None


def _artifact_owner_or_admin(record, claims) -> bool:
    return bool(record and claims and (record.owner == claims.get("name") or claims.get("role") == "admin"))


def _artifact_public_for(record, claims) -> dict:
    data = record.public_dict()
    data["shared"] = bool(claims and record.owner != claims.get("name"))
    data["expired"] = _artifact_expired(record)
    return data


def _artifact_preview(record) -> dict:
    path = Path(record.storage_path)
    preview = {
        "kind": record.type,
        "filename": record.filename,
        "summary": [],
        "packageParts": [],
        "signals": {},
    }
    if not path.exists():
        return {**preview, "error": "artifact file is missing"}
    if record.type in ("xlsx", "pptx", "docx"):
        try:
            with zipfile.ZipFile(path) as z:
                names = sorted(z.namelist())
                preview["packageParts"] = names[:80]
                preview["signals"] = {
                    "hasCoreProperties": "docProps/core.xml" in names,
                    "hasAppProperties": "docProps/app.xml" in names,
                    "hasStyles": any(n.endswith("/styles.xml") or n == "xl/styles.xml" for n in names),
                    "hasTheme": any("/theme/" in n for n in names),
                }
                if record.type == "xlsx":
                    sheets = [n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
                    preview["signals"].update({"sheetCount": len(sheets), "hasAutoFilter": False, "hasFrozenPane": False})
                    for sheet_name in sheets[:3]:
                        sheet = z.read(sheet_name).decode("utf-8", "ignore")
                        preview["signals"]["hasAutoFilter"] = preview["signals"]["hasAutoFilter"] or "<autoFilter" in sheet
                        preview["signals"]["hasFrozenPane"] = preview["signals"]["hasFrozenPane"] or "state=\"frozen\"" in sheet
                    preview["summary"].append(f"Workbook with {len(sheets)} worksheet(s).")
                elif record.type == "pptx":
                    slides = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
                    preview["signals"]["slideCount"] = len(slides)
                    preview["summary"].append(f"Deck with {len(slides)} slide(s).")
                elif record.type == "docx":
                    doc = z.read("word/document.xml").decode("utf-8", "ignore") if "word/document.xml" in names else ""
                    preview["signals"].update({"hasTables": "<w:tbl" in doc, "paragraphCount": doc.count("<w:p")})
                    preview["summary"].append(f"Document with approximately {doc.count('<w:p')} paragraph(s).")
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            preview["error"] = str(exc)
    elif record.type == "email_draft" or record.filename.endswith(".json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            preview["signals"] = {
                "subject": raw.get("subject", ""),
                "toCount": len(raw.get("to") or []),
                "attachmentCount": len(raw.get("attachment_artifact_ids") or []),
            }
            preview["summary"].append(f"Email draft to {len(raw.get('to') or [])} recipient(s).")
        except (OSError, ValueError) as exc:
            preview["error"] = str(exc)
    elif record.type == "calendar_invite":
        try:
            sidecar = Path(record.storage_path).with_suffix(Path(record.storage_path).suffix + ".json")
            raw = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
            preview["signals"] = {
                "title": raw.get("title", record.title),
                "start": raw.get("start", ""),
                "end": raw.get("end", ""),
                "timezone": raw.get("timezone", "UTC"),
                "attendeeCount": len(raw.get("attendees") or []),
                "attendees": ", ".join(raw.get("attendees") or []),
                "location": raw.get("location", ""),
            }
            preview["summary"].append(f"Calendar invite for {len(raw.get('attendees') or [])} attendee(s).")
        except (OSError, ValueError) as exc:
            preview["error"] = str(exc)
    else:
        preview["summary"].append("Binary artifact available for download.")
    return preview


def _onlyoffice_config(record, claims) -> dict | None:
    base = (os.getenv("ONLYOFFICE_DOCUMENT_SERVER_URL") or os.getenv("ONLYOFFICE_DOCSERVER_URL") or "").rstrip("/")
    if not base or record.type not in ("docx", "xlsx", "pptx"):
        return None
    ext = record.type
    mode = "view" if str(os.getenv("ONLYOFFICE_EDIT_MODE") or "view").lower() != "edit" else "edit"
    return {
        "enabled": True,
        "documentServerUrl": base,
        "documentType": {"docx": "word", "xlsx": "cell", "pptx": "slide"}.get(ext, "word"),
        "config": {
            "document": {
                "fileType": ext,
                "key": f"{record.artifact_id}-{record.checksum[-16:]}",
                "title": record.filename,
                "url": f"/artifacts/{record.artifact_id}/download",
                "permissions": {"download": True, "edit": mode == "edit", "print": True},
            },
            "editorConfig": {
                "mode": mode,
                "user": {"id": claims.get("sub", claims.get("name", "user")), "name": claims.get("name", "user")},
            },
        },
    }

def _decode_artifact_payload(body: dict, default_filename: str = "uploaded.txt") -> tuple[str, bytes, str | None]:
    filename = Path(str(body.get("filename") or default_filename)).name or default_filename
    text = body.get("text")
    content_b64 = body.get("content_base64")
    if content_b64:
        try:
            payload = base64.b64decode(str(content_b64), validate=True)
        except Exception:
            raise ValueError("content_base64 is invalid")
    elif text is not None:
        payload = str(text).encode("utf-8")
    else:
        raise ValueError("text or content_base64 is required")
    if len(payload) > 25 * 1024 * 1024:
        raise OverflowError("artifact upload limit is 25 MB")
    return filename, payload, body.get("mime_type")


# Artifact API (session-gated). Tool generation happens through MCP; these routes
# expose the durable outputs to the owning user and to admins.

async def _artifacts(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    try:
        limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100
    if request.method == "POST":
        if claims.get("role") != "admin":
            store = get_store()
            record = store.get_consumer(claims["sub"])
            if record is None or "files" not in _effective_category_ids(store, record):
                return JSONResponse({"error": "your access does not include raw file uploads (files category)"}, status_code=403)
        try:
            body = await request.json() if request.headers.get("content-length") else {}
        except Exception:
            body = {}
        try:
            filename, payload, mime_override = _decode_artifact_payload(body)
        except OverflowError as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        artifact_type = str(body.get("artifact_type") or Path(filename).suffix.lstrip(".") or "txt").lower()
        mime_type = str(mime_override or ("text/plain; charset=utf-8" if artifact_type in ("txt", "md", "csv") else "application/octet-stream"))
        retention_days = body.get("retention_days")
        try:
            retention_days = None if retention_days in (None, "") else int(retention_days)
        except (TypeError, ValueError):
            return JSONResponse({"error": "retention_days must be an integer"}, status_code=400)
        record = artifact_store.create_artifact(
            owner=claims["name"],
            title=str(body.get("title") or filename).strip() or filename,
            filename=filename,
            payload=payload,
            artifact_type=artifact_type,
            mime_type=mime_type,
            classification=list(body.get("classification") or [manifest.INTERNAL]),
            source_tool_calls=["artifact_upload"],
            retention_days=retention_days,
        )
        audit.log_policy_change(actor=claims["name"], action="upload_artifact", target=record.artifact_id, detail=record.filename)
        return JSONResponse(_artifact_public_for(record, claims), status_code=201)

    if claims.get("role") == "admin" and request.query_params.get("all") == "1":
        records = artifact_store.list_artifacts(owner=None, limit=limit)
    else:
        seen: dict[str, object] = {r.artifact_id: r for r in artifact_store.list_artifacts(owner=claims["name"], limit=limit)}
        for share in artifact_share_store.list_shares(shared_with=claims["name"]):
            if not artifact_share_store.is_active(share):
                continue
            record = artifact_store.get_artifact(share.artifact_id)
            if record is not None:
                seen[record.artifact_id] = record
        records = sorted(seen.values(), key=lambda r: r.created_at, reverse=True)[:limit]
    return JSONResponse({"artifacts": [_artifact_public_for(r, claims) for r in records]})


async def _artifact_metadata(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "DELETE":
        if not _artifact_owner_or_admin(record, claims):
            return _unauthorized(is_admin=True)
        ok = artifact_store.delete_artifact(record.artifact_id)
        audit.log_policy_change(actor=claims["name"], action="delete_artifact", target=record.artifact_id, detail=record.filename)
        return JSONResponse({"ok": ok, "artifactId": record.artifact_id})
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    return JSONResponse(_artifact_public_for(record, claims))


async def _artifact_workbench(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    artifact = _artifact_public_for(record, claims)
    preview = _artifact_preview(record)
    approvals = [a.public_dict() for a in approval_store.list_approvals(requested_by=record.owner) if record.artifact_id in a.artifact_ids]
    shares = [x.public_dict() for x in artifact_share_store.list_shares(artifact_id=record.artifact_id, include_revoked=True)] if _artifact_owner_or_admin(record, claims) else []
    versions = [v.public_dict() for v in artifact_store.list_versions(record.artifact_id)]
    reviews = [r.public_dict() for r in document_review_store.list_reviews(artifact_id=record.artifact_id)]
    onlyoffice = _onlyoffice_config(record, claims)
    audit.log_policy_change(
        actor=claims["name"], action="inspect_artifact", target=record.artifact_id,
        detail=f"{record.filename}; classification={','.join(record.classification)}",
    )
    return JSONResponse({"artifact": artifact, "preview": preview, "approvals": approvals, "shares": shares, "versions": versions, "reviews": reviews, "onlyoffice": onlyoffice})

async def _artifact_reviews(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    if request.method == "GET":
        reviews = [r.public_dict() for r in document_review_store.list_reviews(artifact_id=record.artifact_id)]
        return JSONResponse({"artifact": _artifact_public_for(record, claims), "reviews": reviews})
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    provider = str(body.get("provider") or "local").strip() or "local"
    editor_url = str(body.get("editor_url") or body.get("editorUrl") or "").strip()
    if not editor_url and provider == "onlyoffice":
        oo = _onlyoffice_config(record, claims)
        editor_url = (oo or {}).get("documentServerUrl", "")
    review = document_review_store.create_review(
        artifact_id=record.artifact_id,
        artifact_version_id=str(body.get("artifact_version_id") or body.get("artifactVersionId") or record.latest_version_id or "latest"),
        requested_by=claims["name"],
        owner=record.owner,
        reason=str(body.get("reason") or f"Review {record.filename}"),
        provider=provider,
        editor_url=editor_url,
    )
    audit.log_policy_change(actor=claims["name"], action="create_document_review", target=review.review_id, detail=record.artifact_id)
    return JSONResponse(review.public_dict(), status_code=201)


async def _artifact_review_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    review = document_review_store.get_review(request.path_params["rid"])
    if review is None or review.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(review.public_dict())


async def _artifact_review_comments(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    review = document_review_store.get_review(request.path_params["rid"])
    if review is None or review.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
        updated = document_review_store.add_comment(
            review.review_id,
            author=claims["name"],
            body=str(body.get("body") or ""),
            anchor=str(body.get("anchor") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="comment_document_review", target=review.review_id, detail=record.artifact_id)
    return JSONResponse(updated.public_dict())


async def _artifact_review_decision(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    review = document_review_store.get_review(request.path_params["rid"])
    if review is None or review.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    action = request.path_params["action"]
    status = "approved" if action == "approve" else "changes_requested" if action in ("changes", "changes-requested", "request-changes") else "closed" if action == "close" else ""
    if not status:
        return JSONResponse({"error": "unknown action"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
        updated = document_review_store.decide_review(review.review_id, status=status, decided_by=claims["name"], decision=str(body.get("decision") or body.get("note") or ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action=f"{status}_document_review", target=review.review_id, detail=record.artifact_id)
    return JSONResponse(updated.public_dict())


async def _artifact_versions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if request.method == "GET":
        if not _artifact_allowed(record, claims, "view"):
            return _unauthorized(is_admin=True)
        versions = [v.public_dict() for v in artifact_store.list_versions(record.artifact_id)]
        return JSONResponse({"artifact": _artifact_public_for(record, claims), "versions": versions})
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    try:
        filename, payload, mime_override = _decode_artifact_payload(body, default_filename=record.filename)
    except OverflowError as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    version = artifact_store.create_version(
        artifact_id=record.artifact_id,
        payload=payload,
        filename=filename,
        mime_type=str(mime_override or record.mime_type),
        created_by=claims["name"],
        note=str(body.get("note") or ""),
    )
    if version is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    audit.log_policy_change(actor=claims["name"], action="create_artifact_version", target=record.artifact_id, detail=version.version_id)
    latest = artifact_store.get_artifact(record.artifact_id) or record
    return JSONResponse({"artifact": _artifact_public_for(latest, claims), "version": version.public_dict()}, status_code=201)


async def _artifact_version_download(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "download"):
        return _unauthorized(is_admin=True)
    version = artifact_store.get_version(record.artifact_id, request.path_params["vid"])
    if version is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not Path(version.storage_path).exists():
        return JSONResponse({"error": "artifact version file is missing"}, status_code=410)
    audit.log_policy_change(
        actor=claims["name"], action="download_artifact_version", target=record.artifact_id,
        detail=f"{version.version_id}; {version.filename}; classification={','.join(record.classification)}",
    )
    return FileResponse(version.storage_path, media_type=version.mime_type, filename=version.filename)


async def _artifact_shares(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    if request.method == "GET":
        shares = [s.public_dict() for s in artifact_share_store.list_shares(artifact_id=record.artifact_id, include_revoked=True)]
        return JSONResponse({"shares": shares})
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    expires_at = body.get("expires_at")
    if expires_at in (None, "") and body.get("expires_in_days") not in (None, ""):
        try:
            expires_at = time.time() + max(0, int(body.get("expires_in_days"))) * 86400
        except (TypeError, ValueError):
            return JSONResponse({"error": "expires_in_days must be an integer"}, status_code=400)
    elif expires_at not in (None, ""):
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError):
            return JSONResponse({"error": "expires_at must be a unix timestamp"}, status_code=400)
    else:
        expires_at = None
    try:
        share = artifact_share_store.create_share(
            artifact_id=record.artifact_id,
            owner=record.owner,
            shared_with=str(body.get("shared_with") or "").strip(),
            permissions=list(body.get("permissions") or ["view"]),
            created_by=claims["name"],
            expires_at=expires_at,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="share_artifact", target=record.artifact_id, detail=share.shared_with)
    return JSONResponse(share.public_dict(), status_code=201)


async def _artifact_share_revoke(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    share = artifact_share_store.get_share(request.path_params["sid"])
    if share is None or share.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    updated = artifact_share_store.revoke_share(share.share_id, revoked_by=claims["name"])
    audit.log_policy_change(actor=claims["name"], action="revoke_artifact_share", target=record.artifact_id, detail=share.shared_with)
    return JSONResponse(updated.public_dict())


async def _admin_artifact_purge_expired(request):
    claims, err = _require_admin(request)
    if err:
        return err
    purged = artifact_store.purge_expired()
    audit.log_policy_change(actor=claims["name"], action="purge_expired_artifacts", target="artifact_store", detail=str(len(purged)))
    return JSONResponse({"purged": [r.public_dict() for r in purged], "count": len(purged)})


async def _artifact_request_approval(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    reason = str(body.get("reason") or f"Review artifact {record.filename}").strip()
    approval = approval_store.create_approval(
        requested_by=claims["name"], reason=reason, risk_level=str(body.get("risk_level") or "medium"),
        artifact_ids=[record.artifact_id], workflow_run_id=record.source_workflow_run_id,
    )
    audit.log_policy_change(actor=claims["name"], action="request_artifact_approval", target=approval.approval_id, detail=record.artifact_id)
    return JSONResponse(approval.public_dict(), status_code=201)


async def _artifact_download(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "download"):
        return _unauthorized(is_admin=True)
    if not Path(record.storage_path).exists():
        return JSONResponse({"error": "artifact file is missing"}, status_code=410)
    audit.log_policy_change(
        actor=claims["name"], action="download_artifact", target=record.artifact_id,
        detail=f"{record.filename}; classification={','.join(record.classification)}",
    )
    return FileResponse(record.storage_path, media_type=record.mime_type, filename=record.filename)

# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Governed chat (Stage 1: thin, session-keyed assistant) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

async def _chat_page(request):
    if not _session(request):
        return HTMLResponse(_LOGIN_HTML, status_code=401)
    return HTMLResponse(_CHAT_HTML)


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
    session_id = chat_log.resolve_session_id(store, requested_id, record.consumer_id, _CHAT_IDLE_SEC, llm_complete)
    history = chat_log.history_for_llm(store, session_id, record.consumer_id)

    result = await orchestrator.run_chat(mcp, message, session_id, record,
                                         llm_complete=llm_complete, history=history)
    chat_log.record_turn(store, session_id, record.consumer_id, "user", message)
    chat_log.record_turn(store, session_id, record.consumer_id, "assistant", result.get("reply", ""),
                         tools_used=[t.get("tool") for t in result.get("tool_calls", [])])
    result["conversation_id"] = session_id
    return JSONResponse(result)


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


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
    session_id = chat_log.resolve_session_id(store, requested_id, record.consumer_id, _CHAT_IDLE_SEC, llm_complete)
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
                                                          llm_complete=llm_complete, history=history):
                t = ev.get("type")
                if t == "delta":
                    parts.append(ev.get("text", ""))
                elif t == "replace":
                    parts = [ev.get("text", "")]
                elif t == "done":
                    tools = ev.get("tool_calls", [])
                    completed = True
                yield _sse(ev)
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


async def _root(_request):
    # The gateway serves no page at "/"; send browsers to the dashboard.
    return RedirectResponse(url="/dashboard")


async def _health(_request):
    return JSONResponse({"status": "ok", "service": "governance-gateway"})


async def _dashboard(request):
    """Single themed app shell (left-nav SPA). Serves every /dashboard[/section]
    path; the client renders the panel from the URL and hides admin sections for
    non-admins (admin APIs enforce the role server-side regardless)."""
    if not _session(request):
        return HTMLResponse(_LOGIN_HTML, status_code=401)
    # no-store: the shell is an inline HTML string (no hashed asset URL to bust),
    # so without this a browser can keep showing a stale dashboard after a deploy.
    return HTMLResponse(_APP_HTML, headers={"Cache-Control": "no-store"})


async def _signup_page(_request):
    return HTMLResponse(_SIGNUP_HTML)


async def _logo(_request):
    if not _LOGO_BYTES:
        return Response(status_code=404)
    return Response(_LOGO_BYTES, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


async def _admin_calls(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    # Only actual tool-call attempts (Layer 2: reached, or were denied by, a
    # specific tool's policy) -- auth failures, rate limits, and policy changes
    # (signup, key rotation, category edits) are real events but not "a tool
    # call"; policy changes already have their own view (/admin/policy-changes).
    # Filter BEFORE truncating to `limit` so noisy non-call events (e.g. a burst
    # of failed logins) can't crowd real tool calls out of the window.
    calls = [c for c in audit.recent(1000) if c.get("type") in ("call", "denied")]
    # Role-scoped monitoring: a non-admin sees only its own principal's calls.
    if claims.get("role") != "admin":
        calls = [c for c in calls if c.get("consumer") == claims["name"]]
    # Classify against the FULL available window (not yet truncated to `limit`)
    # so a burst/enumeration pattern near the edge of the window isn't
    # under/over-counted by an unrelated display cap.
    calls = safety.classify(calls)
    return JSONResponse({"calls": calls[:limit]})


async def _admin_audit_export(request):
    claims, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    try:
        hours = float(body.get("hours") or request.query_params.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 24 * 30))
    try:
        limit = int(body.get("limit") or request.query_params.get("limit") or 5000)
    except (TypeError, ValueError):
        limit = 5000
    limit = max(1, min(limit, 20000))
    event_type = str(body.get("type") or request.query_params.get("type") or "all").strip().lower()
    consumer = str(body.get("consumer") or request.query_params.get("consumer") or "").strip()
    since = time.time() - hours * 3600
    records = audit.read_since(since, limit=limit)
    if event_type != "all":
        records = [r for r in records if r.get("type") == event_type]
    if consumer:
        records = [r for r in records if r.get("consumer") == consumer]
    records = safety.classify(records)
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    payload = {
        "kind": "audit_export",
        "schemaVersion": 1,
        "exportedAt": time.time(),
        "exportedBy": claims["name"],
        "filters": {"hours": hours, "type": event_type, "consumer": consumer, "limit": limit},
        "summary": {"total": len(records), "byType": counts, "suspicious": sum(1 for r in records if r.get("suspicious"))},
        "records": records,
    }
    safe_type = event_type if event_type != "all" else "all-events"
    filename = f"audit-export-{safe_type}-{int(payload['exportedAt'])}.json"
    record = artifact_store.create_artifact(
        owner=claims["name"],
        title="Audit Export",
        filename=filename,
        payload=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        artifact_type="json",
        mime_type="application/json",
        classification=[manifest.INTERNAL],
        source_tool_calls=["audit_export"],
        retention_days=365,
    )
    audit.log_policy_change(actor=claims["name"], action="export_audit", target=record.artifact_id, detail=f"records={len(records)}; hours={hours}; type={event_type}; consumer={consumer or '*'}")
    return JSONResponse({"artifact": record.public_dict(), "summary": payload["summary"], "filters": payload["filters"]}, status_code=201)

async def _admin_sessions(request):
    claims = _session(request)
    if not claims or claims.get("role") != "admin":
        return _unauthorized(is_admin=bool(claims))
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    return JSONResponse({"sessions": scope_store.snapshot(limit)})


async def _admin_rate_limits(request):
    claims = _session(request)
    if not claims or claims.get("role") != "admin":
        return _unauthorized(is_admin=bool(claims))
    return JSONResponse({"rate_limits": edge.rate_limit_snapshot()})


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Admin CRUD API (admin-only, audited to policy_audit) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

def _require_admin(request):
    """Return (claims, None) for an admin session, else (None, error_response)."""
    claims = _session(request)
    if not claims:
        return None, _unauthorized()
    if claims.get("role") != "admin":
        return None, _unauthorized(is_admin=True)
    return claims, None


def _require_admin_or_category(request, category_id: str):
    """Like _require_admin, but also let a non-admin holding `category_id` through
    (expansion.md §7.1's workflow_admin/agent_admin -- ADDITIVE: the admin role
    always still works, this only widens who ELSE can reach the route)."""
    claims = _session(request)
    if not claims:
        return None, _unauthorized()
    if claims.get("role") == "admin":
        return claims, None
    store = get_store()
    record = store.get_consumer(claims["sub"])
    if record is not None and category_id in _effective_category_ids(store, record):
        return claims, None
    return None, _unauthorized(is_admin=True)


def _consumer_public(r) -> dict:
    """Consumer view without secrets (never expose key_hash / password hash).
    `categories` is this consumer's OWN stored field (may be empty for a
    department-linked user by design -- see departments.py); `effective_categories`
    is what they ACTUALLY resolve to right now (own categories + their department's
    current ones, if any), so the admin UI doesn't read a department member's row
    as "somehow has zero access"."""
    store = get_store()
    grant = resolve_grant(r, store.get_category, store.get_department)
    return {
        "consumer_id": r.consumer_id, "name": r.name, "full_name": r.full_name, "department": r.department,
        "status": r.status,
        "type": r.type, "role": r.role, "categories": r.categories,
        # what this consumer ACTUALLY resolves to right now (own categories + their
        # department's current ones, if any) -- shown so a department member's row
        # doesn't read as "somehow has zero access" just because their own
        # `categories` field is empty by design (see departments.py).
        "effective_backends": ["*"] if grant.all_tools else sorted(grant.tools_by_backend.keys()),
        "rate_limit_per_hour": r.rate_limit_per_hour, "ip_allowlist": list(r.ip_allowlist),
        "overrides": r.overrides, "allowed_levels": sorted(r.allowed_levels),
        "has_key": bool(r.key_hash), "has_login": bool(r.login_password_hash),
    }


def _writable_or_error():
    store = get_store()
    if not store.writable:
        return None, JSONResponse(
            {"error": "policy store is read-only; set GOVERNANCE_STORE_FILE (or Cosmos) to enable editing"},
            status_code=409)
    return store, None


async def _admin_catalog(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    return JSONResponse({
        "backends": {b: sorted(manifest.tools_for_backend(b)) for b in sorted(manifest.backends())},
        "levels": [manifest.PUBLIC, manifest.INTERNAL, manifest.PII, manifest.SENSITIVE],
        "categories": [c.id for c in store.categories()],
        "departments": [d.id for d in store.departments()],
        "roles": ["user", "admin"], "types": ["agent", "user"],
    })


async def _admin_consumers(request):
    claims, err = _require_admin(request)
    if err:
        return err
    if request.method == "GET":
        return JSONResponse({"consumers": [_consumer_public(r) for r in get_store().consumers()]})
    # POST -> create a consumer, mint an API key (shown once)
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    consumer_id = str(body.get("consumer_id") or name).strip()
    if store.get_consumer(consumer_id):
        return JSONResponse({"error": f"consumer {consumer_id} already exists"}, status_code=409)
    api_key = generate_api_key()
    password = body.get("password")
    _now = time.time()
    record = ConsumerRecord(
        consumer_id=consumer_id, name=name, key_hash=hash_api_key(api_key),
        status=body.get("status", "active"), role=body.get("role", "user"),
        type=body.get("type", "agent"), categories=list(body.get("categories") or []),
        rate_limit_per_hour=body.get("rate_limit_per_hour"),
        ip_allowlist=list(body.get("ip_allowlist") or []),
        overrides=body.get("overrides") or {},
        login_password_hash=hash_password(password) if password else None,
        key_created_at=_now, key_rotated_at=_now,
    )
    store.upsert_consumer(record)
    audit.log_policy_change(actor=claims["name"], action="create_consumer", target=consumer_id)
    return JSONResponse({"consumer": _consumer_public(record), "api_key": api_key}, status_code=201)


async def _admin_consumer_item(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    cid = request.path_params["cid"]
    existing = store.get_consumer(cid)
    if not existing:
        return JSONResponse({"error": "not found"}, status_code=404)

    if request.method == "DELETE":
        store.delete_consumer(cid)
        audit.log_policy_change(actor=claims["name"], action="delete_consumer", target=cid)
        return JSONResponse({"ok": True})

    body = await request.json()
    from dataclasses import replace
    fields = {}
    for f in ("status", "role", "type", "categories", "rate_limit_per_hour", "overrides", "full_name", "department"):
        if f in body:
            fields[f] = body[f]
    if "ip_allowlist" in body:
        fields["ip_allowlist"] = list(body["ip_allowlist"] or [])
    if "allowed_levels" in body:
        fields["allowed_levels"] = frozenset(body["allowed_levels"] or [])
    if body.get("password"):
        fields["login_password_hash"] = hash_password(body["password"])
    store.upsert_consumer(replace(existing, **fields))
    audit.log_policy_change(actor=claims["name"], action="update_consumer", target=cid,
                            detail=",".join(sorted(fields)))
    return JSONResponse({"consumer": _consumer_public(store.get_consumer(cid))})


async def _admin_consumer_rotate(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    cid = request.path_params["cid"]
    existing = store.get_consumer(cid)
    if not existing:
        return JSONResponse({"error": "not found"}, status_code=404)
    from dataclasses import replace
    api_key = generate_api_key()
    _now = time.time()
    store.upsert_consumer(replace(existing, key_hash=hash_api_key(api_key),
                                  key_created_at=existing.key_created_at or _now, key_rotated_at=_now))
    audit.log_policy_change(actor=claims["name"], action="rotate_key", target=cid)
    return JSONResponse({"api_key": api_key})


async def _admin_categories(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        from store.file_store import _category_to_dict
        return JSONResponse({"categories": [_category_to_dict(c) for c in store.categories()]})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    if not body.get("id") or not body.get("backend"):
        return JSONResponse({"error": "id and backend are required"}, status_code=400)
    if body["backend"] not in manifest.backends():
        return JSONResponse(
            {"error": f"unknown backend {body['backend']!r}; one of: {sorted(manifest.backends())}"},
            status_code=400)
    cat = Category(
        id=body["id"], display_name=body.get("display_name", body["id"]), backend=body["backend"],
        tools=("*" if body.get("tools") == "*" else frozenset(body.get("tools") or [])),
        levels=frozenset(body.get("levels") or []), data_domains=list(body.get("data_domains") or []),
    )
    store.upsert_category(cat)
    audit.log_policy_change(actor=claims["name"], action="upsert_category", target=cat.id)
    return JSONResponse({"ok": True, "id": cat.id})


async def _admin_category_item(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    cid = request.path_params["cid"]
    store.delete_category(cid)
    audit.log_policy_change(actor=claims["name"], action="delete_category", target=cid)
    return JSONResponse({"ok": True})


async def _admin_departments(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        from store.file_store import _department_to_dict
        return JSONResponse({"departments": [_department_to_dict(d) for d in store.departments()]})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    if not body.get("id"):
        return JSONResponse({"error": "id is required"}, status_code=400)
    unknown = [c for c in (body.get("categories") or []) if store.get_category(c) is None]
    if unknown:
        return JSONResponse({"error": f"unknown categor{'y' if len(unknown)==1 else 'ies'}: {', '.join(unknown)}"},
                            status_code=400)
    dept = Department(id=body["id"], display_name=body.get("display_name", body["id"]),
                      categories=tuple(body.get("categories") or []))
    store.upsert_department(dept)
    audit.log_policy_change(actor=claims["name"], action="upsert_department", target=dept.id)
    return JSONResponse({"ok": True, "id": dept.id})


async def _admin_department_item(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    did = request.path_params["did"]
    store.delete_department(did)
    audit.log_policy_change(actor=claims["name"], action="delete_department", target=did)
    return JSONResponse({"ok": True})


async def _admin_whitelist(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        return JSONResponse({"whitelist": store.get_whitelist()})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    store.set_whitelist(list(body.get("cidrs") or []))
    audit.log_policy_change(actor=claims["name"], action="set_whitelist", target="global")
    return JSONResponse({"ok": True, "whitelist": store.get_whitelist()})


async def _admin_policy_changes(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"changes": audit.recent_policy_changes(200)})


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ Security-monitoring analytics (admin-only, read-only over the audit ring) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

def _range_sec(request, default: int = 86400) -> int:
    return analytics.RANGES.get(request.query_params.get("range", "24h"), default)


async def _admin_overview(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse(analytics.overview_cached(_range_sec(request)))


async def _admin_alerts(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"alerts": analytics.alerts_cached()})


async def _admin_alert_action(request):
    claims, err = _require_admin(request)
    if err:
        return err
    action = request.path_params["action"]
    if action not in ("ack", "resolve"):
        return JSONResponse({"error": "action must be ack or resolve"}, status_code=400)
    alert_id = request.path_params["aid"]
    status = "acknowledged" if action == "ack" else "resolved"
    # The policy-change event IS the durable state: analytics.alerts() reconstructs
    # each incident's status from these (see analytics._alert_statuses).
    audit.log_policy_change(actor=claims["name"], action=f"alert_{action}", target=alert_id)
    analytics.invalidate_cache("alerts")  # reflect the new status on the next poll
    return JSONResponse({"ok": True, "id": alert_id, "status": status})


async def _admin_consumer_profile(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    profile = analytics.consumer_profile(request.path_params["cid"])
    if profile is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(profile)


async def _admin_credential_hygiene(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    return JSONResponse({"consumers": analytics.credential_hygiene_cached()})


async def _admin_backend_health(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    probe = request.query_params.get("probe", "1") != "0"
    return JSONResponse({"backends": await analytics.backends_health_cached(probe=probe)})


async def _admin_controls(request):
    """Break-glass controls: pause all agents and/or specific backends. GET reads
    current state + the backend list; PUT sets it (enforced in _govern)."""
    claims, err = _require_admin(request)
    if err:
        return err
    store = get_store()
    if request.method == "GET":
        return JSONResponse({"controls": store.get_controls(), "backends": sorted(manifest.backends())})
    store, err = _writable_or_error()
    if err:
        return err
    body = await request.json()
    controls = {"paused_agents": bool(body.get("paused_agents")),
                "paused_backends": [b for b in (body.get("paused_backends") or []) if b in manifest.backends()]}
    store.set_controls(controls)
    audit.log_policy_change(actor=claims["name"], action="set_controls", target="global",
                            detail=json.dumps(controls))
    return JSONResponse({"ok": True, "controls": store.get_controls()})


# ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ User self-service (own denials, own activity, in-browser tool playground) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬

async def _my_denials(request):
    """This caller's recent 'not granted' denials, each mapped to the category +
    tool it would take to request -- powering the 'why denied -> request' loop."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    name = claims["name"]
    cats = get_store().categories()
    agg: dict[str, dict] = {}
    for c in audit.recent(1000):
        if c.get("type") != "denied" or c.get("consumer") != name or str(c.get("detail") or "") != "not_granted":
            continue
        tool = c.get("tool") or ""
        canon = manifest.canonical(tool)
        if not canon:
            continue
        e = agg.get(tool)
        if not e:
            pol = manifest.get(canon)
            backend = pol.backend if pol else None
            cat = next((k for k in cats if k.backend == backend and (k.tools == "*" or canon in k.tools)), None)
            e = agg[tool] = {"tool": tool, "canonical": canon,
                             "description": (pol.description if pol else canon) or canon,
                             "backend": backend, "category": cat.id if cat else None,
                             "category_name": cat.display_name if cat else None,
                             "risk": pol.risk if pol else None,
                             "attempts": 0, "last_ts": 0}
        e["attempts"] += 1
        e["last_ts"] = max(e["last_ts"], c.get("ts") or 0)
    return JSONResponse({"denials": sorted(agg.values(), key=lambda x: -x["last_ts"])})


async def _my_activity(request):
    """This caller's own governed calls + a small summary + rate-limit headroom."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    name = claims["name"]
    calls = safety.classify([c for c in audit.recent(1000)
                             if c.get("type") in ("call", "denied") and c.get("consumer") == name])
    summary = {
        "total": len(calls),
        "ok": sum(1 for c in calls if c.get("status") == "ok"),
        "denied": sum(1 for c in calls if c.get("status") == "denied"),
        "error": sum(1 for c in calls if c.get("status") == "error"),
        "redactions": sum(1 for c in calls if str(c.get("detail") or "").startswith("redacted:")),
        "rows": sum((c.get("rows") or 0) for c in calls),
    }
    rl = next((r for r in edge.rate_limit_snapshot() if r.get("consumer") == name), None)
    return JSONResponse({"calls": calls[:200], "summary": summary, "rate_limit": rl})


async def _try_tool(request):
    """In-browser playground: run one governed tool call as THIS user (full PDP +
    scope + redaction + audit), so they can test what their grant actually returns."""
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
    tool = str(body.get("tool") or "").strip()
    canon = manifest.canonical(tool) or tool
    if manifest.get(canon) is None:
        return JSONResponse({"error": f"unknown tool {tool!r}"}, status_code=400)
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return JSONResponse({"error": "args must be a JSON object"}, status_code=400)
    policy = manifest.get(canon)
    if policy and policy.backend in ("office", "email", "knowledge", "calendar", "code"):
        args = {**args, "owner": record.name}
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set(ctx.client_ip(request))
    raw = await _govern(canon, "playground:" + record.consumer_id, str(body.get("customer_id") or ""), args)
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        result = raw
    return JSONResponse({"tool": manifest.namespaced(canon), "result": result})


async def _code_plans(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = get_store().get_consumer(claims["sub"])
    if not record or record.status != "active":
        return JSONResponse({"error": "account is not active yet"}, status_code=403)
    all_requested = request.query_params.get("all") in ("1", "true", "yes")
    owner = None if claims["role"] == "admin" and all_requested else claims["name"]
    limit = int(request.query_params.get("limit") or 100)
    records = [r.public_dict(include_template=False) for r in code_plan_store.list_plans(owner=owner, limit=limit)]
    return JSONResponse({"plans": records})


async def _code_plan_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    plan = code_plan_store.get_plan(request.path_params["pid"])
    if not plan:
        return JSONResponse({"error": "not found"}, status_code=404)
    if claims["role"] != "admin" and plan.owner != claims["name"]:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(plan.public_dict(include_template=True))


async def _code_plan_request_approval(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    plan = code_plan_store.get_plan(request.path_params["pid"])
    if not plan:
        return JSONResponse({"error": "not found"}, status_code=404)
    if claims["role"] != "admin" and plan.owner != claims["name"]:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json() if request.headers.get("content-length") else {}
    reason = str(body.get("reason") or f"Approve code plan {plan.plan_id}: {plan.summary}")
    approval = approval_store.create_approval(
        requested_by=claims["name"],
        reason=reason,
        risk_level=plan.risk_level,
        artifact_ids=[],
        workflow_run_id="",
    )
    updated = code_plan_store.attach_approval(plan.plan_id, approval.approval_id) or plan
    audit.log_policy_change(actor=claims["name"], action="request_code_plan_approval", target=plan.plan_id, detail=reason[:200])
    return JSONResponse({"plan": updated.public_dict(), "approval": approval.public_dict()}, status_code=201)


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


async def _admin_requests(request):
    _claims, err = _require_admin(request)
    if err:
        return err
    reqs = get_store().list_access_requests()
    if request.query_params.get("status"):
        reqs = [r for r in reqs if r.get("status") == request.query_params["status"]]
    return JSONResponse({"requests": reqs})


def _find_request(store, rid):
    for r in store.list_access_requests():
        if r.get("id") == rid:
            return r
    return None


async def _admin_request_approve(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    rid = request.path_params["rid"]
    req = _find_request(store, rid)
    if not req:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json() if request.headers.get("content-length") else {}
    record = store.get_consumer(req["consumer_id"])
    if not record:
        return JSONResponse({"error": "consumer no longer exists"}, status_code=409)

    if req["kind"] == "account":
        categories = (body.get("categories") or req.get("categories") or record.categories)
        store.upsert_consumer(replace(record, status="active", categories=list(categories),
                                      overrides=body.get("overrides") or record.overrides))
    elif "selections" in req:  # access request (tool-level, grouped by category)
        overrides = {k: dict(v) for k, v in (record.overrides or {}).items()}
        any_valid = False
        for sel in req.get("selections") or []:
            backend = sel.get("backend") or ""
            tools = [t for t in (sel.get("tools") or []) if t]
            if backend not in manifest.backends() or not tools:
                continue
            any_valid = True
            b = overrides.get(backend, {})
            b["grantTools"] = sorted(set(b.get("grantTools", [])) | set(tools))
            # Grant the category's levels too, else the newly-granted tool comes
            # back with everything classified redacted -- the requester picked
            # tools, not PUBLIC/INTERNAL/PII/SENSITIVE, so this is what makes the
            # grant actually show real data.
            category = store.get_category(sel.get("category"))
            if category:
                b["grantLevels"] = sorted(set(b.get("grantLevels", [])) | set(category.levels))
            overrides[backend] = b
        if not any_valid:
            return JSONResponse(
                {"error": "request has no valid selections; deny it and ask the requester to resubmit"},
                status_code=409,
            )
        store.upsert_consumer(replace(record, overrides=overrides))
    elif "categories" in req:  # access request (whole-category), from an earlier
        # iteration of this self-service redesign -- kept so any request already
        # pending when this shipped can still be approved.
        requested = [c for c in (req.get("categories") or []) if store.get_category(c) is not None]
        if not requested:
            return JSONResponse(
                {"error": "request has no valid categories; deny it and ask the requester to resubmit"},
                status_code=409,
            )
        categories = sorted(set(record.categories or []) | set(requested))
        store.upsert_consumer(replace(record, categories=categories))
    else:
        # Legacy shape (backend/tools/levels -> overrides), from before the
        # category-based self-service redesign -- kept only so a request already
        # pending when this shipped can still be approved.
        backend = req.get("backend") or ""
        if backend not in manifest.backends():
            # No silent default to a guessed backend -- a request predating the
            # 2026-07 domain split may have no backend or the stale "minierp".
            return JSONResponse(
                {"error": f"request has no valid backend ({backend!r}); deny it and ask the "
                          f"requester to resubmit with one of: {sorted(manifest.backends())}"},
                status_code=409,
            )
        overrides = {k: dict(v) for k, v in (record.overrides or {}).items()}
        b = overrides.get(backend, {})
        b["grantTools"] = sorted(set(b.get("grantTools", [])) | set(req.get("tools") or []))
        b["grantLevels"] = sorted(set(b.get("grantLevels", [])) | set(req.get("levels") or []))
        overrides[backend] = b
        store.upsert_consumer(replace(record, overrides=overrides))

    store.update_access_request(rid, {"status": "approved", "decided_by": claims["name"], "decided_at": time.time()})
    audit.log_policy_change(actor=claims["name"], action=f"approve_{req['kind']}", target=req["consumer_id"])
    return JSONResponse({"ok": True})


async def _admin_request_deny(request):
    claims, err = _require_admin(request)
    if err:
        return err
    store, err = _writable_or_error()
    if err:
        return err
    rid = request.path_params["rid"]
    req = _find_request(store, rid)
    if not req:
        return JSONResponse({"error": "not found"}, status_code=404)
    if req["kind"] == "account":
        record = store.get_consumer(req["consumer_id"])
        if record:
            store.upsert_consumer(replace(record, status="disabled"))
    store.update_access_request(rid, {"status": "denied", "decided_by": claims["name"], "decided_at": time.time()})
    audit.log_policy_change(actor=claims["name"], action=f"deny_{req['kind']}", target=req["consumer_id"])
    return JSONResponse({"ok": True})


async def _admin_access_suggestions(request):
    """Denial-derived access requests: aggregate 'not_granted' denials by (consumer, tool)."""
    _claims, err = _require_admin(request)
    if err:
        return err
    agg: dict[tuple, int] = {}
    for r in audit.recent(1000):
        if r.get("type") == "denied" and r.get("detail") == "not_granted":
            key = (r.get("consumer"), r.get("tool"))
            agg[key] = agg.get(key, 0) + 1
    suggestions = [{"consumer": c, "tool": t, "attempts": n} for (c, t), n in agg.items()]
    suggestions.sort(key=lambda s: s["attempts"], reverse=True)
    return JSONResponse({"suggestions": suggestions})


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
            closed = chat_log.close_idle_sessions(store, _CHAT_IDLE_SEC, llm_complete)
            if closed:
                print(f"[chat-sweep] closed {closed} idle session(s)", flush=True)
        except Exception as exc:  # the sweep must never crash the process
            print(f"[chat-sweep] failed: {exc}", file=sys.stderr, flush=True)


app = mcp.streamable_http_app()

# FastMCP's own lifespan (app.router.lifespan_context) runs its stateless-http
# session manager; this Starlette version dropped add_event_handler("startup"), so
# to also start the chat-idle sweep we wrap that lifespan rather than replace it --
# the session manager still runs, we just additionally spin up the sweep task for
# the life of the process and cancel it on shutdown.
_mcp_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_with_chat_sweep(asgi_app):
    task = asyncio.create_task(_chat_sweep_loop())
    try:
        async with _mcp_lifespan(asgi_app) as state:
            yield state
    finally:
        task.cancel()


app.router.lifespan_context = _lifespan_with_chat_sweep
app.add_route("/", _root)
app.add_route("/health", _health)
app.add_route("/dashboard", _dashboard)
# All app-shell section routes serve the same SPA shell (session-gated); the
# client renders the panel from the path. Admin panels are hidden for non-admins
# and every admin API enforces the role server-side.
for _p in ("admin", "assistant", "access", "consumers", "requests", "categories", "department-admin", "whitelist", "monitor", "history", "alerts", "security", "activity", "developer", "home", "playground", "files", "workflows", "my_workflows", "automations", "knowledge", "sends", "calendar", "approvals", "templates", "code-plans", "agents"):
    app.add_route(f"/dashboard/{_p}", _dashboard)
app.add_route("/dashboard/signup", _signup_page)
app.add_route("/dashboard/logo.png", _logo)
app.add_route("/dashboard/departments", _departments)
app.add_route("/dashboard/category-catalog", _categories_catalog)
app.add_route("/dashboard/login", _login, methods=["POST"])
app.add_route("/dashboard/signup", _signup, methods=["POST"])
app.add_route("/dashboard/logout", _logout, methods=["POST"])
app.add_route("/dashboard/me", _me)
app.add_route("/dashboard/my-access", _my_access)
app.add_route("/dashboard/my-key/rotate", _my_key_rotate, methods=["POST"])
app.add_route("/dashboard/request-access", _request_access, methods=["POST"])
app.add_route("/dashboard/chat", _chat_page)
app.add_route("/chat", _chat, methods=["POST"])
app.add_route("/chat/stream", _chat_stream, methods=["POST"])
app.add_route("/dashboard/chat-history", _chat_history)
app.add_route("/dashboard/chat-history/{sid}", _chat_transcript)
app.add_route("/dashboard/chat-history/{sid}/resume", _chat_resume, methods=["POST"])
app.add_route("/admin/calls", _admin_calls)
app.add_route("/admin/audit-export", _admin_audit_export, methods=["POST"])
app.add_route("/admin/sessions", _admin_sessions)
app.add_route("/admin/rate-limits", _admin_rate_limits)
app.add_route("/admin/catalog", _admin_catalog)
app.add_route("/admin/consumers", _admin_consumers, methods=["GET", "POST"])
app.add_route("/admin/consumers/{cid}", _admin_consumer_item, methods=["PATCH", "DELETE"])
app.add_route("/admin/consumers/{cid}/rotate-key", _admin_consumer_rotate, methods=["POST"])
app.add_route("/admin/categories", _admin_categories, methods=["GET", "POST"])
app.add_route("/admin/categories/{cid}", _admin_category_item, methods=["DELETE"])
app.add_route("/admin/departments", _admin_departments, methods=["GET", "POST"])
app.add_route("/admin/departments/{did}", _admin_department_item, methods=["DELETE"])
app.add_route("/admin/whitelist", _admin_whitelist, methods=["GET", "PUT"])
app.add_route("/admin/policy-changes", _admin_policy_changes)
app.add_route("/admin/requests", _admin_requests)
app.add_route("/admin/requests/{rid}/approve", _admin_request_approve, methods=["POST"])
app.add_route("/admin/requests/{rid}/deny", _admin_request_deny, methods=["POST"])
app.add_route("/admin/access-suggestions", _admin_access_suggestions)
# Security-monitoring analytics
app.add_route("/admin/overview", _admin_overview)
app.add_route("/admin/alerts", _admin_alerts)
app.add_route("/admin/alerts/{aid}/{action}", _admin_alert_action, methods=["POST"])
app.add_route("/admin/consumers/{cid}/profile", _admin_consumer_profile)
app.add_route("/admin/credential-hygiene", _admin_credential_hygiene)
app.add_route("/admin/agents", _admin_agents, methods=["GET", "POST"])
app.add_route("/admin/agents/{aid}", _admin_agent_item, methods=["GET", "PATCH"])
app.add_route("/admin/backends/health", _admin_backend_health)
app.add_route("/admin/controls", _admin_controls, methods=["GET", "PUT"])
app.add_route("/dashboard/my-denials", _my_denials)
app.add_route("/dashboard/my-activity", _my_activity)
app.add_route("/dashboard/try-tool", _try_tool, methods=["POST"])
app.add_route("/dashboard/feedback", _chat_feedback, methods=["POST"])
app.add_route("/email-sends", _email_sends)
app.add_route("/email-sends/{sid}", _email_send_item)
app.add_route("/calendar-sends", _calendar_sends)
app.add_route("/calendar-sends/{sid}", _calendar_send_item)
app.add_route("/templates", _templates, methods=["GET", "POST"])
app.add_route("/templates/{tid}", _template_item, methods=["GET", "PATCH"])
app.add_route("/templates/{tid}/versions", _template_versions, methods=["POST"])
app.add_route("/templates/{tid}/disable", _template_disable, methods=["POST"])
app.add_route("/code-plans", _code_plans)
app.add_route("/code-plans/{pid}", _code_plan_item)
app.add_route("/code-plans/{pid}/request-approval", _code_plan_request_approval, methods=["POST"])
app.add_route("/knowledge/documents", _knowledge_documents, methods=["GET"])
app.add_route("/knowledge/documents/{did}", _knowledge_document_item, methods=["GET", "DELETE"])
app.add_route("/knowledge/search", _knowledge_search, methods=["POST"])
app.add_route("/knowledge/answer", _knowledge_answer, methods=["POST"])
app.add_route("/automations", _automations, methods=["GET", "POST"])
app.add_route("/automations/run-due", _automation_run_due, methods=["POST"])
app.add_route("/automations/{aid}", _automation_item, methods=["GET", "DELETE"])
app.add_route("/approvals", _approvals, methods=["GET", "POST"])
app.add_route("/approvals/{aid}/{action}", _approval_decide, methods=["POST"])
app.add_route("/workflow-suggestions", _workflow_suggestions, methods=["POST"])
app.add_route("/workflow-suggestions/launch", _workflow_suggestion_launch, methods=["POST"])
app.add_route("/workflows", _workflows)
app.add_route("/workflows/{tid}", _workflow_template)
app.add_route("/workflows/{tid}/preflight", _workflow_preflight, methods=["GET", "POST"])
app.add_route("/admin/workflow-health", _admin_workflow_health)
app.add_route("/admin/workflows/{tid}/{action}", _admin_workflow_template_status, methods=["POST"])
app.add_route("/workflows/{tid}/run", _workflow_run_start, methods=["POST"])
app.add_route("/workflow-runs", _workflow_runs)
app.add_route("/workflow-runs/{rid}", _workflow_run_item)
app.add_route("/workflow-runs/{rid}/export-evidence", _workflow_run_export_evidence, methods=["POST"])
app.add_route("/workflow-runs/{rid}/cancel", _workflow_run_cancel, methods=["POST"])
app.add_route("/workflow-runs/{rid}/resume", _workflow_run_resume, methods=["POST"])
app.add_route("/workflow-graphs", _workflow_graphs, methods=["GET", "POST"])
app.add_route("/workflow-graphs/{gid}", _workflow_graph_item)
app.add_route("/workflow-graphs/{gid}/versions", _workflow_graph_versions, methods=["POST"])
app.add_route("/workflow-graphs/{gid}/publish", _workflow_graph_publish, methods=["POST"])
app.add_route("/workflow-graphs/{gid}/validate", _workflow_graph_validate, methods=["POST"])
app.add_route("/dashboard/workflow-graph-catalog", _workflow_graph_catalog)
app.add_route("/artifacts", _artifacts, methods=["GET", "POST"])
app.add_route("/artifacts/{aid}", _artifact_metadata, methods=["GET", "DELETE"])
app.add_route("/artifacts/{aid}/reviews", _artifact_reviews, methods=["GET", "POST"])
app.add_route("/artifacts/{aid}/reviews/{rid}", _artifact_review_item)
app.add_route("/artifacts/{aid}/reviews/{rid}/comments", _artifact_review_comments, methods=["POST"])
app.add_route("/artifacts/{aid}/reviews/{rid}/{action}", _artifact_review_decision, methods=["POST"])
app.add_route("/artifacts/{aid}/versions", _artifact_versions, methods=["GET", "POST"])
app.add_route("/artifacts/{aid}/versions/{vid}/download", _artifact_version_download)
app.add_route("/artifacts/{aid}/shares", _artifact_shares, methods=["GET", "POST"])
app.add_route("/artifacts/{aid}/shares/{sid}/revoke", _artifact_share_revoke, methods=["POST"])
app.add_route("/admin/artifacts/purge-expired", _admin_artifact_purge_expired, methods=["POST"])
app.add_route("/artifacts/{aid}/workbench", _artifact_workbench)
app.add_route("/artifacts/{aid}/request-approval", _artifact_request_approval, methods=["POST"])
app.add_route("/artifacts/{aid}/download", _artifact_download)
app.add_middleware(edge.EdgeMiddleware)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("GATEWAY_PORT") or "8020")
    print(f"[gateway] Starting Governance Gateway on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
