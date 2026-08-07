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

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

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
from policy import manifest

import backend
from backend.chat import _chat_sweep_loop
from govern import _govern
from mcp_server import mcp


# ── The governed tool plane ───────────────────────────────────────────────────
# Explicit signatures give callers a proper input schema. Each tool just marshals
# its args and defers all policy to _govern (govern.py). Tool names are namespaced
# "<backend>_<canonical>" (policy.manifest.namespaced) -- the backend prefix here
# MUST match each tool's ToolPolicy.backend in policy/manifest.py.

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


@mcp.tool(name="minierp_finance_get_sales_price")
async def minierp_finance_get_sales_price(
    session_id: str,
    inventory_id: str,
    cust_price_class_id: str = "",
    customer_id: str = "",
    company_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List sales price records (price class, currency, UOM, break quantity) for one inventory item, optionally narrowed to a price class or customer."""
    return await _govern("get_sales_price", session_id, "", {
        "inventory_id": inventory_id, "cust_price_class_id": cust_price_class_id,
        "customer_id": customer_id, "company_id": company_id,
        "page": page, "page_size": page_size,
    })


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

# Every HTTP route lives in the backend package; see backend/__init__.py for the
# table and the /backend-vs-legacy prefix rules.
backend.register(app)

app.add_middleware(edge.EdgeMiddleware)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("GATEWAY_PORT") or "8020")
    print(f"[gateway] Starting Governance Gateway on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
