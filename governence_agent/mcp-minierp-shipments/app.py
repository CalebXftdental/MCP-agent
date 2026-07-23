"""mcp-minierp-shipments -- backend MCP server for shipment/tracking lookups
by order number or shipment number.

Thin backend in the federating-governance topology (see
governence_agent/design_plan.md): exposes read tools returning structured
JSON, holds only its own miniERP credentials, does NO auth/rate/redaction --
that's the Governance Gateway's job.

Trust model: this server does not manage sessions. It receives an
already-resolved, already-trusted `customer_id` (Acumatica AcctCD) from the
gateway. Account ownership for shipment-by-number is enforced here (it needs
a DB lookup through the linked sales order), fail-closed.

Run:
    pip install -r requirements.txt
    python app.py            # serves MCP on PORT (default 8024) at /mcp
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(os.getenv("MINIERP_SHIPMENTS_ENV_FILE") or ".env.local")

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from sqlagent.index import (
    _company_ids as resolve_company_ids,
    _gql_baccount_ids_for_acct_cd as gql_baccount_ids_for_acct_cd,
    _json_tool_result as json_tool_result,
)
from sqlagent.structured import shipping_by_order_struct, shipping_by_shipment_struct


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("MINIERP_SHIPMENTS_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-mcp-minierp-shipments",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in _ALLOWED_HOSTS if ":" not in h and not h.startswith(("localhost", "127."))]
        + ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


@mcp.tool()
async def get_shipping_by_shipment(shipment_number: str, customer_id: str) -> str:
    """Get tracking/invoice details for a shipment by shipment number.

    Released only if the shipment's linked order belongs to customer_id."""
    if not shipment_number.strip():
        return json_tool_result(
            status="missing_identifier", intent="shipment_tracking",
            message="A shipment_number is required.", missingFields=["shipment_number"],
        )
    if not customer_id.strip():
        return json_tool_result(
            status="missing_identifier", intent="shipment_tracking",
            message="A customer_id is required for this lookup.", missingFields=["customerId"],
        )
    owner_ids = await gql_baccount_ids_for_acct_cd(customer_id.strip(), resolve_company_ids(None))
    return await shipping_by_shipment_struct(shipment_number.strip(), owner_baccount_ids=owner_ids)


@mcp.tool()
async def get_shipping_by_order(order_number: str, company_id: int | None = None) -> str:
    """Get shipment/tracking/invoice numbers linked to a sales order by order number."""
    if not order_number.strip():
        return json_tool_result(
            status="missing_identifier", intent="shipment_tracking",
            message="A order_number is required.", missingFields=["order_number"],
        )
    return await shipping_by_order_struct(order_number.strip(), company_id)


async def _health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-minierp-shipments"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("MINIERP_SHIPMENTS_PORT") or "8024")
    print(f"[mcp-minierp-shipments] Starting MCP server on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
