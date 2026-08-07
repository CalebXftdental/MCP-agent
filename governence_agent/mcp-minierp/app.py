"""mcp-minierp -- the single backend MCP server for all miniERP domains.

Consolidation of the four former domain backends (mcp-minierp-orders /
-accounts / -finance / -shipments) into one deployable. The domains stay
separated as query modules under sqlagent/<domain>/, but there is now one
FastMCP server, one process, and one shared data-access client
(minierp_core.graphql_client) instead of four byte-identical copies.

This changes nothing the gateway or the LLM sees: the gateway is still the only
client of this server, and each tool keeps its canonical name (get_customer_orders,
get_contacts, get_invoice_details, ...) and behavior. The gateway's per-tool
`backend` tag in policy/manifest.py continues to route/authorize by domain, so
the category model is unchanged -- the tags are now logical, not per-process.

Thin backend in the federating-governance topology (design_plan.md): read tools
returning structured JSON, holds only its own miniERP credentials, does NO
auth/rate/redaction -- that's the Governance Gateway's job.

Run:
    pip install -r requirements.txt
    python app.py            # serves MCP on PORT (default 8021) at /mcp
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("MINIERP_ENV_FILE") or ".env.local")

# minierp_core lives at the repo root; put it on sys.path so the domain modules
# (sqlagent/<domain>/index.py) can `import minierp_core` when run as `python app.py`.
_REPO_ROOT = str(Path(__file__).parent.parent.resolve())
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

# ── Domain query modules (verbatim from the former per-domain backends) ───────
from sqlagent.orders.index import (
    _company_ids as resolve_company_ids,
    _gql_customer_order_total as gql_customer_order_total,
    _gql_orders_for_customer as gql_orders_for_customer,
    _gql_product_details_in_order as gql_product_details_in_order,
    _json_tool_result as json_tool_result,
)
from sqlagent.orders.structured import order_details_struct
from sqlagent.accounts.index import (
    _gql_addresses as gql_addresses,
    _gql_contacts as gql_contacts,
    _gql_customer_profile as gql_customer_profile,
)
from sqlagent.finance.index import (
    get_ap_invoice_details as _fin_get_ap_invoice_details,
    get_gl_account_transactions as _fin_get_gl_account_transactions,
    get_invoice_details as _fin_get_invoice_details,
    get_po_order_status as _fin_get_po_order_status,
    get_sales_price as _fin_get_sales_price,
    get_vendor_ap_invoices as _fin_get_vendor_ap_invoices,
    get_vendor_details as _fin_get_vendor_details,
)
from sqlagent.shipments.index import (
    _gql_baccount_ids_for_acct_cd as gql_baccount_ids_for_acct_cd,
)
from sqlagent.shipments.structured import (
    shipping_by_order_struct,
    shipping_by_shipment_struct,
)
from sqlagent.resolvers import (
    find_customer as _resolve_find_customer,
    get_order_addresses as _resolve_get_order_addresses,
    resolve_contact as _resolve_resolve_contact,
)
from sqlagent.composites import (
    get_customer_overview as _composite_customer_overview,
    get_order_overview as _composite_order_overview,
    get_customer_shipment_status as _composite_customer_shipment_status,
)
from sqlagent.analytics import (
    get_customer_order_summary as _an_customer_order_summary,
    get_customers_by_region as _an_customers_by_region,
    get_orders_by_product as _an_orders_by_product,
    get_product_sales as _an_product_sales,
    get_top_customers_by_spend as _an_top_customers_by_spend,
)


# ── MCP server + transport security (same host-allowlist rationale as before) ─

def _allowed_hosts() -> list[str]:
    raw = (os.getenv("MINIERP_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-mcp-minierp",
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


def _missing(field_name: str, intent: str) -> str:
    return json_tool_result(
        status="missing_identifier", intent=intent,
        message=f"A {field_name} is required.", missingFields=[field_name],
    )


# ── Orders domain ─────────────────────────────────────────────────────────────

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
        return _missing("order_number", "order_details")
    return await order_details_struct(order_number.strip(), company_id)


@mcp.tool()
async def get_product_details_in_order(
    order_number: str, company_id: int | None = None, page: int = 1, page_size: int = 10
) -> str:
    """List the line items (products, quantities, prices) inside one sales order."""
    if not order_number.strip():
        return _missing("order_number", "product_details_in_order")
    return await gql_product_details_in_order(
        order_number.strip(), company_id=company_id, page=page, page_size=page_size
    )


# ── Accounts domain ───────────────────────────────────────────────────────────

@mcp.tool()
async def get_contacts(customer_id: str, page: int = 1, page_size: int = 10) -> str:
    """List contacts (name, role, phone, email) on a customer's account."""
    if not customer_id.strip():
        return _MISSING_CUSTOMER
    return await gql_contacts(customer_id.strip(), page=page, page_size=page_size)


