"""Governance Agent -- MCP server holding all miniERP + HubSpot credentials.

This is the only process that ever imports sqlagent/hubspot or reads
MINIERP_*/HUBSPOT_* env vars. Any consumer that needs ERP or CRM data (the
chatbot orchestrator today, an email bot or other internal agents later)
connects as an MCP client over Streamable HTTP and calls one of the typed
tools below -- nobody gets a raw SQL/GraphQL passthrough.

Run:
    pip install -r requirements.txt
    python app.py            # serves on GOVERNANCE_PORT (default 8020)

This file is Layer 2 -- the Governed MCP Server: approved tool registry,
typed input schemas (via @mcp.tool()'s Pydantic-based schema generation),
tool-level scope checks, output redaction, and audit logging. It answers
"is this specific tool call allowed, and what data can safely be returned?"
Layer 1 -- the Governance Edge (agent authentication via per-consumer API
keys, rate limits/quotas, IP allowlist, request size limits, request-id,
traffic logging for denied/throttled requests) lives in edge.py and runs as
an outer ASGI middleware in front of everything below. See edge.py's module
docstring for the full rationale and env var configuration.

Every tool call is audit-logged (audit.py) and account-scoped tools resolve
their customer_id from session_store.py (server-side), never from a
caller-supplied argument alone -- see session_store.py for the current trust
model and its known relaxation.

Monitoring: open /dashboard in a browser and paste in one of the
GOVERNANCE_KEY_* values to watch live tool calls (consumer, caller IP/user-
agent, tool, args, session, status, latency), active session-to-account
scoping, per-consumer rate-limit usage, and -- separately, since it's the
signal that matters most for "is someone hitting this service who shouldn't
be" -- requests Layer 1 rejected before they ever reached a tool. Backed by
audit.py's in-memory ring buffer: fine for a POC, not a durable audit trail.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("GOVERNANCE_ENV_FILE") or ".env.local")

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, JSONResponse

import audit
import edge
import request_context as ctx
import session_store
from hubspot.hubspot_query import lookup_sales_rep, lookup_sales_rep_by_name
from sqlagent.index import (
    _company_ids as resolve_company_ids,
    _gql_addresses as gql_addresses,
    _gql_baccount_ids_for_acct_cd as gql_baccount_ids_for_acct_cd,
    _gql_contacts as gql_contacts,
    _gql_customer_order_total as gql_customer_order_total,
    _gql_order_details as gql_order_details,
    _gql_orders_for_customer as gql_orders_for_customer,
    _gql_product_details_in_order as gql_product_details_in_order,
    _gql_shipping_by_order as gql_shipping_by_order,
    _gql_shipping_by_shipment as gql_shipping_by_shipment,
    _json_tool_result as json_tool_result,
)

_CUSTOMER_ID_REQUIRED = json_tool_result(
    status="missing_identifier",
    intent="account_scoped_lookup",
    message="A verified customer ID is required before I can look up account data.",
    missingFields=["customerId"],
)
_SHIPMENT_CUSTOMER_ID_REQUIRED = json_tool_result(
    status="missing_identifier",
    intent="shipment_tracking",
    message="A verified customer ID is required before I can look up a shipment.",
    missingFields=["customerId"],
)

def _allowed_hosts() -> list[str]:
    """localhost always allowed (local dev); production hosts come from env.

    The MCP SDK's Streamable HTTP transport validates the incoming Host/Origin
    header against this list to prevent DNS rebinding attacks, and defaults to
    rejecting anything not on it -- including this service's own real Azure
    hostname once deployed. Without this configured, every request is silently
    rejected at the transport layer (a 421, logged by the `mcp` package itself
    as "Invalid Host header") before it ever reaches EdgeMiddleware or a tool,
    so it won't show up in this service's own audit dashboard at all -- that
    silence is the symptom, not a sign the request never arrived.

    The SDK's own matching (transport_security.py) only treats a "host:*"
    entry as a wildcard if the incoming Host header actually has a ":<port>"
    suffix; Azure serves HTTPS on the default port, so the header is the bare
    hostname with no port at all. Both the bare and ":*" forms are listed for
    each host so it matches regardless of whether a port shows up.
    """
    raw = (os.getenv("GOVERNANCE_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if not h:
            continue
        hosts.append(h)
        hosts.append(f"{h}:*")
    return hosts


def _allowed_origins(hosts: list[str]) -> list[str]:
    origins = ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"]
    origins.extend(
        f"https://{h}" for h in hosts
        if not h.startswith(("localhost", "127.0.0.1")) and not h.endswith(":*")
    )
    return origins


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-governance-agent",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=_allowed_origins(_ALLOWED_HOSTS),
    ),
)


def _denied(tool_name: str, session_id: str, reason: str) -> None:
    audit.log_denied(
        tool=tool_name, session_id=session_id, reason=reason,
        consumer=ctx.consumer_ctx.get(), client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(),
    )


def _resolve_scope(session_id: str, customer_id: str | None) -> str | None:
    """Resolve the account this call is scoped to and remember it for the session."""
    supplied = (customer_id or "").strip()
    if supplied:
        session_store.note_customer_id(session_id, supplied, consumer=ctx.consumer_ctx.get())
        return supplied
    return session_store.customer_id_for_session(session_id)


async def _run(tool_name: str, session_id: str, coro, *, args: dict | None = None) -> str:
    start = time.time()
    consumer = ctx.consumer_ctx.get()
    session_store.touch(session_id, consumer=consumer)
    args_summary = audit.summarize_args(args or {})
    try:
        result = await coro
        audit.log_call(tool=tool_name, session_id=session_id, status="ok", consumer=consumer,
                        client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(), args_summary=args_summary,
                        latency_ms=(time.time() - start) * 1000)
        return result
    except Exception as exc:
        audit.log_call(tool=tool_name, session_id=session_id, status="error", consumer=consumer,
                        client_ip=ctx.ip_ctx.get(), user_agent=ctx.ua_ctx.get(), args_summary=args_summary,
                        latency_ms=(time.time() - start) * 1000, detail=str(exc))
        return json_tool_result(
            status="error",
            intent=tool_name,
            errorCode=type(exc).__name__,
            message="The lookup could not be completed.",
        )


# ── miniERP tools ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_customer_orders(
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
    call_args = {
        "customer_id": customer_id, "start_date": start_date, "end_date": end_date,
        "order_status": order_status, "min_total": min_total, "page": page, "page_size": page_size,
    }
    acct_cd = _resolve_scope(session_id, customer_id)
    if not acct_cd:
        _denied("get_customer_orders", session_id, "missing_customer_id")
        return _CUSTOMER_ID_REQUIRED
    return await _run(
        "get_customer_orders", session_id,
        gql_orders_for_customer(
            acct_cd, resolve_company_ids(None),
            start_date=start_date or None, end_date=end_date or None,
            order_status=order_status or None, min_total=min_total,
            page=page, page_size=page_size,
        ),
        args=call_args,
    )


@mcp.tool()
async def get_order_details(session_id: str, order_number: str, company_id: int | None = None) -> str:
    """Get header-level details (status, total, date) for one sales order by order number."""
    if not order_number:
        return "Missing required order_number. Ask the customer for the order number before looking up order details."
    return await _run(
        "get_order_details", session_id, gql_order_details(order_number, company_id),
        args={"order_number": order_number, "company_id": company_id},
    )


@mcp.tool()
async def get_product_details_in_order(
    session_id: str,
    order_number: str,
    company_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List the line items (products, quantities, prices) inside one sales order."""
    if not order_number:
        _denied("get_product_details_in_order", session_id, "missing_order_number")
        return json_tool_result(
            status="missing_identifier",
            intent="product_details_in_order",
            message="Missing required order_number. Ask the customer for the order number before looking up product details in an order.",
            missingFields=["order_number"],
        )
    return await _run(
        "get_product_details_in_order", session_id,
        gql_product_details_in_order(order_number, company_id=company_id, page=page, page_size=page_size),
        args={"order_number": order_number, "company_id": company_id, "page": page, "page_size": page_size},
    )


