"""mcp-minierp-accounts -- backend MCP server for the account-master-data
domain (BAccount/Customer/Contact/Address).

Thin backend in the federating-governance topology (see
governence_agent/design_plan.md): exposes read tools returning structured
JSON, holds only its own miniERP credentials, does NO auth/rate/redaction --
that's the Governance Gateway's job.

Shared across departments (sales, customer_service, crm_marketing all grant
tools from it) -- it is the account-data domain, not any one team's backend.

Run:
    pip install -r requirements.txt
    python app.py            # serves MCP on PORT (default 8023) at /mcp
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(os.getenv("MINIERP_ACCOUNTS_ENV_FILE") or ".env.local")

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from sqlagent.index import (
    _gql_addresses as gql_addresses,
    _gql_contacts as gql_contacts,
    _gql_customer_profile as gql_customer_profile,
    _json_tool_result as json_tool_result,
)


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("MINIERP_ACCOUNTS_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


_ALLOWED_HOSTS = _allowed_hosts()

mcp = FastMCP(
    "frontier-mcp-minierp-accounts",
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


async def _health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-minierp-accounts"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("WEBSITES_PORT") or os.getenv("MINIERP_ACCOUNTS_PORT") or "8023")
    print(f"[mcp-minierp-accounts] Starting MCP server on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