@mcp.tool()
async def get_addresses(customer_id: str, page: int = 1, page_size: int = 10) -> str:
    """List addresses on file for a customer's account."""
    if not customer_id.strip():
        return _MISSING_CUSTOMER
    return await gql_addresses(customer_id.strip(), page=page, page_size=page_size)


@mcp.tool()
async def get_customer_profile(customer_id: str) -> str:
    """Get a customer's billing/credit profile: credit limit, terms, default payment method, statement cycle."""
    if not customer_id.strip():
        return _MISSING_CUSTOMER
    return await gql_customer_profile(customer_id.strip())


# ── Shipments domain ──────────────────────────────────────────────────────────

@mcp.tool()
async def get_shipping_by_shipment(shipment_number: str, customer_id: str) -> str:
    """Get tracking/invoice details for a shipment by shipment number.

    Released only if the shipment's linked order belongs to customer_id."""
    if not shipment_number.strip():
        return _missing("shipment_number", "shipment_tracking")
    if not customer_id.strip():
        return _MISSING_CUSTOMER
    owner_ids = await gql_baccount_ids_for_acct_cd(customer_id.strip(), resolve_company_ids(None))
    return await shipping_by_shipment_struct(shipment_number.strip(), owner_baccount_ids=owner_ids)


@mcp.tool()
async def get_shipping_by_order(order_number: str, company_id: int | None = None) -> str:
    """Get shipment/tracking/invoice numbers linked to a sales order by order number."""
    if not order_number.strip():
        return _missing("order_number", "shipment_tracking")
    return await shipping_by_order_struct(order_number.strip(), company_id)


# ── Finance domain ────────────────────────────────────────────────────────────

@mcp.tool()
async def get_vendor_details(vendor_code: str) -> str:
    """Get a vendor's profile: class, terms, currency, default payment method, 1099 flag."""
    if not vendor_code.strip():
        return _missing("vendor_code", "vendor_details")
    return await _fin_get_vendor_details(vendor_code.strip())


@mcp.tool()
async def get_vendor_ap_invoices(vendor_code: str, page: int = 1, page_size: int = 10) -> str:
    """List AP invoices/bills for a vendor by vendor code."""
    if not vendor_code.strip():
        return _missing("vendor_code", "vendor_ap_invoices")
    return await _fin_get_vendor_ap_invoices(vendor_code.strip(), page=page, page_size=page_size)


@mcp.tool()
async def get_ap_invoice_details(invoice_number: str, company_id: int | None = None) -> str:
    """Get header-level details (total, tax, due date, paid status) for one AP invoice by invoice/reference number."""
    if not invoice_number.strip():
        return _missing("invoice_number", "ap_invoice_details")
    return await _fin_get_ap_invoice_details(invoice_number.strip(), company_id)


@mcp.tool()
async def get_invoice_details(invoice_number: str, company_id: int | None = None) -> str:
    """Get header-level details (total, tax, unpaid balance, terms) for one AR invoice by invoice/reference number."""
    if not invoice_number.strip():
        return _missing("invoice_number", "invoice_details")
    return await _fin_get_invoice_details(invoice_number.strip(), company_id)


@mcp.tool()
async def get_po_order_status(po_number: str, company_id: int | None = None) -> str:
    """Get header-level status (status, total, vendor, ship-via, hold) for one purchase order by PO number."""
    if not po_number.strip():
        return _missing("po_number", "po_order_status")
    return await _fin_get_po_order_status(po_number.strip(), company_id)


