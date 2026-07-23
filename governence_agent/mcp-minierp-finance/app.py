"""mcp-minierp-finance -- backend MCP server for finance-domain miniERP data.

Thin backend in the same federating-governance topology as the other miniERP
domain backends (mcp-minierp-orders/-accounts/-shipments; see
governence_agent/design_plan.md): exposes finance read tools returning
structured JSON, holds only its own miniERP credentials, and does NO
authentication/rate-limiting/redaction -- that's the Governance Gateway's job.

Unlike mcp-minierp-orders/-accounts (customer-facing, account-scoped to a
verified customer_id), this backend's tools are either directly ID-scoped
(vendor code, invoice number, PO number, GL account code) with no
session-scope trust model, since finance consumers are internal staff/agents,
not external customers acting on their own account. Gate *who* may call this
backend at all via the gateway's access matrix, not via per-row ownership
checks here.

get_invoice_details (ARInvoice) is the one exception worth noting: it's also
narrowly granted to the customer_service department (see
governance_core/policy/departments.py) so front-line reps can answer "what's
my balance" -- that's a gateway-side access-matrix decision, invisible here.

Run:
    pip install -r requirements.txt
    python app.py            # serves MCP on PORT (default 8022) at /mcp
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(os.getenv("MINIERP_FINANCE_ENV_FILE") or ".env.local")

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from sqlagent.index import (
    get_ap_invoice_details as _get_ap_invoice_details,
    get_gl_account_transactions as _get_gl_account_transactions,
    get_invoice_details as _get_invoice_details,
    get_po_order_status as _get_po_order_status,
    get_vendor_ap_invoices as _get_vendor_ap_invoices,
    get_vendor_details as _get_vendor_details,
    _json_tool_result as json_tool_result,
)


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("MINIERP_FINANCE_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-mcp-minierp-finance",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in _ALLOWED_HOSTS if ":" not in h and not h.startswith(("localhost", "127."))]
        + ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


def _missing(field_name: str, intent: str) -> str:
    return json_tool_result(
        status="missing_identifier", intent=intent,
        message=f"A {field_name} is required.", missingFields=[field_name],
    )


@mcp.tool()
async def get_vendor_details(vendor_code: str) -> str:
    """Get a vendor's profile: class, terms, currency, default payment method, 1099 flag."""
    if not vendor_code.strip():
        return _missing("vendor_code", "vendor_details")
    return await _get_vendor_details(vendor_code.strip())


@mcp.tool()
async def get_vendor_ap_invoices(vendor_code: str, page: int = 1, page_size: int = 10) -> str:
    """List AP invoices/bills for a vendor by vendor code."""
    if not vendor_code.strip():
        return _missing("vendor_code", "vendor_ap_invoices")
    return await _get_vendor_ap_invoices(vendor_code.strip(), page=page, page_size=page_size)


@mcp.tool()
async def get_ap_invoice_details(invoice_number: str, company_id: int | None = None) -> str:
    """Get header-level details (total, tax, due date, paid status) for one AP invoice by invoice/reference number."""
    if not invoice_number.strip():
        return _missing("invoice_number", "ap_invoice_details")
    return await _get_ap_invoice_details(invoice_number.strip(), company_id)


@mcp.tool()
async def get_invoice_details(invoice_number: str, company_id: int | None = None) -> str:
    """Get header-level details (total, tax, unpaid balance, terms) for one AR invoice by invoice/reference number."""
    if not invoice_number.strip():
        return _missing("invoice_number", "invoice_details")
    return await _get_invoice_details(invoice_number.strip(), company_id)


@mcp.tool()
async def get_po_order_status(po_number: str, company_id: int | None = None) -> str:
    """Get header-level status (status, total, vendor, ship-via, hold) for one purchase order by PO number."""
    if not po_number.strip():
        return _missing("po_number", "po_order_status")
    return await _get_po_order_status(po_number.strip(), company_id)


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
    return await _get_gl_account_transactions(
        account_cd.strip(),
        start_date=start_date or None,
        end_date=end_date or None,
        company_id=company_id,
        page=page,
        page_size=page_size,
    )


async def _health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-minierp-finance"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("MINIERP_FINANCE_PORT") or "8022")
    print(f"[mcp-minierp-finance] Starting MCP server on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
