"""Governance Gateway (PEP) -- the single MCP endpoint for all callers.

Every AI agent and user talks ONLY to this server. It authenticates the caller
(edge.py / EdgeMiddleware, Layer 1), and for each tool call it runs the
deterministic PDP (policy.decision.decide), resolves session scope server-side,
forwards to the owning backend MCP server as an MCP client (mcp_clients.call),
applies the PDP's redaction plan to the result, and audits the outcome.

Backends hold the credentials and do the data access; this process holds none.
Tools are exposed namespaced as "<backend>_<canonical>" (e.g.
minierp_orders_get_customer_orders) so multiple backends can be federated behind
one surface without name collisions.

This module is now the MCP (machine) plane plus app construction:
    govern.py   the enforcement pipeline both planes share
    backend/    every HTTP route the browser calls, one module per domain

This is Stage 1 of governence_agent/design_plan.md: gateway + the miniERP
domain backends.

Run:
    pip install -r requirements.txt
    python app.py            # serves MCP on GATEWAY_PORT (default 8020) at /mcp
"""
from __future__ import annotations

import ast
import asyncio
import json
import math
import operator
import os
import statistics
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from pydantic import Field

load_dotenv(os.getenv("GATEWAY_ENV_FILE") or ".env.local")

# governance_core holds the shared plane (edge/audit/scope_store) + the policy
# package. Put it on sys.path so its modules import flatly, as they did when they
# lived in one service. Everything below this line depends on it, including the
# backend package and govern.
_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import uvicorn

import edge
import request_context as ctx
import workflow_graph_store
import workflow_scratchpad
import workflows
from policy import manifest
from policy.resolve import resolve as resolve_grant
from store import get_store

import backend
import schema_catalog
from backend.chat import _chat_sweep_loop
from backend.workflow_graphs import _graph_edge_from_wire, _graph_node_from_wire
from govern import _govern
from mcp_server import mcp


# ── The governed tool plane ───────────────────────────────────────────────────
# Explicit signatures give callers a proper input schema. Each tool just marshals
# its args and defers all policy to _govern (govern.py). Tool names are namespaced
# "<backend>_<canonical>" (policy.manifest.namespaced) -- the backend prefix here
# MUST match each tool's ToolPolicy.backend in policy/manifest.py.
#
# Every tool below takes session_id and most take customer_id -- both are
# annotated once here so every tool's MCP input schema documents them the same
# way, instead of relying on a docstring sentence that only some tools happen to
# have. session_id can't be scoped down to "the one tool that needs it": every
# call is required to pass one because _govern (govern.py) uses it for THREE
# things on every single call, not just customer_id memory -- scope resolution
# (_resolve_scope), the audit trail (audit.log_call/log_denied), and break-glass
# pause bookkeeping (scope_store.touch). Drop it from a tool's signature and that
# call becomes unaudited and unscoped, not merely less convenient.
SessionId = Annotated[str, Field(description=(
    "Caller-generated id for this conversation. Use any unique string (e.g. a "
    "UUID) and reuse the SAME value for every tool call in the conversation -- "
    "the gateway uses it to (1) remember a resolved customer_id across calls so "
    "you don't have to re-resolve it every time, and (2) correlate this call "
    "with the rest of the conversation in the audit trail. A new value starts a "
    "fresh session with no remembered customer_id."
))]

CustomerId = Annotated[str, Field(description=(
    "Internal customer id (acctCd). Only required the first time in a session; "
    "once supplied it is remembered for this session_id and later calls may "
    "omit it. If you only have a name/email/phone, call "
    "minierp_accounts_find_customer first and use the customerId it returns."
))]