@mcp.tool()
async def get_gl_account_transactions(
    account_cd: str,
    start_date: str = "",
    end_date: str = "",
    company_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List GL transactions for one account code, optionally filtered by date range, with a net debit/credit movement for the returned page."""
    if not account_cd.strip():
        return _missing("account_cd", "gl_account_transactions")
    return await _fin_get_gl_account_transactions(
        account_cd.strip(),
        start_date=start_date or None,
        end_date=end_date or None,
        company_id=company_id,
        page=page,
        page_size=page_size,
    )


@mcp.tool()
async def get_sales_price(
    inventory_id: str,
    cust_price_class_id: str = "",
    customer_id: str = "",
    company_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List sales price records (price class, currency, UOM, break quantity) for
    one inventory item, optionally narrowed to a price class or customer."""
    if not inventory_id.strip():
        return _missing("inventory_id", "sales_price")
    return await _fin_get_sales_price(
        inventory_id.strip(),
        cust_price_class_id=cust_price_class_id or None,
        customer_id=customer_id or None,
        company_id=company_id,
        page=page,
        page_size=page_size,
    )


# ── Secondary resolvers (entry points: human handle -> canonical key) ─────────

@mcp.tool()
async def find_customer(
    query: str, by: str = "auto", company_id: str = "", page: int = 1, page_size: int = 10
) -> str:
    """Find candidate customers by name, email, phone, or customer id (acctCd).

    Use this FIRST when the user identifies a customer by anything other than the
    internal customer id -- it returns candidates {customerId, name, company, ...}
    whose customerId then feeds the account-scoped tools. `by` may be
    auto|name|email|phone|acctCd (auto infers from the query)."""
    return await _resolve_find_customer(query, by=by, company_id=company_id or None,
                                        page=page, page_size=page_size)


@mcp.tool()
async def resolve_contact(query: str, by: str = "auto", page: int = 1, page_size: int = 10) -> str:
    """Find the people (contacts) behind an email, phone, or name -- name, title,
    role, and the business account they belong to. `by` may be auto|name|email|phone."""
    return await _resolve_resolve_contact(query, by=by, page=page, page_size=page_size)


@mcp.tool()
async def get_order_addresses(order_number: str) -> str:
    """Get the ship-to and bill-to addresses for a sales order by order number."""
    return await _resolve_get_order_addresses(order_number)


# ── Tertiary composites (360 views across domains) ────────────────────────────

@mcp.tool()
async def get_customer_overview(customer_id: str) -> str:
    """One-call 360 view of a customer: billing/credit profile, primary contact,
    addresses, recent orders, and total spend. Prefer this over separate profile/
    contacts/orders lookups when the user wants a full picture of one customer."""
    return await _composite_customer_overview(customer_id)


@mcp.tool()
async def get_order_overview(order_number: str, company_id: int | None = None) -> str:
    """One-call 360 view of an order: header, line items, ship/bill addresses, and
    shipment/tracking. Prefer this over separate order/product/shipping lookups."""
    return await _composite_order_overview(order_number, company_id)


@mcp.tool()
async def get_customer_shipment_status(customer_id: str, max_orders: int = 5) -> str:
    """Shipment/tracking status of a customer's most recent orders."""
    return await _composite_customer_shipment_status(customer_id, max_orders)


# ── Quaternary: aggregations + reverse lookups ────────────────────────────────

@mcp.tool()
async def get_customer_order_summary(customer_id: str, start_date: str = "", end_date: str = "") -> str:
    """Aggregate a customer's orders: counts by status, total/average spend,
    first/last order date, and month-by-month buckets over a date range."""
    return await _an_customer_order_summary(customer_id, start_date, end_date)


@mcp.tool()
async def get_product_sales(inventory_id: str, start_date: str = "", end_date: str = "") -> str:
    """How a product is selling: total quantity, revenue, and distinct orders/customers."""
    return await _an_product_sales(inventory_id, start_date, end_date)


@mcp.tool()
async def get_orders_by_product(inventory_id: str, page: int = 1, page_size: int = 25) -> str:
    """Which orders (and customers) bought a given product, by inventory id."""
    return await _an_orders_by_product(inventory_id, page, page_size)


@mcp.tool()
async def get_customers_by_region(country: str = "", state: str = "", city: str = "",
                                  page: int = 1, page_size: int = 25) -> str:
    """Customers in a territory, filtered by country, state, and/or city."""
    return await _an_customers_by_region(country, state, city, page, page_size)


@mcp.tool()
async def get_top_customers_by_spend(start_date: str = "", end_date: str = "", limit: int = 10) -> str:
    """Rank customers by total spend over a period (cross-customer analytics)."""
    return await _an_top_customers_by_spend(start_date, end_date, limit)


# ── ASGI app ──────────────────────────────────────────────────────────────────

async def _health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-minierp"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("MINIERP_PORT") or "8021")
    print(f"[mcp-minierp] Starting consolidated MCP server on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
