"""Structured (JSON) variants of the shipment lookups, so the gateway can
redact per field (can't drop/mask a field inside a formatted text blob).
"""
from __future__ import annotations

from typing import Any

from .index import (
    MINIERP_FIELDS as F,
    _coerce_company_id,
    _dedupe_preserve_order,
    _display_order_status,
    _json_tool_result,
    _order_belongs_to_customer,
    _pending_line_records_for_order,
    _resolve_order_header_lookup,
    _resolve_shipping_lookup_by_order_number,
    _resolve_shipping_lookup_by_shipment_number,
)


async def shipping_by_order_struct(order_number: str, company_id: int | None = None) -> str:
    items, matched_number = await _resolve_shipping_lookup_by_order_number(order_number)
    if not items:
        return _json_tool_result(
            status="not_found", intent="shipment_tracking",
            message=f"No shipment found for order {order_number}.", orderNumber=order_number, records=[],
        )
    head, _header_number, matched_company = await _resolve_order_header_lookup(
        matched_number or order_number, company_id
    )
    pending = await _pending_line_records_for_order(
        matched_number or order_number, matched_company,
        head.get(F["order_status"]) if head else None,
    )
    records = [
        {
            "shipmentNumber": item.get("shipmentNumber"),
            "trackingNumber": item.get("trackingNumber"),
            "invoiceNumber": item.get("invoiceNumber"),
            "company": item.get("companyID"),
        }
        for item in items
    ]
    return _json_tool_result(
        status="success", intent="shipment_tracking",
        orderNumber=matched_number or order_number,
        shipmentNumbers=_dedupe_preserve_order([str(i.get("shipmentNumber") or "") for i in items]),
        trackingNumbers=_dedupe_preserve_order([str(i.get("trackingNumber") or "") for i in items]),
        invoiceNumbers=_dedupe_preserve_order([str(i.get("invoiceNumber") or "") for i in items]),
        orderStatus=_display_order_status(head.get(F["order_status"])) if head else None,
        orderDate=head.get(F["created_date"]) if head else None,
        orderTotal=float(head.get(F["order_total"]) or 0) if head else None,
        company=matched_company,
        pendingLines=pending,
        records=records,
    )


async def shipping_by_shipment_struct(
    shipment_number: str,
    owner_baccount_ids: list[Any] | None = None,
) -> str:
    items, matched_number = await _resolve_shipping_lookup_by_shipment_number(shipment_number)
    if not items:
        return _json_tool_result(
            status="not_found", intent="shipment_tracking",
            message=f"No shipment found for shipment number {shipment_number}.",
            shipmentNumber=shipment_number, records=[],
        )
    sales_orders = _dedupe_preserve_order([str(i.get("salesOrderNumber") or "") for i in items])

    # Ownership enforcement: a shipment number has no direct customer link, so
    # resolve it through its sales order(s). Released only if at least one
    # linked sales order belongs to the verified customer. Fail closed.
    if owner_baccount_ids is not None:
        owned = any(
            [await _order_belongs_to_customer(so, None, owner_baccount_ids) for so in sales_orders if so]
        )
        if not owned:
            return _json_tool_result(
                status="not_found", intent="shipment_tracking",
                message=f"I couldn't find shipment {matched_number or shipment_number} on your account.",
                shipmentNumber=matched_number or shipment_number,
            )

    order_head = None
    matched_company = _coerce_company_id(items[0].get("companyID")) if items else None
    if len(sales_orders) == 1:
        order_head, _matched_order_number, matched_company = await _resolve_order_header_lookup(
            sales_orders[0], matched_company
        )
    records = [
        {
            "salesOrderNumber": item.get("salesOrderNumber"),
            "trackingNumber": item.get("trackingNumber"),
            "invoiceNumber": item.get("invoiceNumber"),
            "company": item.get("companyID"),
        }
        for item in items
    ]
    return _json_tool_result(
        status="success", intent="shipment_tracking",
        shipmentNumber=matched_number or shipment_number,
        salesOrders=sales_orders,
        trackingNumbers=_dedupe_preserve_order([str(i.get("trackingNumber") or "") for i in items]),
        invoiceNumbers=_dedupe_preserve_order([str(i.get("invoiceNumber") or "") for i in items]),
        orderStatus=_display_order_status(order_head.get(F["order_status"])) if order_head else None,
        company=matched_company,
        records=records,
    )