@mcp.tool()
async def get_shipping_by_order(session_id: str, order_number: str, company_id: int | None = None) -> str:
    """Get shipment/tracking/invoice numbers linked to a sales order, looked up by order number."""
    if not order_number:
        return "Missing required order_number. Ask the customer for the order number before looking up shipment tracking."
    return await _run(
        "get_shipping_by_order", session_id, gql_shipping_by_order(order_number, company_id),
        args={"order_number": order_number, "company_id": company_id},
    )


@mcp.tool()
async def get_shipping_by_shipment(session_id: str, shipment_number: str, customer_id: str = "") -> str:
    """Get tracking/invoice details for a shipment, looked up by shipment number.

    Requires a customer_id in scope (this session or supplied here) -- the
    shipment is only released if it is linked to that customer's orders."""
    if not shipment_number:
        return "Missing required shipment_number. Ask the customer for the shipment number before looking up tracking."
    acct_cd = _resolve_scope(session_id, customer_id)
    if not acct_cd:
        _denied("get_shipping_by_shipment", session_id, "missing_customer_id")
        return _SHIPMENT_CUSTOMER_ID_REQUIRED
    company_ids = resolve_company_ids(None)

    async def _lookup() -> str:
        owner_baccount_ids = await gql_baccount_ids_for_acct_cd(acct_cd, company_ids)
        return await gql_shipping_by_shipment(shipment_number, owner_baccount_ids=owner_baccount_ids)

    return await _run(
        "get_shipping_by_shipment", session_id, _lookup(),
        args={"shipment_number": shipment_number, "customer_id": customer_id},
    )


