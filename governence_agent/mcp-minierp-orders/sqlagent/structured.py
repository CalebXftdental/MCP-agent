"""Structured (JSON) variant of the order-header lookup, so the gateway can
redact per field (can't drop/mask a field inside a formatted text blob).
"""
from __future__ import annotations

from sqlagent.index import (
    MINIERP_FIELDS as F,
    _display_order_status,
    _json_tool_result,
    _pending_line_records_for_order,
    _resolve_order_header_lookup,
)


async def order_details_struct(order_number: str, company_id: int | None = None) -> str:
    head, matched_number, matched_company = await _resolve_order_header_lookup(order_number, company_id)
    if not head:
        return _json_tool_result(
            status="not_found", intent="order_details",
            message=f"Order {order_number} not found.", orderNumber=order_number,
        )
    pending = await _pending_line_records_for_order(
        matched_number or order_number, matched_company, head.get(F["order_status"])
    )
    return _json_tool_result(
        status="success", intent="order_details",
        orderNumber=head.get(F["order_number"]),
        orderStatus=_display_order_status(head.get(F["order_status"])),
        statusCode=head.get(F["order_status"]),
        total=float(head.get(F["order_total"]) or 0),
        date=head.get(F["created_date"]),
        company=matched_company,
        pendingLines=pending,
    )
