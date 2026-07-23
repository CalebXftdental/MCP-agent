"""Shipments-domain miniERP tools: shipment/tracking lookups by order number or
shipment number.

Split out of the original mcp-minierp (2026-07). This domain needs its own
copy of BAccount resolution and SOOrder header/line lookup (duplicated from
mcp-minierp-orders, same pattern mcp-minierp-finance already uses for its own
BAccount resolution) because:
  - get_shipping_by_shipment must verify the shipment's linked sales order
    belongs to the verified customer (order ownership check) -- needs SOOrder.
  - both shipment tools enrich their result with order status/date/total and
    pending (unshipped) line items -- needs SOOrder + SOLine.
Each domain backend is self-contained and queries these tables directly
rather than calling into mcp-minierp-orders' code, consistent with "backends
are thin, own their own credentials/queries."
"""

from __future__ import annotations

from typing import Any

from minierp_core import (
    GraphQLConfigError,
    GraphQLQueryError,
    find_with_offset_pagination,
    get_sales_order_data_by_order_number,
)

REGION: str | None = "CA"

MINIERP_ENTITIES: dict[str, str] = {
    "baccount":         "baccount",
    "sales_order":      "soorder",
    "sales_order_line": "soline",
}

MINIERP_FIELDS: dict[str, str] = {
    # SOOrder
    "order_number": "orderNbr",
    "customer_id":  "customerId",
    "order_status": "status",
    "order_total":  "orderTotal",
    "created_date": "orderDate",
    "company_id":   "companyId",
    # SOLine
    "line_order_nbr":   "orderNbr",
    "product_name":     "tranDesc",
    "ordered_quantity": "orderQty",
    "shipped_quantity": "shippedQty",
    # BAccount
    "baccount_id": "bAccountId",
    "acct_cd":     "AcctCD",
}

_DEFAULT_LIST_PAGE_SIZE = 10
_MAX_CUSTOMER_PAGE_SIZE = 25


def _select_all_for(*field_keys: str) -> dict[str, bool]:
    return {MINIERP_FIELDS[k]: True for k in field_keys}


def _ensure_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _company_ids(company_ids: list | None) -> list[int]:
    if REGION == "CA":
        return [11]
    if REGION == "US":
        return [2]
    valid = {2, 11}
    resolved: list[int] = []
    for item in _ensure_list(company_ids) or [2, 11]:
        try:
            company_id = int(item)
        except (TypeError, ValueError):
            continue
        if company_id in valid:
            resolved.append(company_id)
    return resolved or [2, 11]


def _clamp_page(value: Any, default: int = 1) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, page)


def _clamp_page_size(value: Any, default: int = _DEFAULT_LIST_PAGE_SIZE) -> int:
    try:
        page_size = int(value)
    except (TypeError, ValueError):
        return default
    return min(_MAX_CUSTOMER_PAGE_SIZE, max(1, page_size))


def _json_tool_result(**payload: Any) -> str:
    import json
    return json.dumps({"source": "miniERP", **payload}, ensure_ascii=True)


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize_lookup_identifier(value: str | None) -> str | None:
    import re
    normalized = re.sub(r"[\s-]+", "", str(value or "").strip()).upper()
    return normalized or None


def _identifier_candidates(value: str | None) -> list[str]:
    raw = str(value or "").strip().upper()
    normalized = _normalize_lookup_identifier(value)
    candidates: list[str] = []
    for candidate in (raw, normalized):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _company_lookup_candidates(company_id: int | None) -> list[int]:
    if company_id is not None:
        try:
            return [int(company_id)]
        except (TypeError, ValueError):
            pass
    return _company_ids(None)


def _coerce_company_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_order_status(raw_status: Any) -> str:
    status = str(raw_status or "").upper()
    return {
        "N": "Open", "C": "Completed", "H": "Hold", "B": "Back Order",
        "S": "Shipping", "I": "Invoiced", "L": "Cancelled", "X": "Cancelled",
    }.get(status, str(raw_status or "?"))


