"""Tertiary (composite / 360-view) tools -- one call that fans across several
domains for a single entity, so an internal user gets the whole picture without
issuing four separate lookups.

Implementation: compose the EXISTING governed domain functions and nest their
parsed results under named sections. That reuses each domain's field-name
mapping (so keys stay consistent) and, because the gateway's redaction walks the
result recursively by key name, the composite redacts correctly as long as every
PII/SENSITIVE key it can emit is classified in policy/manifest.py (see the
composite entries there -- this is the "composite widens the redaction surface"
risk called out in ARA_GOVERNANCE_DESIGN.md sec 6).

These are homed on the minierp_accounts backend in the manifest so the accounts
category (the only one granting the FULL level set -- PII + SENSITIVE) authorizes
them; a principal needs full customer-data access to pull a 360 view.
"""
from __future__ import annotations

import json

from sqlagent.orders.index import (
    _company_ids,
    _gql_customer_order_total,
    _gql_orders_for_customer,
    _gql_product_details_in_order,
    _json_tool_result,
)
from sqlagent.orders.structured import order_details_struct
from sqlagent.accounts.index import (
    _gql_addresses,
    _gql_contacts,
    _gql_customer_profile,
)
from sqlagent.shipments.structured import shipping_by_order_struct
from sqlagent.resolvers import get_order_addresses
from sqlagent.accounts.index import _gql_baccount_ids_for_acct_cd
from minierp_core import find_with_offset_pagination


def _parse(result: str):
    try:
        return json.loads(result)
    except (ValueError, TypeError):
        return {"raw": result}


def _first_record(section) -> dict | None:
    if isinstance(section, dict):
        recs = section.get("records")
        if isinstance(recs, list) and recs:
            return recs[0]
    return None


async def get_customer_overview(customer_id: str) -> str:
    """360 view of one customer: profile + primary contact + addresses + recent
    orders + total spend, composed from the accounts and orders domains."""
    acct = (customer_id or "").strip()
    if not acct:
        return _json_tool_result(
            status="missing_identifier", intent="customer_overview",
            message="A customer_id is required.", missingFields=["customer_id"],
        )
    companies = _company_ids(None)
    profile = _parse(await _gql_customer_profile(acct))
    contacts = _parse(await _gql_contacts(acct, page=1, page_size=10))
    addresses = _parse(await _gql_addresses(acct, page=1, page_size=10))
    recent_orders = _parse(await _gql_orders_for_customer(acct, companies, page=1, page_size=5))
    spend = _parse(await _gql_customer_order_total(acct, companies, None, None))
    return _json_tool_result(
        status="ok", intent="customer_overview", customerId=acct,
        profile=profile,
        primaryContact=_first_record(contacts),
        contacts=contacts,
        addresses=addresses,
        recentOrders=recent_orders,
        spend=spend,
    )


async def get_order_overview(order_number: str, company_id: int | None = None) -> str:
    """360 view of one order: header + line items + ship/bill addresses +
    shipment/tracking, composed from the orders, accounts, and shipments domains."""
    on = (order_number or "").strip()
    if not on:
        return _json_tool_result(
            status="missing_identifier", intent="order_overview",
            message="An order_number is required.", missingFields=["order_number"],
        )
    details = _parse(await order_details_struct(on, company_id))
    line_items = _parse(await _gql_product_details_in_order(on, company_id=company_id, page=1, page_size=20))
    addresses = _parse(await get_order_addresses(on))
    shipments = _parse(await shipping_by_order_struct(on, company_id))
    return _json_tool_result(
        status="ok", intent="order_overview", orderNumber=on,
        details=details, lineItems=line_items, addresses=addresses, shipments=shipments,
    )


async def get_customer_shipment_status(customer_id: str, max_orders: int = 5) -> str:
    """Shipment/tracking status of a customer's most recent orders. Resolves the
    customer's recent order numbers, then joins shipment/tracking for each."""
    acct = (customer_id or "").strip()
    if not acct:
        return _json_tool_result(
            status="missing_identifier", intent="customer_shipment_status",
            message="A customer_id is required.", missingFields=["customer_id"],
        )
    baccount_ids = await _gql_baccount_ids_for_acct_cd(acct, None)
    if not baccount_ids:
        return _json_tool_result(status="not_found", intent="customer_shipment_status",
                                 message=f"No customer account found for {acct}.", customerId=acct)
    orders_res = await find_with_offset_pagination("soorder", {
        "select": {"orderNbr": True, "status": True, "orderDate": True},
        "where": {"customerId": {"in": baccount_ids}, "companyId": {"in": _company_ids(None)}},
        "orderBy": {"orderDate": "DESC"},
        "page": 1, "pageSize": max(1, min(20, max_orders)),
    })
    orders = orders_res.get("items") or []
    shipments = []
    for o in orders:
        on = o.get("orderNbr")
        if not on:
            continue
        shipments.append({
            "orderNumber": on, "orderStatus": o.get("status"), "orderDate": o.get("orderDate"),
            "shipment": _parse(await shipping_by_order_struct(str(on), None)),
        })
    return _json_tool_result(status="ok", intent="customer_shipment_status",
                             customerId=acct, shipments=shipments, count=len(shipments))