@mcp.tool()
async def get_customer_order_total(
    session_id: str,
    customer_id: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """Calculate a customer's total spend across completed orders, optionally within a date range."""
    acct_cd = _resolve_scope(session_id, customer_id)
    if not acct_cd:
        _denied("get_customer_order_total", session_id, "missing_customer_id")
        return _CUSTOMER_ID_REQUIRED
    return await _run(
        "get_customer_order_total", session_id,
        gql_customer_order_total(acct_cd, resolve_company_ids(None), start_date or None, end_date or None),
        args={"customer_id": customer_id, "start_date": start_date, "end_date": end_date},
    )


@mcp.tool()
async def get_contacts(session_id: str, customer_id: str = "", page: int = 1, page_size: int = 10) -> str:
    """List contacts (name, role, phone, email) on a customer's account."""
    acct_cd = _resolve_scope(session_id, customer_id)
    if not acct_cd:
        _denied("get_contacts", session_id, "missing_customer_id")
        return _CUSTOMER_ID_REQUIRED
    return await _run(
        "get_contacts", session_id, gql_contacts(acct_cd, page=page, page_size=page_size),
        args={"customer_id": customer_id, "page": page, "page_size": page_size},
    )


@mcp.tool()
async def get_addresses(session_id: str, customer_id: str = "", page: int = 1, page_size: int = 10) -> str:
    """List addresses on file for a customer's account."""
    acct_cd = _resolve_scope(session_id, customer_id)
    if not acct_cd:
        _denied("get_addresses", session_id, "missing_customer_id")
        return _CUSTOMER_ID_REQUIRED
    return await _run(
        "get_addresses", session_id, gql_addresses(acct_cd, page=page, page_size=page_size),
        args={"customer_id": customer_id, "page": page, "page_size": page_size},
    )


# ── HubSpot tool ──────────────────────────────────────────────────────────────

@mcp.tool()
async def get_sales_rep(
    session_id: str,
    customer_id: str = "",
    salesperson_name: str = "",
    wants_email: bool = False,
) -> str:
    """Look up a sales rep and their contact details.

    Provide ONE of customer_id (returns the rep assigned to that account) or
    salesperson_name (fuzzy-matches a rep by name). The rep's email is only
    released once this session has an account in scope (see customer_id)."""

    async def _lookup() -> str:
        if salesperson_name.strip() and not customer_id.strip():
            result = await lookup_sales_rep_by_name(salesperson_name)
        else:
            result = await lookup_sales_rep(customer_id)
            if result.get("status") == "success" and customer_id.strip():
                session_store.note_customer_id(session_id, customer_id.strip(), consumer=ctx.consumer_ctx.get())
        result = _apply_email_policy(result, wants_email=wants_email, verified=session_store.is_verified(session_id))
        return json.dumps(result)

    return await _run(
        "get_sales_rep", session_id, _lookup(),
        args={"customer_id": customer_id, "salesperson_name": salesperson_name, "wants_email": wants_email},
    )


def _apply_email_policy(result: dict, *, wants_email: bool, verified: bool) -> dict:
    has_email = bool(result.get("email"))
    if not wants_email:
        result.pop("email", None)
        if has_email and not result.get("phone"):
            result["email_available"] = True
        return result
    if verified:
        return result
    result.pop("email", None)
    if has_email:
        result["email_requires_verification"] = True
    return result


# ── ASGI app: MCP handler + Layer 1 edge + monitoring ────────────────────────

_DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")


async def _health(_request):
    return JSONResponse({"status": "ok"})


async def _dashboard(_request):
    return HTMLResponse(_DASHBOARD_HTML)


async def _admin_calls(request):
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    return JSONResponse({"calls": audit.recent(limit)})


async def _admin_sessions(request):
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    return JSONResponse({"sessions": session_store.snapshot(limit)})


async def _admin_rate_limits(_request):
    return JSONResponse({"rate_limits": edge.rate_limit_snapshot()})


app = mcp.streamable_http_app()
app.add_route("/health", _health)
app.add_route("/dashboard", _dashboard)
app.add_route("/admin/calls", _admin_calls)
app.add_route("/admin/sessions", _admin_sessions)
app.add_route("/admin/rate-limits", _admin_rate_limits)
app.add_middleware(edge.EdgeMiddleware)


if __name__ == "__main__":
    # Azure App Service assigns the public listener port via PORT/WEBSITES_PORT
    # (same convention as gateway/app.py in the chatbot service) -- GOVERNANCE_PORT
    # is only the local-dev fallback.
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("GOVERNANCE_PORT") or "8020")
    print(f"[governance] Starting MCP server on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
