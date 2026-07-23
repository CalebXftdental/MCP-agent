"""mcp-minierp-orders -- backend MCP server for the sales-order domain
(SOOrder/SOLine/InventoryItem + the BAccount resolution every account-scoped
tool here needs).

Thin backend in the federating-governance topology (see
governence_agent/design_plan.md): exposes read tools returning structured
JSON, holds only its own miniERP credentials, does NO auth/rate/redaction --
that's the Governance Gateway's job.

This backend is shared across departments (finance, sales, customer service
all grant tools from it in governance_core/policy/departments.py) -- it is
NOT any one team's backend, it's the sales-order data domain.

Run:
    pip install -r requirements.txt
    python app.py            # serves MCP on PORT (default 8021) at /mcp
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(os.getenv("MINIERP_ORDERS_ENV_FILE") or ".env.local")

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from sqlagent.index import (
    _company_ids as resolve_company_ids,
    _gql_baccount_ids_for_acct_cd as gql_baccount_ids_for_acct_cd,
    _gql_customer_order_total as gql_customer_order_total,
    _gql_orders_for_customer as gql_orders_for_customer,
    _gql_product_details_in_order as gql_product_details_in_order,
    _json_tool_result as json_tool_result,
)
from sqlagent.structured import order_details_struct


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("MINIERP_ORDERS_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-mcp-minierp-orders",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in _ALLOWED_HOSTS if ":" not in h and not h.startswith(("localhost", "127."))]
        + ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)

_MISSING_CUSTOMER = json_tool_result(
    status="missing_identifier", intent="account_scoped_lookup",
    message="A customer_id is required for this lookup.", missingFields=["customerId"],
)


@mcp.tool()
async def get_customer_orders(
    customer_id: str,
    start_date: str = "",
    end_date: str = "",
    order_status: str = "",
    min_total: float | None = None,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List a customer's sales orders, optionally filtered by date range, status, or minimum total."""
    if not customer_id.strip():
        return _MISSING_CUSTOMER
    return await gql_orders_for_customer(
        customer_id.strip(), resolve_company_ids(None),
        start_date=start_date or None, end_date=end_date or None,
        order_status=order_status or None, min_total=min_total,
        page=page, page_size=page_size,
    )


@mcp.tool()
async def get_customer_order_total(customer_id: str, start_date: str = "", end_date: str = "") -> str:
    """Calculate a customer's total spend across completed orders, optionally within a date range."""
    if not customer_id.strip():
        return _MISSING_CUSTOMER
    return await gql_customer_order_total(
        customer_id.strip(), resolve_company_ids(None), start_date or None, end_date or None
    )


@mcp.tool()
async def get_order_details(order_number: str, company_id: int | None = None) -> str:
    """Get header-level details (status, total, date) for one sales order by order number."""
    if not order_number.strip():
        return json_tool_result(
            status="missing_identifier", intent="order_details",
            message="A order_number is required.", missingFields=["order_number"],
        )
    return await order_details_struct(order_number.strip(), company_id)


@mcp.tool()
async def get_product_details_in_order(
    order_number: str, company_id: int | None = None, page: int = 1, page_size: int = 10
) -> str:
    """List the line items (products, quantities, prices) inside one sales order."""
    if not order_number.strip():
        return json_tool_result(
            status="missing_identifier", intent="product_details_in_order",
            message="A order_number is required.", missingFields=["order_number"],
        )
    return await gql_product_details_in_order(
        order_number.strip(), company_id=company_id, page=page, page_size=page_size
    )


async def _health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-minierp-orders"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("MINIERP_ORDERS_PORT") or "8021")
    print(f"[mcp-minierp-orders] Starting MCP server on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
