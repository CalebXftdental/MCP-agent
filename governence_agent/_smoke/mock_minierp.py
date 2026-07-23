"""Mock mcp-minierp backend for end-to-end gateway tests.

Same MCP tool NAMES and structured result shapes as the real mcp-minierp, but
canned data and zero credentials -- so the gateway's full path (edge auth -> PDP
-> MCP client -> backend -> redaction -> audit) can be exercised offline.

Run:  python _smoke/mock_minierp.py   (serves MCP on :8021 at /mcp)
"""
import json
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "mock-mcp-minierp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


@mcp.tool()
async def get_customer_orders(customer_id: str, start_date: str = "", end_date: str = "",
                              order_status: str = "", min_total: float | None = None,
                              page: int = 1, page_size: int = 10) -> str:
    return json.dumps({
        "source": "miniERP", "status": "success", "intent": "customer_orders",
        "customerId": customer_id,
        "records": [{"orderNumber": "SO123", "status": "Open", "statusCode": "N",
                     "total": 1999.50, "date": "2026-01-01"}],
    })


@mcp.tool()
async def get_contacts(customer_id: str, page: int = 1, page_size: int = 10) -> str:
    return json.dumps({
        "source": "miniERP", "status": "success", "intent": "account_contacts",
        "customerId": customer_id,
        "records": [{"contactId": 1, "name": "Jane Doe", "displayName": "Frontier Dental",
                     "type": "Billing", "email": "jane@frontier.com", "phone": "555-1212"}],
    })


@mcp.tool()
async def get_customer_order_total(customer_id: str, start_date: str = "", end_date: str = "") -> str:
    return json.dumps({"source": "miniERP", "status": "success", "intent": "customer_order_total",
                       "customerId": customer_id, "orderCount": 3, "grandTotal": 5999.00, "currency": "USD"})


@mcp.tool()
async def get_addresses(customer_id: str, page: int = 1, page_size: int = 10) -> str:
    return json.dumps({"source": "miniERP", "status": "success", "intent": "account_addresses",
                       "customerId": customer_id,
                       "records": [{"addressId": 1, "type": "Billing", "street": "123 Main St",
                                    "city": "Toronto", "state": "ON", "postalCode": "M5V1A1", "country": "CA"}]})


@mcp.tool()
async def get_shipping_by_shipment(shipment_number: str, customer_id: str) -> str:
    return json.dumps({"source": "miniERP", "status": "success", "intent": "shipment_tracking",
                       "shipmentNumber": shipment_number, "salesOrders": ["SO123"],
                       "trackingNumbers": ["1Z999"], "invoiceNumbers": ["IN001"], "records": []})


@mcp.tool()
async def get_order_details(order_number: str, company_id: int | None = None) -> str:
    return json.dumps({"source": "miniERP", "status": "success", "intent": "order_details",
                       "orderNumber": order_number, "orderStatus": "Open", "statusCode": "N",
                       "total": 1999.50, "date": "2026-01-01", "company": 11, "pendingLines": []})


@mcp.tool()
async def get_product_details_in_order(order_number: str, company_id: int | None = None,
                                       page: int = 1, page_size: int = 10) -> str:
    return json.dumps({"source": "miniERP", "status": "success", "intent": "product_details_in_order",
                       "orderNumber": order_number,
                       "records": [{"inventoryId": 5, "sku": "ABC", "description": "Widget",
                                    "quantity": 2, "unitPrice": 10.0, "total": 20.0}]})


@mcp.tool()
async def get_shipping_by_order(order_number: str, company_id: int | None = None) -> str:
    return json.dumps({"source": "miniERP", "status": "success", "intent": "shipment_tracking",
                       "orderNumber": order_number, "shipmentNumbers": ["SH1"], "trackingNumbers": ["1Z999"],
                       "invoiceNumbers": ["IN001"], "orderStatus": "Open", "orderTotal": 1999.50,
                       "company": 11, "records": []})


app = mcp.streamable_http_app()

if __name__ == "__main__":
    port = int(os.getenv("MINIERP_PORT") or "8021")
    print(f"[mock-minierp] on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