@mcp.tool(name="minierp_orders_get_customer_orders")
async def minierp_orders_get_customer_orders(
    session_id: SessionId,
    customer_id: CustomerId = "",
    start_date: str = "",
    end_date: str = "",
    order_status: str = "",
    min_total: float | None = None,
    page: int = 1,
    page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List a customer's sales orders, optionally filtered by date range, status, or minimum total.

    customer_id is only needed the first time in a session; once supplied it is
    remembered for this session_id and later calls may omit it. fetch_all=true
    returns every matching order in one call instead of paging manually."""
    return await _govern("get_customer_orders", session_id, customer_id, {
        "start_date": start_date, "end_date": end_date, "order_status": order_status,
        "min_total": min_total, "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_orders_get_customer_order_total")
async def minierp_orders_get_customer_order_total(
    session_id: SessionId, customer_id: CustomerId = "", start_date: str = "", end_date: str = "",
) -> str:
    """Calculate a customer's total spend across completed orders, optionally within a date range."""
    return await _govern("get_customer_order_total", session_id, customer_id, {
        "start_date": start_date, "end_date": end_date,
    })


@mcp.tool(name="minierp_orders_get_order_details")
async def minierp_orders_get_order_details(session_id: SessionId, order_number: str, company_id: int | None = None) -> str:
    """Get header-level details (status, total, date) for one sales order by order number."""
    return await _govern("get_order_details", session_id, "", {
        "order_number": order_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_orders_get_product_details_in_order")
async def minierp_orders_get_product_details_in_order(
    session_id: SessionId, order_number: str, company_id: int | None = None, page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List the line items (products, quantities, prices) inside one sales
    order. fetch_all=true returns every line item in one call instead of
    paging manually."""
    return await _govern("get_product_details_in_order", session_id, "", {
        "order_number": order_number, "company_id": company_id, "page": page, "page_size": page_size,
        "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_accounts_get_customer_profile")
async def minierp_accounts_get_customer_profile(session_id: SessionId, customer_id: CustomerId = "") -> str:
    """Get a customer's billing/credit profile: credit limit, terms, default payment method, statement cycle."""
    return await _govern("get_customer_profile", session_id, customer_id, {})


@mcp.tool(name="minierp_accounts_get_contacts")
async def minierp_accounts_get_contacts(
    session_id: SessionId, customer_id: CustomerId = "", page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List contacts (name, role, phone, email) on a customer's account.
    fetch_all=true returns every contact in one call instead of paging
    manually."""
    return await _govern("get_contacts", session_id, customer_id, {
        "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_accounts_get_addresses")
async def minierp_accounts_get_addresses(
    session_id: SessionId, customer_id: CustomerId = "", page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List addresses on file for a customer's account. fetch_all=true
    returns every address in one call instead of paging manually."""
    return await _govern("get_addresses", session_id, customer_id, {
        "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_accounts_find_customer")
async def minierp_accounts_find_customer(
    session_id: SessionId, query: str, by: str = "auto", company_id: str = "", page: int = 1, page_size: int = 10,
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
    session_id: SessionId, query: str, by: str = "auto", page: int = 1, page_size: int = 10,
) -> str:
    """Find the people (contacts) behind an email, phone, or name: name, title, and their account."""
    return await _govern("resolve_contact", session_id, "", {
        "query": query, "by": by, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_accounts_get_order_addresses")
async def minierp_accounts_get_order_addresses(session_id: SessionId, order_number: str) -> str:
    """Get the ship-to and bill-to addresses for a sales order by order number."""
    return await _govern("get_order_addresses", session_id, "", {"order_number": order_number})


@mcp.tool(name="minierp_accounts_get_customer_overview")
async def minierp_accounts_get_customer_overview(session_id: SessionId, customer_id: CustomerId = "") -> str:
    """One-call 360 view of a customer: profile, primary contact, addresses, recent
    orders, and total spend. Prefer this over separate profile/contacts/orders calls."""
    return await _govern("get_customer_overview", session_id, customer_id, {})


@mcp.tool(name="minierp_accounts_get_order_overview")
async def minierp_accounts_get_order_overview(session_id: SessionId, order_number: str, company_id: int | None = None) -> str:
    """One-call 360 view of an order: header, line items, ship/bill addresses, and
    shipment/tracking. Prefer this over separate order/product/shipping calls."""
    return await _govern("get_order_overview", session_id, "", {
        "order_number": order_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_orders_get_customer_order_summary")
async def minierp_orders_get_customer_order_summary(
    session_id: SessionId, customer_id: CustomerId = "", start_date: str = "", end_date: str = "",
) -> str:
    """Aggregate a customer's orders: counts by status, total/average spend, first/last
    order date, and month-by-month buckets over a date range."""
    return await _govern("get_customer_order_summary", session_id, customer_id, {
        "start_date": start_date, "end_date": end_date,
    })


@mcp.tool(name="minierp_orders_get_product_sales")
async def minierp_orders_get_product_sales(
    session_id: SessionId, inventory_id: str, start_date: str = "", end_date: str = "",
) -> str:
    """How a product is selling: total quantity, revenue, distinct orders/customers."""
    return await _govern("get_product_sales", session_id, "", {
        "inventory_id": inventory_id, "start_date": start_date, "end_date": end_date,
    })


@mcp.tool(name="minierp_orders_get_orders_by_product")
async def minierp_orders_get_orders_by_product(
    session_id: SessionId, inventory_id: str, page: int = 1, page_size: int = 25,
) -> str:
    """Which orders (and customers) bought a given product, by inventory id."""
    return await _govern("get_orders_by_product", session_id, "", {
        "inventory_id": inventory_id, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_accounts_get_customers_by_region")
async def minierp_accounts_get_customers_by_region(
    session_id: SessionId, country: str = "", state: str = "", city: str = "", page: int = 1, page_size: int = 25,
) -> str:
    """Customers in a territory, filtered by country, state, and/or city."""
    return await _govern("get_customers_by_region", session_id, "", {
        "country": country, "state": state, "city": city, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_shipments_get_customer_shipment_status")
async def minierp_shipments_get_customer_shipment_status(
    session_id: SessionId, customer_id: CustomerId = "", max_orders: int = 5,
) -> str:
    """Shipment/tracking status of a customer's most recent orders."""
    return await _govern("get_customer_shipment_status", session_id, customer_id, {
        "max_orders": max_orders,
    })


@mcp.tool(name="minierp_analytics_get_top_customers_by_spend")
async def minierp_analytics_get_top_customers_by_spend(
    session_id: SessionId, start_date: str = "", end_date: str = "", limit: int = 10,
) -> str:
    """Rank customers by total spend over a period (cross-customer analytics; requires
    the analytics entitlement)."""
    return await _govern("get_top_customers_by_spend", session_id, "", {
        "start_date": start_date, "end_date": end_date, "limit": limit,
    })


@mcp.tool(name="minierp_analytics_get_customer_order_recency")
async def minierp_analytics_get_customer_order_recency(
    session_id: SessionId, country: str = "", state: str = "", city: str = "", page: int = 1, page_size: int = 25,
) -> str:
    """Cross-customer order recency by territory: order count, last-order date, and a
    best-effort phone number per customer (a win-back / reorder-due signal), for
    customers matching country/state/city (cross-customer analytics; requires the
    analytics entitlement). phone is a best-effort contact number (lowest-id
    contact on file with one), not a verified primary contact."""
    return await _govern("get_customer_order_recency", session_id, "", {
        "country": country, "state": state, "city": city, "page": page, "page_size": page_size,
    })


@mcp.tool(name="office_create_excel_report")
async def office_create_excel_report(
    session_id: SessionId,
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
    session_id: SessionId,
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
    session_id: SessionId,
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
    session_id: SessionId,
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
async def office_convert_artifact(session_id: SessionId, artifact_id: str, target_format: str = "txt", title: str = "") -> str:
    """Convert a governed artifact to TXT or PDF."""
    return await _govern("convert_artifact", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "artifact_id": artifact_id,
        "target_format": target_format,
        "title": title,
    })


@mcp.tool(name="office_edit_office_document")
async def office_edit_office_document(session_id: SessionId, artifact_id: str, edits: str) -> str:
    """Apply a small whitelisted set of edits to an existing governed XLSX/DOCX
    artifact via ONLYOFFICE Document Builder, and persist the result as a new
    artifact version. `edits` is a JSON-encoded string describing ops, e.g. for
    xlsx: '[{"op":"set_cell","sheet":"Sheet1","cell":"B4","value":"500"}]';
    for docx: '[{"op":"replace_text","find":"TBD","replace":"Q3 2026"}]'."""
    return await _govern("edit_office_document", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "artifact_id": artifact_id,
        "edits": edits,
    })


@mcp.tool(name="office_extract_tables_from_document")
async def office_extract_tables_from_document(session_id: SessionId, artifact_id: str, create_json_artifact: bool = False) -> str:
    """Extract table-like data from a governed XLSX/DOCX artifact."""
    return await _govern("extract_tables_from_document", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "artifact_id": artifact_id,
        "create_json_artifact": create_json_artifact,
    })


@mcp.tool(name="email_create_email_draft")
async def email_create_email_draft(
    session_id: SessionId,
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
async def email_send_email_draft(session_id: SessionId, draft_id: str, approval_id: str = "") -> str:
    """Request delivery for an email draft. External sending is approval-gated in this scaffold."""
    return await _govern("send_email_draft", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "draft_id": draft_id,
        "approval_id": approval_id,
    })


@mcp.tool(name="calendar_draft_calendar_invite")
async def calendar_draft_calendar_invite(
    session_id: SessionId,
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
async def calendar_send_calendar_invite(session_id: SessionId, draft_id: str, approval_id: str = "") -> str:
    """Queue an approved calendar invite for connector-backed event creation."""
    return await _govern("send_calendar_invite", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "draft_id": draft_id,
        "approval_id": approval_id,
    })


@mcp.tool(name="calendar_create_meeting_brief")
async def calendar_create_meeting_brief(
    session_id: SessionId,
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
async def calendar_list_upcoming_meetings(session_id: SessionId, within_days: int = 14, limit: int = 20) -> str:
    """List this user's drafted calendar invites starting within the next N days."""
    return await _govern("list_upcoming_meetings", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "within_days": within_days,
        "limit": limit,
    })


@mcp.tool(name="knowledge_search_knowledge")
async def knowledge_search_knowledge(session_id: SessionId, query: str, limit: int = 5, document_id: str = "") -> str:
    """Search indexed knowledge chunks visible to this consumer (direct Azure AI
    Search, then an HTTP proxy, then a local fallback store -- first configured
    tier wins; not local-only)."""
    return await _govern("search_knowledge", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "query": query,
        "limit": limit,
        "document_id": document_id,
    })


@mcp.tool(name="knowledge_answer_from_knowledge")
async def knowledge_answer_from_knowledge(session_id: SessionId, query: str, limit: int = 5, document_id: str = "") -> str:
    """Return a citation-backed extractive answer from indexed documents (same
    direct/proxy/local precedence as knowledge_search_knowledge; not local-only)."""
    return await _govern("answer_from_knowledge", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "query": query,
        "limit": limit,
        "document_id": document_id,
    })


# ── Personal knowledge tier -- private per-owner, no admin bypass ─────────────
# `owner` is hardcoded from the authenticated session on every one of these five,
# exactly like the company-tier wrappers above -- never a parameter an LLM/caller
# can set. This is the actual enforcement point for "personal docs are private to
# their owner" (see digest_persoanl_kb.md §0's "Owner identity source" row).

@mcp.tool(name="knowledge_ingest_my_document")
async def knowledge_ingest_my_document(
    session_id: SessionId, title: str, filename: str, content_base64: str, classification: str = "",
) -> str:
    """Upload a document into YOUR OWN private knowledge base (PDF, docx, pptx,
    xlsx, txt, md, csv). Not visible to anyone else, including admins."""
    return await _govern("ingest_my_document", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "title": title,
        "filename": filename,
        "content_base64": content_base64,
        "classification": classification,
    })


@mcp.tool(name="knowledge_search_my_documents")
async def knowledge_search_my_documents(session_id: SessionId, query: str, limit: int = 5, document_id: str = "") -> str:
    """Search YOUR OWN private document chunks (semantic search, falls back to
    keyword search automatically)."""
    return await _govern("search_my_documents", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "query": query,
        "limit": limit,
        "document_id": document_id,
    })


@mcp.tool(name="knowledge_answer_from_my_documents")
async def knowledge_answer_from_my_documents(session_id: SessionId, query: str, limit: int = 5, document_id: str = "") -> str:
    """Return a citation-backed extractive answer from YOUR OWN private documents."""
    return await _govern("answer_from_my_documents", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "query": query,
        "limit": limit,
        "document_id": document_id,
    })


@mcp.tool(name="knowledge_list_my_documents")
async def knowledge_list_my_documents(session_id: SessionId, limit: int = 100) -> str:
    """List documents in YOUR OWN private knowledge base."""
    return await _govern("list_my_documents", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "limit": limit,
    })


@mcp.tool(name="knowledge_delete_my_document")
async def knowledge_delete_my_document(session_id: SessionId, document_id: str) -> str:
    """Delete a document from YOUR OWN private knowledge base."""
    return await _govern("delete_my_document", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "document_id": document_id,
    })


@mcp.tool(name="code_opencode_plan_change")
async def code_opencode_plan_change(session_id: SessionId, request: str, target_area: str = "", risk_level: str = "medium") -> str:
    """Create a read-only opencode implementation plan for a requested codebase change."""
    return await _govern("opencode_plan_change", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "request": request,
        "target_area": target_area,
        "risk_level": risk_level,
    })


@mcp.tool(name="code_opencode_review_repo")
async def code_opencode_review_repo(session_id: SessionId, focus: str = "", paths: list[str] | None = None) -> str:
    """Create a read-only repository review plan."""
    return await _govern("opencode_review_repo", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "focus": focus,
        "paths": paths or [],
    })


@mcp.tool(name="code_opencode_generate_template")
async def code_opencode_generate_template(session_id: SessionId, goal: str, template_type: str = "workflow") -> str:
    """Generate a governed workflow/template draft without applying code changes."""
    return await _govern("opencode_generate_template", session_id, "", {
        "owner": ctx.consumer_ctx.get() or "unknown",
        "goal": goal,
        "template_type": template_type,
    })


@mcp.tool(name="minierp_shipments_get_shipping_by_shipment")
async def minierp_shipments_get_shipping_by_shipment(session_id: SessionId, shipment_number: str, customer_id: CustomerId = "") -> str:
    """Get tracking/invoice details for a shipment by shipment number.

    Requires a customer_id in scope -- the shipment is only released if it is
    linked to that customer's orders."""
    return await _govern("get_shipping_by_shipment", session_id, customer_id, {
        "shipment_number": shipment_number,
    })


@mcp.tool(name="minierp_shipments_get_shipping_by_order")
async def minierp_shipments_get_shipping_by_order(session_id: SessionId, order_number: str, company_id: int | None = None) -> str:
    """Get shipment/tracking/invoice numbers linked to a sales order by order number."""
    return await _govern("get_shipping_by_order", session_id, "", {
        "order_number": order_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_finance_get_invoice_details")
async def minierp_finance_get_invoice_details(session_id: SessionId, invoice_number: str, company_id: int | None = None) -> str:
    """Get header-level details (total, tax, unpaid balance, terms) for one AR invoice by invoice/reference number.

    Not customer-ownership-gated (ARInvoice has no customer link field in this
    schema) -- same trust model as minierp_orders_get_order_details."""
    return await _govern("get_invoice_details", session_id, "", {
        "invoice_number": invoice_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_finance_get_vendor_details")
async def minierp_finance_get_vendor_details(session_id: SessionId, vendor_code: str) -> str:
    """Get a vendor's profile: class, terms, currency, default payment method, 1099 flag."""
    return await _govern("get_vendor_details", session_id, "", {"vendor_code": vendor_code})


@mcp.tool(name="minierp_finance_get_vendor_ap_invoices")
async def minierp_finance_get_vendor_ap_invoices(
    session_id: SessionId, vendor_code: str, page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List AP invoices/bills for a vendor by vendor code. Prefer fetch_all=true
    over manually paging page=1,2,3... when you need this vendor's complete
    invoice history -- one governed call instead of several round trips."""
    return await _govern("get_vendor_ap_invoices", session_id, "", {
        "vendor_code": vendor_code, "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_ap_invoice_details")
async def minierp_finance_get_ap_invoice_details(
    session_id: SessionId, invoice_number: str, company_id: int | None = None,
) -> str:
    """Get header-level details (total, tax, due date, paid status) for one AP invoice by invoice/reference number."""
    return await _govern("get_ap_invoice_details", session_id, "", {
        "invoice_number": invoice_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_finance_get_po_order_status")
async def minierp_finance_get_po_order_status(
    session_id: SessionId, po_number: str, company_id: int | None = None,
) -> str:
    """Get header-level status (status, total, vendor, ship-via, hold) for one purchase order by PO number."""
    return await _govern("get_po_order_status", session_id, "", {
        "po_number": po_number, "company_id": company_id,
    })


@mcp.tool(name="minierp_finance_get_gl_account_transactions")
async def minierp_finance_get_gl_account_transactions(
    session_id: SessionId,
    account_cd: str,
    start_date: str = "",
    end_date: str = "",
    company_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List GL transactions for one account code, optionally filtered by date
    range, with a net debit/credit movement for the returned page.
    fetch_all=true returns every matching transaction in one call instead of
    paging manually."""
    return await _govern("get_gl_account_transactions", session_id, "", {
        "account_cd": account_cd, "start_date": start_date, "end_date": end_date,
        "company_id": company_id, "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_sales_price")
async def minierp_finance_get_sales_price(
    session_id: SessionId,
    inventory_id: str,
    cust_price_class_id: str = "",
    customer_id: CustomerId = "",
    company_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List sales price records (price class, currency, UOM, break quantity)
    for one inventory item, optionally narrowed to a price class or customer.
    fetch_all=true returns every matching price record in one call instead
    of paging manually."""
    return await _govern("get_sales_price", session_id, "", {
        "inventory_id": inventory_id, "cust_price_class_id": cust_price_class_id,
        "customer_id": customer_id, "company_id": company_id,
        "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_ap_invoices_due_soon")
async def minierp_finance_get_ap_invoices_due_soon(
    session_id: SessionId, days_ahead: int = 14, company_id: int | None = None, page: int = 1, page_size: int = 250,
) -> str:
    """Cross-vendor: AP invoices due within the next N days, across every vendor (not one
    vendor at a time like minierp_finance_get_vendor_ap_invoices) -- an AP-aging / due-soon
    signal for a workflow's filter step."""
    return await _govern("get_ap_invoices_due_soon", session_id, "", {
        "days_ahead": days_ahead, "company_id": company_id, "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_finance_get_ar_invoices_past_due")
async def minierp_finance_get_ar_invoices_past_due(
    session_id: SessionId, min_invoice_age_days: int = 30, company_id: int | None = None,
    page: int = 1, page_size: int = 250,
) -> str:
    """Cross-customer: AR invoices older than N days that may still be outstanding, across
    every customer -- an AR-aging signal for a workflow's filter step. NOTE: ARInvoice has no
    due-date column or customer link in this schema, so this ages by invoice date and cannot
    be attributed to a specific customer. ONE real page per call -- set paginate:true on this
    node for the full dataset."""
    return await _govern("get_ar_invoices_past_due", session_id, "", {
        "min_invoice_age_days": min_invoice_age_days, "company_id": company_id,
        "page": page, "page_size": page_size,
    })


@mcp.tool(name="minierp_finance_get_po_line_items")
async def minierp_finance_get_po_line_items(
    session_id: SessionId, po_number: str, company_id: int | None = None, page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List the line items (product, quantities, unit/extended cost) inside
    one purchase order by PO number. fetch_all=true returns every line item
    in one call instead of paging manually."""
    return await _govern("get_po_line_items", session_id, "", {
        "po_number": po_number, "company_id": company_id, "page": page, "page_size": page_size,
        "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_ar_payment_history")
async def minierp_finance_get_ar_payment_history(
    session_id: SessionId, customer_id: CustomerId = "", page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List which invoices a customer's payments/credit memos were applied
    to, when, and for how much. fetch_all=true returns this customer's
    complete payment history in one call instead of paging manually."""
    return await _govern("get_ar_payment_history", session_id, customer_id, {
        "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_ap_payment_history")
async def minierp_finance_get_ap_payment_history(
    session_id: SessionId, vendor_code: str, page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List which bills a vendor's payments were applied to, when, and for how
    much. Prefer fetch_all=true over manually paging page=1,2,3... when you
    need this vendor's complete payment history -- one governed call instead
    of several round trips."""
    return await _govern("get_ap_payment_history", session_id, "", {
        "vendor_code": vendor_code, "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_gl_period_summary")
async def minierp_finance_get_gl_period_summary(
    session_id: SessionId, account_cd: str, fin_period_id: str = "",
    company_id: int | None = None, page: int = 1, page_size: int = 12,
    fetch_all: bool = False,
) -> str:
    """Get period-level GL balances (beginning balance, period debit/credit,
    YTD balance) for one account, optionally narrowed to one fiscal period
    ("YYYYMM"). fetch_all=true returns every period on file in one call
    instead of paging manually."""
    return await _govern("get_gl_period_summary", session_id, "", {
        "account_cd": account_cd, "fin_period_id": fin_period_id,
        "company_id": company_id, "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_invoice_line_items")
async def minierp_finance_get_invoice_line_items(
    session_id: SessionId, invoice_number: str, company_id: int | None = None, page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List the billed line items (product, quantity, price, sales rep)
    inside one AR invoice by invoice/reference number. fetch_all=true
    returns every line item in one call instead of paging manually."""
    return await _govern("get_invoice_line_items", session_id, "", {
        "invoice_number": invoice_number, "company_id": company_id, "page": page, "page_size": page_size,
        "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_bill_line_items")
async def minierp_finance_get_bill_line_items(
    session_id: SessionId, invoice_number: str, company_id: int | None = None, page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List the billed line items (product, quantity, cost, linked PO)
    inside one AP bill by invoice/reference number. fetch_all=true returns
    every line item in one call instead of paging manually."""
    return await _govern("get_bill_line_items", session_id, "", {
        "invoice_number": invoice_number, "company_id": company_id, "page": page, "page_size": page_size,
        "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_customer_invoice_history")
async def minierp_finance_get_customer_invoice_history(
    session_id: SessionId, customer_id: CustomerId = "", page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List a customer's AR invoices (reference number, order number,
    payment amount/method). fetch_all=true returns this customer's complete
    invoice history in one call instead of paging manually."""
    return await _govern("get_customer_invoice_history", session_id, customer_id, {
        "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="minierp_finance_get_item_movement_history")
async def minierp_finance_get_item_movement_history(
    session_id: SessionId, inventory_id: str, start_date: str = "", end_date: str = "",
    company_id: int | None = None, page: int = 1, page_size: int = 10,
    fetch_all: bool = False,
) -> str:
    """List inventory transaction history (receipts, issues, transfers) for
    one item, including lot/serial number and expiration date where tracked.
    This is transaction history, NOT live lot status or current
    stock-on-hand. fetch_all=true returns every transaction in range in one
    call instead of paging manually."""
    return await _govern("get_item_movement_history", session_id, "", {
        "inventory_id": inventory_id, "start_date": start_date, "end_date": end_date,
        "company_id": company_id, "page": page, "page_size": page_size, "fetch_all": fetch_all,
    })


@mcp.tool(name="submit_workflow_request")
async def submit_workflow_request(session_id: SessionId, description: str) -> str:
    """Log a request for a workflow/report capability that does not exist yet
    (e.g. a business rule needing logic no current tool/node can express).
    Surfaces to admins in the dashboard's Access Requests panel, under
    "Workflow requests", for future build-out. Not a data lookup -- does not
    go through _govern; mirrors backend/session.py's self-service request-access
    handler (same store, same record shape, kind="workflow" instead of "access")."""
    description = (description or "").strip()
    if not description:
        return '{"source": "workflow_request", "status": "error", "errorCode": "description_required"}'
    record = ctx.consumer_record_ctx.get()
    consumer_id = record.consumer_id if record else (ctx.consumer_ctx.get() or "unknown")
    username = record.name if record else (ctx.consumer_ctx.get() or "unknown")
    req = {
        "id": uuid.uuid4().hex[:12], "kind": "workflow", "consumer_id": consumer_id,
        "username": username, "justification": description, "status": "pending",
        "created_at": time.time(),
    }
    get_store().add_access_request(req)
    return (
        '{"source": "workflow_request", "status": "success", "requestId": "' + req["id"] + '"}'
    )


@mcp.tool(name="update_workflow_plan")
async def update_workflow_plan(
    session_id: SessionId,
    fields_discovered: list[dict] | None = None,
    modules_chosen: list[str] | None = None,
    draft_nodes: list[dict] | None = None,
    notes: str | None = None,
) -> str:
    """Update (or, if every argument is omitted, just re-read) this
    conversation's in-RAM workflow plan -- discovered tool fields/values,
    which tools/modules you've decided the workflow needs, and a draft node
    list you're building toward. RAM-only (workflow_scratchpad.py): never
    saved to chat history or any database, and gone once this conversation
    goes idle. Always returns the FULL current plan, merging in whatever you
    passed and leaving anything omitted unchanged, so you never have to hold
    the whole plan in your own head or re-derive it from earlier prose --
    call this with no arguments at any time to just re-read it.
    The returned plan also includes a `checks` list -- the structured result
    of validate_graph from your MOST RECENT propose_graph call on this graph
    (empty once a save is clean). You never set this yourself; it's recorded
    automatically so you can see what's still outstanding without calling
    propose_graph again just to check."""
    plan = workflow_scratchpad.update_plan(
        session_id, fields_discovered=fields_discovered, modules_chosen=modules_chosen,
        draft_nodes=draft_nodes, notes=notes,
    )
    return json.dumps({"source": "workflow_plan", "status": "success", "plan": plan})


def _flatten_output_schema(schema: dict) -> dict:
    """Reduce a full JSON Schema (as FastMCP derives it from a Pydantic return
    model) down to just the field names a workflow-graph binding needs:
    top-level properties, plus -- for any property that's an array of
    $ref'd objects -- that array's own item field names. Drops
    types/defaults/titles; this is a field-name reference, not a schema
    document."""
    defs = schema.get("$defs") or {}
    top_level = sorted((schema.get("properties") or {}).keys())
    array_fields: dict[str, list[str]] = {}
    for name, prop in (schema.get("properties") or {}).items():
        items = prop.get("items") if isinstance(prop, dict) else None
        ref = items.get("$ref") if isinstance(items, dict) else None
        if not ref:
            continue
        def_schema = defs.get(ref.rsplit("/", 1)[-1]) or {}
        array_fields[name] = sorted((def_schema.get("properties") or {}).keys())
    return {"topLevelFields": top_level, "arrayFields": array_fields}


@mcp.tool(name="get_field_catalog")
async def get_field_catalog(session_id: SessionId, tool_name: str) -> str:
    """Look up a tool's real output field names WITHOUT calling it -- call this
    BEFORE making any exploratory data call, to learn field shape for free
    instead of burning a real tool call (and its context/latency cost) just to
    see what fields exist.

    Only tools with a static schema come back status="success" (today: the
    miniERP finance-domain tools -- get_vendor_details, get_ap_invoices_due_soon,
    and the rest of that family). Everything else comes back
    status="unavailable" -- for those, fall back to a real call with a small
    page_size to sample real fields, the same way you always have.

    This never tells you real VALUES (e.g. what a status code actually
    contains, or whether a specific vendor/invoice exists) -- only field
    names/shape. You still need one real, small sample call to confirm values
    or that an identifier resolves to something real -- that's verification,
    not exploration, and should stay small (a handful of rows)."""
    name = (tool_name or "").strip()
    canonical = manifest.canonical(name)
    if canonical is None and manifest.get(name) is not None:
        canonical = name
    if canonical is None:
        return json.dumps({"source": "field_catalog", "status": "error", "errorCode": "unknown_tool",
                           "tool": tool_name, "message": f"{tool_name!r} is not a known tool name."})
    policy = manifest.get(canonical)
    schema = await schema_catalog.get_output_schema(policy.backend, canonical)
    if not schema:
        return json.dumps({
            "source": "field_catalog", "status": "unavailable", "tool": tool_name,
            "message": "No static schema available for this tool yet -- call it directly with a "
                       "small page_size to sample real fields instead.",
        })
    return json.dumps({
        "source": "field_catalog", "status": "success", "tool": tool_name,
        **_flatten_output_schema(schema),
    })


@mcp.tool(name="list_my_workflows")
async def list_my_workflows(session_id: SessionId) -> str:
    """List the calling user's own "My Workflow" graphs -- real graphIds,
    names, statuses (draft|active|disabled), and version numbers. Scoped to
    the calling principal only, same as the dropdown on their own "My
    Workflow" page -- never sees anyone else's.
    Call this FIRST whenever the user asks to change, fix, update, or add to
    a workflow they already have, to find its REAL graphId by matching on
    displayName -- never invent a graphId, and never reuse one from earlier
    in this same conversation without re-confirming it here first (it may
    have been deleted, or the name you remember may not match what's
    actually there). Once you have the right graphId, call get_my_workflow
    to read its current contents before changing anything."""
    record = ctx.consumer_record_ctx.get()
    if record is None:
        return json.dumps({"source": "workflow_graph", "status": "error", "errorCode": "no_identity",
                           "message": "No authenticated principal for this session."})
    graphs = workflow_graph_store.list_graphs(owner=record.name)
    return json.dumps({
        "source": "workflow_graph", "status": "success",
        "workflows": [{
            "graphId": g.graph_id, "displayName": g.display_name, "description": g.description,
            "status": g.status, "currentVersion": g.current_version, "publishedVersion": g.published_version,
            "updatedAt": g.updated_at,
        } for g in graphs],
    })


@mcp.tool(name="get_my_workflow")
async def get_my_workflow(session_id: SessionId, graph_id: str) -> str:
    """Read an existing "My Workflow" graph's CURRENT nodes/edges (its latest
    saved version, whether draft or already published) -- call this before
    editing one with propose_graph, so you know what's actually there right
    now. Never assume you already know a graph's contents from earlier in
    this conversation -- the human may have edited it by hand in the canvas
    since, and your own earlier edit (if any) may not be the latest version.
    graph_id must be a REAL id from list_my_workflows and must belong to you
    -- an unknown id or someone else's graph comes back as status="error"
    with no nodes/edges, never a peek at another user's workflow."""
    gid = (graph_id or "").strip()
    if not gid:
        return json.dumps({"source": "workflow_graph", "status": "error", "errorCode": "missing_identifier",
                           "message": "graph_id is required -- call list_my_workflows for a real one."})
    record = ctx.consumer_record_ctx.get()
    if record is None:
        return json.dumps({"source": "workflow_graph", "status": "error", "errorCode": "no_identity",
                           "message": "No authenticated principal for this session."})
    graph = workflow_graph_store.get_graph(gid)
    if graph is None or (graph.owner != record.name and record.role != "admin"):
        return json.dumps({"source": "workflow_graph", "status": "error", "errorCode": "not_found",
                           "message": f"No workflow {gid!r} owned by you was found. Call list_my_workflows for real ids."})
    version = graph.version_record()
    return json.dumps({
        "source": "workflow_graph", "status": "success", "graphId": graph.graph_id,
        "displayName": graph.display_name, "description": graph.description, "graphStatus": graph.status,
        "currentVersion": graph.current_version, "publishedVersion": graph.published_version,
        "nodes": [n.public_dict() for n in (version.nodes if version else [])],
        "edges": [e.public_dict() for e in (version.edges if version else [])],
    })


@mcp.tool(name="propose_graph")
async def propose_graph(
    session_id: SessionId,
    nodes: list[dict],
    edges: list[dict],
    display_name: str = "",
    description: str = "",
    notes: str = "",
    graph_id: str = "",
) -> str:
    """Save a DRAFT "My Workflow" graph for the user to review/edit/publish
    themselves in the canvas -- this never publishes or runs anything.

    TWO MODES, controlled by `graph_id`:
    - Omit it (default) -> creates a brand-new workflow, appearing as a new
      entry in the dropdown.
    - Pass a real graphId (from list_my_workflows) -> EDITS that existing
      workflow instead, by saving a new version onto it -- it does NOT
      create a duplicate. The graph must belong to you (same rule as
      get_my_workflow); an unrecognized or someone-else's graph_id fails
      with status="error" rather than silently creating a new graph anyway,
      so a typo in the id can never turn an intended edit into an unwanted
      duplicate. Editing NEVER touches what's currently live: if the
      workflow is already published/active, its running version keeps
      running unchanged until the user reviews and republishes the new one
      -- exactly as non-destructive as creating a fresh draft.
    display_name/description are used only when creating (a graph can't be
    renamed by versioning, today -- the hand-built editor has this same
    limit); they're ignored when `graph_id` is set.

    THE #1 WAY TO BREAK AN EDIT: a saved version is the COMPLETE graph, not a
    diff. When editing, `nodes`/`edges` must include EVERY node you want to
    keep -- not just the ones you're adding or changing -- or the ones you
    leave out are silently deleted. Always call get_my_workflow first, start
    from its exact nodes/edges, and only add/modify/remove what the user
    actually asked for; never reconstruct the existing steps from memory of
    what you proposed earlier in the conversation, since the human may have
    since changed them by hand.

    Goes through the exact same validation a human building one by hand
    would hit (workflow_graph_store.create_graph/add_graph_version): exactly
    one trigger node with no incoming edges, no cycles, every node reachable
    from the trigger, any send-risk tool_call (e.g. send_email_draft) must
    sit behind an approval_gate node, and you must actually be entitled to
    every tool used. A ValueError from any of that comes back as
    status="error" with the exact blocker message -- fix the graph and try
    again rather than guessing.
    `nodes`/`edges` use the same shape the canvas itself sends: each node is
    {"nodeId", "kind" (trigger|tool_call|approval_gate|llm_transform|filter --
    see the FILTER NODE section of your instructions for that fifth kind's own
    config/input_bindings shape, which differs from a tool_call's), "title",
    "tool" (canonical tool name, tool_call only), "config" (literal values),
    "inputBindings" (bind an arg to {"source":"trigger","path":...} or
    {"source":"node","node_id":...,"path":...})}; each edge is {"edgeId",
    "sourceNodeId", "targetNodeId"}. Any OTHER kind value is rejected outright
    at save time (never silently accepted) -- an unrecognized kind would
    otherwise be skipped entirely at run time with no error and no output,
    which is far worse than a loud rejection now.
    The trigger node's declared inputs go in ITS OWN "config": {"inputs":
    [{"name": "customer_id", "label": "Customer ID"}, ...]} -- "name" is the
    key other nodes bind to via {"source":"trigger","path":<name>} and MUST be
    a non-empty string on every entry; get this shape wrong (a missing
    "name", or inputs as bare strings) and the canvas fails to open the draft
    at all, so the user could never review or publish it.
    Every tool_call node's REQUIRED arguments (per that tool's own schema --
    the same one that made you call it in the first place) must be filled in
    with a real literal or binding before you call this, especially ones easy
    to overlook because they don't come from the data itself: a report tool's
    own title/name is the most common miss. Leaving one out DOES fail this
    call now (status="error", same as any other blocker) rather than saving a
    broken draft -- fix it and call propose_graph again. The outcome (clean or
    not) is also recorded in this conversation's scratchpad automatically, so
    a later update_workflow_plan (with every argument omitted) tells you
    what's still outstanding without needing to call propose_graph again just
    to check. Do NOT set "position" on any node -- omit that field entirely
    (when editing, this also means dropping whatever positions get_my_workflow
    showed you -- keep everything else about those nodes, just not position).
    You have no way to see the canvas this draft opens onto and cannot judge
    good x/y placement; leaving it out lets the canvas auto-arrange every
    node by dependency depth, which reliably reads as intentional, whereas
    e.g. every node at the same x with increasing y (an easy shape to default
    to when you're really just thinking of it as a numbered list) is kept
    AS GIVEN and renders as a squeezed, overlapping stack."""
    record = ctx.consumer_record_ctx.get()
    if record is None:
        return json.dumps({"source": "workflow_graph", "status": "error", "errorCode": "no_identity",
                           "message": "No authenticated principal for this session."})
    store = get_store()
    gid = (graph_id or "").strip()
    wire_nodes = [_graph_node_from_wire(n) for n in (nodes or [])]
    wire_edges = [_graph_edge_from_wire(e) for e in (edges or [])]
    # Record this attempt's structured validation outcome in the copilot's own
    # scratchpad (RAM-only, session-scoped -- see workflow_scratchpad.py), so a
    # later update_workflow_plan re-read shows what's still outstanding without
    # calling propose_graph again just to check. create_graph/add_graph_version
    # below re-run the same check as their own hard gate -- this call is purely
    # for the scratchpad copy, never the enforcement itself.
    owner_grant = resolve_grant(record, store.get_category, store.get_department)
    checks = workflow_graph_store.validate_graph(wire_nodes, wire_edges, owner_grant=owner_grant)
    workflow_scratchpad.update_plan(session_id, checks=[c.public_dict() for c in checks])
    if gid:
        existing = workflow_graph_store.get_graph(gid)
        if existing is None or (existing.owner != record.name and record.role != "admin"):
            return json.dumps({
                "source": "workflow_graph", "status": "error", "errorCode": "not_found",
                "message": f"No workflow {gid!r} owned by you was found -- call list_my_workflows for a "
                           "real id, or omit graph_id entirely to create a new draft instead.",
            })
        try:
            updated = workflow_graph_store.add_graph_version(
                gid, nodes=wire_nodes, edges=wire_edges,
                owner_record=record, get_category=store.get_category, get_department=store.get_department,
                created_by=record.name, notes=notes,
            )
        except ValueError as exc:
            return json.dumps({"source": "workflow_graph", "status": "error", "errorCode": "invalid_graph", "message": str(exc)})
        return json.dumps({
            "source": "workflow_graph", "status": "success", "graphId": updated.graph_id,
            "displayName": updated.display_name, "graphStatus": updated.status, "edited": True,
            "newVersion": updated.current_version, "publishedVersion": updated.published_version,
        })
    try:
        new_graph = workflow_graph_store.create_graph(
            display_name=display_name or "My Workflow", description=description,
            owner_record=record, get_category=store.get_category, get_department=store.get_department,
            nodes=wire_nodes, edges=wire_edges,
            created_by=record.name, notes=notes,
            reserved_ids=set(workflows.TEMPLATES.keys()),
        )
    except ValueError as exc:
        return json.dumps({"source": "workflow_graph", "status": "error", "errorCode": "invalid_graph", "message": str(exc)})
    return json.dumps({
        "source": "workflow_graph", "status": "success", "graphId": new_graph.graph_id,
        "displayName": new_graph.display_name, "graphStatus": new_graph.status, "edited": False,
    })


# ── Math / utility tools ───────────────────────────────────────────────────────
# Meta tools, not governed business data -- same reasoning as submit_workflow_request/
# propose_graph above: no manifest entry (so no ToolPolicy.backend to gate on),
# available on BOTH Home chat and the "My Workflow" copilot. They operate only on
# numbers the caller already has (from an earlier governed tool call), so there is
# nothing here to redact or audit. Exist because a small local model is not
# reliable at exact arithmetic across many values or period-over-period math --
# see orchestrator.py's WORKFLOW_COPILOT_SYSTEM_PROMPT rule 4, same principle.
#
# A small family of narrow tools, not one with an operation switch: picking the
# right tool by name from a list is an easier, more reliable call for a
# tool-calling model than picking the right subset of a shared parameter set
# once it's chosen a mode -- every governed tool in this file already follows
# that one-job-per-tool shape. A grouped result (group_stats) is a genuinely
# different response SHAPE than a flat one (compute_stats), not just a
# different scope of the same shape (the way page/page_size are) -- that's
# the test for "new tool" vs. "new field on an existing tool" applied here.

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MAX_EXPRESSION_LEN = 200
_MAX_EXPONENT = 12


def _safe_eval(node):
    """Evaluate an already-parsed arithmetic AST node. Only numeric literals,
    +-*/// % **, unary +/-, and parentheses (implicit in AST structure) are
    reachable -- no Name/Call/Attribute/Subscript node is ever handled, so
    there is no path to a variable, a function, or any other lookup. Never use
    eval()/exec() for this: this walks a restricted grammar instead of running
    arbitrary Python."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"unsupported literal {node.value!r}")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.BinOp):
        if type(node.op) is ast.Pow:
            base, exp = _safe_eval(node.left), _safe_eval(node.right)
            if abs(exp) > _MAX_EXPONENT:
                raise ValueError(f"exponent magnitude over the limit ({_MAX_EXPONENT})")
            return base ** exp
        if type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    raise ValueError(f"unsupported expression near {ast.dump(node)}")


@mcp.tool(name="calculate")
async def calculate(session_id: SessionId, expression: str) -> str:
    """Evaluate ONE numeric arithmetic expression exactly, e.g.
    "(45231.12 - 38004.50) / 38004.50" for a margin or growth ratio. Supports
    + - * / // % ** and parentheses over numeric literals only -- no
    variables, function calls, or names of any kind. Use this instead of
    computing a ratio, percentage, or multi-step formula by hand; small
    models are not reliable at exact arithmetic."""
    expr = (expression or "").strip()
    if not expr:
        return json.dumps({"source": "compute", "status": "empty_input", "message": "expression is empty."})
    if len(expr) > _MAX_EXPRESSION_LEN:
        return json.dumps({"source": "compute", "status": "error", "errorCode": "expression_too_long",
                           "message": f"expression is over the {_MAX_EXPRESSION_LEN}-character limit."})
    try:
        value = _safe_eval(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        return json.dumps({"source": "compute", "status": "error", "errorCode": "division_by_zero",
                           "message": f"Division by zero in {expr!r}."})
    except (SyntaxError, ValueError, TypeError) as exc:
        return json.dumps({"source": "compute", "status": "error", "errorCode": "invalid_expression",
                           "message": f"Could not evaluate {expr!r} ({exc}). Only numbers, "
                                      "+ - * / // % ** and parentheses are allowed."})
    if isinstance(value, complex) or not isinstance(value, (int, float)):
        return json.dumps({"source": "compute", "status": "error", "errorCode": "non_real_result",
                           "message": f"{expr!r} did not produce a real number."})
    return json.dumps({"source": "compute", "status": "ok", "expression": expr, "result": value})


@mcp.tool(name="compute_stats")
async def compute_stats(session_id: SessionId, values: list) -> str:
    """Compute descriptive statistics over a list of numbers you already have
    (e.g. every row's amount from a paginated tool's results): count, sum,
    mean, min, max, range, median, sample/population standard deviation, and
    sample/population variance -- all in one call. Use this instead of adding
    up or averaging a list of numbers yourself; small models are not reliable
    at exact arithmetic across many values."""
    if not values:
        return json.dumps({"source": "compute", "status": "empty_input", "message": "values is empty -- nothing to compute."})
    nums, bad = [], []
    for v in values:
        try:
            if isinstance(v, bool):
                raise TypeError
            f = float(v)
            if not math.isfinite(f):  # "inf"/"nan" parse fine but would silently poison every stat below
                raise ValueError
            nums.append(f)
        except (TypeError, ValueError):
            bad.append(v)
    if bad:
        # Reported, never silently coerced to 0 -- coercing a bad value would
        # quietly corrupt the exact answer this tool exists to guarantee.
        return json.dumps({
            "source": "compute", "status": "invalid_input",
            "message": f"{len(bad)} of {len(values)} values were not numbers -- fix or remove them and retry.",
            "invalidValues": [repr(b) for b in bad[:20]], "truncated": len(bad) > 20,
        })
    n = len(nums)
    total = sum(nums)
    return json.dumps({
        "source": "compute", "status": "ok", "count": n,
        "sum": total, "mean": total / n,
        "min": min(nums), "max": max(nums), "range": max(nums) - min(nums),
        "median": statistics.median(nums),
        "sampleStdev": statistics.stdev(nums) if n > 1 else None,
        "populationStdev": statistics.pstdev(nums),
        "sampleVariance": statistics.variance(nums) if n > 1 else None,
        "populationVariance": statistics.pvariance(nums),
    })


@mcp.tool(name="percent_change")
async def percent_change(session_id: SessionId, from_value: float | int | str, to_value: float | int | str) -> str:
    """Compute the change from one number to another -- e.g. last quarter's
    revenue vs. this quarter's -- as both an absolute and a percentage
    change. Use this instead of computing period-over-period growth by hand:
    which value is the base and the sign of a decline are easy to get
    backwards."""
    # from_value/to_value take float|int|str (not a bare float) so a genuinely
    # non-numeric value fails HERE with a clean status, not as an uncaught
    # pydantic ValidationError from FastMCP's own pre-validation -- confirmed
    # by _smoke/test_math_tools.py that a bare `float` annotation lets that
    # propagate as an unhandled ToolError, which orchestrator.execute_tool has
    # no try/except around (same root cause compute_stats' list case hit).
    try:
        from_value, to_value = float(from_value), float(to_value)
    except (TypeError, ValueError):
        return json.dumps({"source": "compute", "status": "invalid_input",
                           "message": f"fromValue={from_value!r} / toValue={to_value!r} -- both must be numbers."})
    if not (math.isfinite(from_value) and math.isfinite(to_value)):
        return json.dumps({"source": "compute", "status": "invalid_input",
                           "message": "fromValue/toValue must be finite numbers (not inf/nan)."})
    absolute = to_value - from_value
    if from_value == 0:
        return json.dumps({
            "source": "compute", "status": "ok", "fromValue": from_value, "toValue": to_value,
            "absoluteChange": absolute, "percentChange": None,
            "message": "fromValue is 0 -- percentChange is undefined (would divide by zero); absoluteChange is still valid.",
        })
    return json.dumps({
        "source": "compute", "status": "ok", "fromValue": from_value, "toValue": to_value,
        "absoluteChange": absolute, "percentChange": (absolute / abs(from_value)) * 100.0,
    })


@mcp.tool(name="group_stats")
async def group_stats(session_id: SessionId, rows: list) -> str:
    """Per-group count/sum/mean/min/max over rows you already have from an
    earlier tool call -- e.g. one customer's orders grouped by status, or a
    handful of subtotals grouped by category. Each row must be
    {"group": <label>, "value": <number>}. Use this instead of bucketing and
    adding these up yourself.

    NOT for company-wide/unbounded data: if the source itself is too large
    for one paginated tool call, that's a case for a purpose-built bulk
    aggregation tool, not for handing hundreds of rows to this one."""
    if not rows:
        return json.dumps({"source": "compute", "status": "empty_input", "message": "rows is empty -- nothing to compute."})
    buckets: dict = {}
    bad = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or "group" not in row or "value" not in row:
            bad.append({"index": i, "row": row, "reason": 'each row must be {"group": <label>, "value": <number>}'})
            continue
        val = row["value"]
        try:
            if isinstance(val, bool):
                raise TypeError
            v = float(val)
            if not math.isfinite(v):
                raise ValueError
        except (TypeError, ValueError):
            bad.append({"index": i, "row": row, "reason": "value is not a finite number"})
            continue
        buckets.setdefault(str(row["group"]), []).append(v)
    if bad:
        # Reported, never silently dropped -- a silently skipped bad row would
        # quietly change the grand total this tool exists to get exactly right.
        return json.dumps({
            "source": "compute", "status": "invalid_input",
            "message": f"{len(bad)} of {len(rows)} rows were malformed -- fix or remove them and retry.",
            "invalidRows": bad[:20], "truncated": len(bad) > 20,
        })
    groups = {
        g: {"count": len(vs), "sum": sum(vs), "mean": sum(vs) / len(vs), "min": min(vs), "max": max(vs)}
        for g, vs in buckets.items()
    }
    return json.dumps({
        "source": "compute", "status": "ok", "groups": groups,
        "groupCount": len(groups), "rowCount": len(rows),
        "grandTotal": sum(v for vs in buckets.values() for v in vs),
    })


# ── Startup self-check: gateway tools <-> manifest.py must agree ──────────────
# A CODE-LEVEL gate, not a test someone has to remember to run: this executes on
# every import of this module (dev, prod, and _smoke/test_tool_registration_
# consistency.py itself), so a drifted registration fails the moment the
# gateway tries to start, not "whenever someone next runs the smoke suite."
#
# Only covers this process's own two legs (gateway tools <-> manifest
# entries) -- it cannot also confirm the physical backend actually implements
# the canonical tool without a live network call to that backend, which would
# make gateway startup depend on backend liveness/ordering. That third leg
# (backend <-> manifest) stays a smoke-test/CI concern:
# _smoke/test_tool_registration_consistency.py.
#
# Any gateway tool not backed by a manifest.py ToolPolicy must be listed here,
# as a deliberate, visible decision -- never a silent exemption.
_UNGOVERNED_GATEWAY_TOOLS = {
    # Workflow-authoring meta tools -- scratchpad reads/writes and draft-graph
    # proposals, not data lookups; see each one's own docstring above.
    "submit_workflow_request", "update_workflow_plan", "list_my_workflows",
    "get_my_workflow", "propose_graph", "get_field_catalog",
    # "Data Aggregation" utility tools (STAGE2_PLAN.md SS10.2): pure local
    # computation over values/rows the caller already has, no backend call, no
    # _govern(...), nothing to redact or authorize.
    "calculate", "compute_stats", "percent_change", "group_stats",
}


def _check_tool_registration() -> None:
    registered = {t.name: (t.description or "") for t in mcp._tool_manager.list_tools()}
    errors: list[str] = []
    for canonical in manifest.TOOL_POLICIES:
        expected_name = manifest.namespaced(canonical)
        if expected_name not in registered:
            errors.append(
                f"manifest tool {canonical!r} has no gateway wrapper ({expected_name}) "
                "-- no LLM caller (Home chat, workflow copilot, or any MCP client) can reach it"
            )
    for name in registered:
        if name in _UNGOVERNED_GATEWAY_TOOLS:
            continue
        if manifest.canonical(name) is None:
            errors.append(
                f"gateway tool {name!r} has no policy/manifest.py ToolPolicy and isn't in "
                "_UNGOVERNED_GATEWAY_TOOLS -- add a ToolPolicy, or add it to that set if it's "
                "deliberately ungoverned (pure compute / workflow-authoring meta tool)"
            )
    if errors:
        raise RuntimeError(
            "gateway/app.py tool registration is out of sync with policy/manifest.py:\n  "
            + "\n  ".join(errors)
        )


_check_tool_registration()


app = mcp.streamable_http_app()

# FastMCP's own lifespan (app.router.lifespan_context) runs its stateless-http
# session manager; this Starlette version dropped add_event_handler("startup"), so
# to also start the idle sweeps we wrap that lifespan rather than replace it --
# the session manager still runs, we just additionally spin up the sweep tasks for
# the life of the process and cancel them on shutdown.
_mcp_lifespan = app.router.lifespan_context


async def _warm_schema_catalog() -> None:
    """Pre-populate schema_catalog's cache for every governed tool at boot,
    so a conversation's first get_field_catalog call is an in-memory hit
    instead of paying a live schema fetch on someone's first turn. Each
    lookup is independent and swallowed on failure -- one slow/down backend
    at boot must not block startup or crash the gateway."""
    async def _warm_one(canonical: str) -> None:
        policy = manifest.get(canonical)
        try:
            await schema_catalog.get_output_schema(policy.backend, canonical)
        except Exception:
            pass

    await asyncio.gather(*(_warm_one(canonical) for canonical in manifest.TOOL_POLICIES))


@asynccontextmanager
async def _lifespan_with_sweeps(asgi_app):
    chat_task = asyncio.create_task(_chat_sweep_loop())
    scratchpad_task = asyncio.create_task(workflow_scratchpad.scratchpad_sweep_loop())
    warm_task = asyncio.create_task(_warm_schema_catalog())
    try:
        async with _mcp_lifespan(asgi_app) as state:
            yield state
    finally:
        chat_task.cancel()
        scratchpad_task.cancel()
        warm_task.cancel()


app.router.lifespan_context = _lifespan_with_sweeps

# Every HTTP route lives in the backend package; see backend/__init__.py for the
# table and the /backend-vs-legacy prefix rules.
backend.register(app)

app.add_middleware(edge.EdgeMiddleware)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("GATEWAY_PORT") or "8020")
    print(f"[gateway] Starting Governance Gateway on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