def _filter_items_by_region(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Post-filter a result list by companyID when REGION is set.

    Used for the custom getSalesOrderDataByOrderNumber field, which doesn't
    accept a companyId parameter. That response's key is 'companyID' (capital
    I, capital D) -- different casing than SOOrder's own 'companyId'.
    """
    if REGION is None:
        return items
    allowed = set(_company_ids(None))
    return [item for item in items if _coerce_company_id(item.get("companyID")) in allowed]


# ── BAccount resolution (customer_id -> bAccountId(s); never trust a supplied one) ─


async def _gql_baccount_ids_for_acct_cd(acct_cd: str, company_ids: list | None) -> list[Any]:
    options = {
        "where": {
            MINIERP_FIELDS["acct_cd"]: acct_cd,
            MINIERP_FIELDS["company_id"]: {"in": _company_ids(company_ids)},
        },
        "page": 1,
        "pageSize": 100,
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["baccount"], options)
    return [
        item.get(MINIERP_FIELDS["baccount_id"])
        for item in (result.get("items") or [])
        if item.get(MINIERP_FIELDS["baccount_id"]) is not None
    ]


# ── Order header/line resolution (for ownership check + status/pending-lines enrichment) ─


async def _order_belongs_to_customer(
    order_number: str,
    company_id: int | None,
    baccount_ids: list[Any],
) -> bool:
    """True only if an order with this number is linked to one of baccount_ids.

    Fails closed: no linked owner, or a query error, => not owned."""
    if not baccount_ids:
        return False
    for candidate in _identifier_candidates(order_number):
        for company_candidate in _company_lookup_candidates(company_id):
            options = {
                "select": _select_all_for("order_number"),
                "where": {
                    MINIERP_FIELDS["order_number"]: candidate,
                    MINIERP_FIELDS["company_id"]: company_candidate,
                    MINIERP_FIELDS["customer_id"]: {"in": baccount_ids},
                },
                "page": 1,
                "pageSize": 1,
            }
            try:
                result = await find_with_offset_pagination(MINIERP_ENTITIES["sales_order"], options)
            except (GraphQLConfigError, GraphQLQueryError):
                return False
            if result.get("items"):
                return True
    return False


async def _query_order_header_once(order_number: str, company_id: int) -> dict[str, Any] | None:
    options = {
        "select": _select_all_for("order_number", "order_status", "order_total", "created_date", "company_id"),
        "where": {
            MINIERP_FIELDS["order_number"]: order_number,
            MINIERP_FIELDS["company_id"]: company_id,
        },
        "page": 1,
        "pageSize": 1,
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["sales_order"], options)
    items = result.get("items") or []
    return items[0] if items else None


async def _resolve_order_header_lookup(
    order_number: str,
    company_id: int | None = None,
) -> tuple[dict[str, Any] | None, str | None, int | None]:
    for candidate in _identifier_candidates(order_number):
        for company_candidate in _company_lookup_candidates(company_id):
            item = await _query_order_header_once(candidate, company_candidate)
            if item:
                matched_company = (
                    _coerce_company_id(item.get(MINIERP_FIELDS["company_id"])) or company_candidate
                )
                return item, candidate, matched_company
    return None, None, None


async def _pending_line_records_for_order(
    order_number: str,
    company_id: int | None,
    order_status: Any,
) -> list[dict[str, Any]]:
    if str(order_status or "").upper() not in {"B", "S"} or company_id is None:
        return []
    options = {
        "select": _select_all_for("product_name", "ordered_quantity", "shipped_quantity"),
        "where": {
            MINIERP_FIELDS["line_order_nbr"]: order_number,
            MINIERP_FIELDS["company_id"]: company_id,
            MINIERP_FIELDS["shipped_quantity"]: 0,
        },
        "page": 1,
        "pageSize": 30,
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["sales_order_line"], options)
    items = result.get("items") or []
    if not items:
        return []
    f_name = MINIERP_FIELDS["product_name"]
    f_ordered = MINIERP_FIELDS["ordered_quantity"]
    f_shipped = MINIERP_FIELDS["shipped_quantity"]
    return [
        {
            "description": item.get(f_name) or "Unnamed line",
            "orderedQty": float(item.get(f_ordered) or 0),
            "shippedQty": float(item.get(f_shipped) or 0),
        }
        for item in items
    ]


async def _resolve_shipping_lookup_by_order_number(order_number: str) -> tuple[list[dict[str, Any]], str | None]:
    for candidate in _identifier_candidates(order_number):
        result = await get_sales_order_data_by_order_number(candidate)
        items = _filter_items_by_region(result.get("items") or [])
        if items:
            return items, candidate
    return [], None


async def _resolve_shipping_lookup_by_shipment_number(shipment_number: str) -> tuple[list[dict[str, Any]], str | None]:
    for candidate in _identifier_candidates(shipment_number):
        result = await get_sales_order_data_by_order_number(candidate, field="shipmentNumber")
        items = _filter_items_by_region(result.get("items") or [])
        if items:
            return items, candidate
    return [], None
