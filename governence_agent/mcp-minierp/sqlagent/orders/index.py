"""Orders-domain miniERP tools: sales orders, order lines, order totals.

Split out of the original mcp-minierp (2026-07) once mcp-minierp-finance made
clear that backend was a shared domain (SOOrder/BAccount/Customer data used by
multiple departments), not "customer service's" own backend. This backend
owns SOOrder/SOLine/InventoryItem plus the BAccount resolution every
account-scoped tool needs (customer_id -> bAccountId(s), never trusted as
supplied directly).

Dropped, not carried over from the original file: a legacy regex-based
question-classification/pagination-extraction layer (`query_agent`,
`classify_topic`, `_extract_*`) that predated FastMCP tool definitions and was
not called by any @mcp.tool() in app.py -- dead code relative to the live
server, not reproduced here.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from typing import Any

from minierp_core import (
    GraphQLQueryError,
    find_with_offset_pagination,
)

# "CA" -> company 11 only, "US" -> company 2 only, None -> both.
# MINIERP_REGION overrides the default below; set it to "ALL" (or "") to see
# both companies instead of Canada-only.
_REGION_ENV = os.getenv("MINIERP_REGION", "CA")
REGION: str | None = None if _REGION_ENV in ("", "ALL") else _REGION_ENV

MINIERP_ENTITIES: dict[str, str] = {
    "baccount":         "baccount",
    "sales_order":      "soorder",
    "sales_order_line": "soline",
    "inventory_item":   "InventoryItem",
}

MINIERP_FIELDS: dict[str, str] = {
    # SOOrder
    "order_number":   "orderNbr",
    "customer_id":    "customerId",
    "order_status":   "status",
    "order_total":    "orderTotal",
    "created_date":   "orderDate",
    "company_id":     "companyId",
    # SOLine
    "line_order_nbr":    "orderNbr",
    "product_name":      "tranDesc",
    "sku":                "inventoryId",
    "quantity":           "shippedQty",
    "ordered_quantity":   "orderQty",
    "shipped_quantity":   "shippedQty",
    "unit_price":         "curyUnitPrice",
    "line_total":         "extPrice",
    "inventory_item_id":  "inventoryId",
    "inventory_cd":       "inventoryCd",
    "inventory_descr":    "descr",
    # BAccount (customer_id resolution)
    "baccount_id": "bAccountId",
    "acct_cd":     "AcctCD",
}

_DEFAULT_LIST_PAGE_SIZE = 10
_MAX_CUSTOMER_PAGE_SIZE = 25
_AGGREGATE_PAGE_SIZE = 250
_MAX_AGGREGATE_PAGES = 20


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
    return json.dumps({"source": "miniERP", **payload}, ensure_ascii=True)


def _display_order_status(raw_status: Any) -> str:
    status = str(raw_status or "").upper()
    return {
        "N": "Open", "C": "Completed", "H": "Hold", "B": "Back Order",
        "S": "Shipping", "I": "Invoiced", "L": "Cancelled", "X": "Cancelled",
    }.get(status, str(raw_status or "?"))


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


def _format_minierp_date(date_str: str, *, end_of_day: bool = False) -> str:
    if "T" in date_str:
        return date_str
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    suffix = "23:59:59Z" if end_of_day else "00:00:00Z"
    return f"{dt:%Y-%m-%d}T{suffix}"


# ── BAccount resolution (customer_id -> bAccountId(s); never trust a supplied one) ─


async def _gql_baccounts_for_acct_cd(
    acct_cd: str,
    company_ids: list | None,
) -> list[dict[str, Any]]:
    options = {
        "where": {
            MINIERP_FIELDS["acct_cd"]: acct_cd,
            MINIERP_FIELDS["company_id"]: {"in": _company_ids(company_ids)},
        },
        "page": 1,
        "pageSize": 100,
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["baccount"], options)
    return result.get("items") or []


async def _gql_baccount_ids_for_acct_cd(
    acct_cd: str,
    company_ids: list | None,
) -> list[Any]:
    baccounts = await _gql_baccounts_for_acct_cd(acct_cd, company_ids)
    return [
        item.get(MINIERP_FIELDS["baccount_id"])
        for item in baccounts
        if item.get(MINIERP_FIELDS["baccount_id"]) is not None
    ]


# ── Order header resolution (by order number + company, never by customer_id) ──


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


async def _query_order_lines_once(
    order_number: str,
    company_id: int,
    *,
    page: int,
    page_size: int,
    select_fields: tuple[str, ...],
    unshipped_only: bool = False,
) -> dict[str, Any]:
    where: dict[str, Any] = {
        MINIERP_FIELDS["line_order_nbr"]: order_number,
        MINIERP_FIELDS["company_id"]: company_id,
    }
    if unshipped_only:
        where[MINIERP_FIELDS["shipped_quantity"]] = 0
    options = {
        "select": _select_all_for(*select_fields),
        "where": where,
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    return await find_with_offset_pagination(MINIERP_ENTITIES["sales_order_line"], options)


async def _resolve_order_lines_lookup(
    order_number: str,
    company_id: int | None,
    *,
    page: int,
    page_size: int,
    select_fields: tuple[str, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None, int | None]:
    fallback = {"items": [], "page": _clamp_page(page), "pageSize": _clamp_page_size(page_size), "hasMore": False}
    last_result = fallback
    for candidate in _identifier_candidates(order_number):
        for company_candidate in _company_lookup_candidates(company_id):
            result = await _query_order_lines_once(
                candidate, company_candidate, page=page, page_size=page_size, select_fields=select_fields,
            )
            items = result.get("items") or []
            last_result = result
            if items:
                return result, items, candidate, company_candidate
    return last_result, [], None, None


async def _pending_line_records_for_order(
    order_number: str,
    company_id: int | None,
    order_status: Any,
) -> list[dict[str, Any]]:
    if str(order_status or "").upper() not in {"B", "S"} or company_id is None:
        return []
    result = await _query_order_lines_once(
        order_number, company_id, page=1, page_size=30,
        select_fields=("product_name", "ordered_quantity", "shipped_quantity"),
        unshipped_only=True,
    )
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


# ── Public tools ─────────────────────────────────────────────────────────────


async def _gql_orders_for_customer(
    acct_cd: str,
    company_ids: list | None,
    start_date: str | None = None,
    end_date: str | None = None,
    order_status: str | None = None,
    min_total: float | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
) -> str:
    baccount_ids = await _gql_baccount_ids_for_acct_cd(acct_cd, company_ids)
    if not baccount_ids:
        return _json_tool_result(
            status="not_found", intent="customer_orders",
            message=f"No customer account found for {acct_cd}.", customerId=acct_cd, records=[],
        )

    where: dict[str, Any] = {
        MINIERP_FIELDS["customer_id"]: {"in": baccount_ids},
        MINIERP_FIELDS["company_id"]: {"in": _company_ids(company_ids)},
    }
    if start_date:
        where[MINIERP_FIELDS["created_date"]] = {"gte": _format_minierp_date(start_date)}
    if end_date:
        existing = where.get(MINIERP_FIELDS["created_date"], {})
        if isinstance(existing, dict):
            existing["lte"] = _format_minierp_date(end_date, end_of_day=True)
            where[MINIERP_FIELDS["created_date"]] = existing
        else:
            where[MINIERP_FIELDS["created_date"]] = {"lte": _format_minierp_date(end_date, end_of_day=True)}
    if order_status:
        where[MINIERP_FIELDS["order_status"]] = order_status
    if min_total is not None:
        where[MINIERP_FIELDS["order_total"]] = {"gt": min_total}

    options = {
        "select": _select_all_for("order_number", "order_status", "order_total", "created_date"),
        "where": where,
        "orderBy": {MINIERP_FIELDS["created_date"]: "DESC"},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["sales_order"], options)
    items = result.get("items") or []
    if not items:
        return _json_tool_result(
            status="not_found", intent="customer_orders",
            message=f"No orders found for customer {acct_cd}.", customerId=acct_cd,
            filters={"startDate": start_date, "endDate": end_date,
                     "status": _display_order_status(order_status) if order_status else None, "minTotal": min_total},
            records=[],
            pagination={"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                        "returned": 0, "hasMore": bool(result.get("hasMore"))},
        )

    f_num, f_st, f_tot, f_date = (
        MINIERP_FIELDS["order_number"], MINIERP_FIELDS["order_status"],
        MINIERP_FIELDS["order_total"], MINIERP_FIELDS["created_date"],
    )
    filter_labels: list[str] = []
    if start_date or end_date:
        filter_labels.append("date range")
    if order_status:
        filter_labels.append(f"status={_display_order_status(order_status)}")
    if min_total is not None:
        filter_labels.append(f"total>${min_total:.2f}")

    records = [
        {"orderNumber": it.get(f_num), "status": _display_order_status(it.get(f_st)),
         "statusCode": it.get(f_st), "total": float(it.get(f_tot) or 0), "date": it.get(f_date)}
        for it in items
    ]
    pagination = {"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                  "returned": len(records), "hasMore": bool(result.get("hasMore"))}
    return _json_tool_result(
        status="success", intent="customer_orders",
        message=(f"Showing {len(records)} order{'s' if len(records) != 1 else ''} for {acct_cd}"
                 f"{' filtered by ' + ', '.join(filter_labels) if filter_labels else ''}."),
        customerId=acct_cd,
        filters={"startDate": start_date, "endDate": end_date,
                 "status": _display_order_status(order_status) if order_status else None, "minTotal": min_total},
        records=records, pagination=pagination,
        nextAction=(f"More orders are available. Ask whether to show page {pagination['page'] + 1}."
                    if pagination["hasMore"] else None),
    )


async def _gql_customer_order_total(
    acct_cd: str,
    company_ids: list | None,
    start_date: str | None,
    end_date: str | None,
) -> str:
    """GraphQL has no aggregate; fetch matching orders and sum client-side."""
    baccount_ids = await _gql_baccount_ids_for_acct_cd(acct_cd, company_ids)
    if not baccount_ids:
        return _json_tool_result(
            status="not_found", intent="customer_order_total",
            message=f"No customer account found for {acct_cd}.", customerId=acct_cd,
        )

    where: dict[str, Any] = {
        MINIERP_FIELDS["customer_id"]: {"in": baccount_ids},
        MINIERP_FIELDS["company_id"]: {"in": _company_ids(company_ids)},
        MINIERP_FIELDS["order_status"]: "C",
    }
    if start_date:
        where[MINIERP_FIELDS["created_date"]] = {"gte": _format_minierp_date(start_date)}
    if end_date:
        existing = where.get(MINIERP_FIELDS["created_date"], {})
        if isinstance(existing, dict):
            existing["lte"] = _format_minierp_date(end_date, end_of_day=True)
            where[MINIERP_FIELDS["created_date"]] = existing
        else:
            where[MINIERP_FIELDS["created_date"]] = {"lte": _format_minierp_date(end_date, end_of_day=True)}

    grand_total = 0.0
    order_count = 0
    page = 1
    has_more = True
    pages_fetched = 0
    while has_more and pages_fetched < _MAX_AGGREGATE_PAGES:
        options = {
            "select": _select_all_for("order_total"),
            "where": where,
            "orderBy": {MINIERP_FIELDS["created_date"]: "DESC"},
            "page": page,
            "pageSize": _AGGREGATE_PAGE_SIZE,
        }
        result = await find_with_offset_pagination(MINIERP_ENTITIES["sales_order"], options)
        items = result.get("items") or []
        order_count += len(items)
        grand_total += sum(float(it.get(MINIERP_FIELDS["order_total"]) or 0) for it in items)
        has_more = bool(result.get("hasMore"))
        pages_fetched += 1
        page += 1

    is_complete = not has_more
    return _json_tool_result(
        status="success", intent="customer_order_total",
        message=(f"Calculated completed-order total for {acct_cd}." if is_complete
                 else f"Calculated a partial completed-order total for {acct_cd}; more orders remain."),
        customerId=acct_cd,
        filters={"startDate": start_date, "endDate": end_date, "status": "Completed"},
        orderCount=order_count, grandTotal=round(grand_total, 2), currency="USD", isComplete=is_complete,
        pagination={"pagesFetched": pages_fetched, "pageSize": _AGGREGATE_PAGE_SIZE, "hasMore": has_more},
    )


async def _gql_inventory_items_by_ids(
    inventory_ids: list[Any],
    company_ids: list | None = None,
) -> dict[str, dict[str, Any]]:
    ids = []
    for item in inventory_ids:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}

    options = {
        "select": _select_all_for("inventory_item_id", "inventory_cd", "inventory_descr"),
        "where": {
            MINIERP_FIELDS["company_id"]: {"in": _company_ids(company_ids)},
            MINIERP_FIELDS["inventory_item_id"]: {"in": ids},
        },
        "page": 1,
        "pageSize": min(len(ids), _MAX_CUSTOMER_PAGE_SIZE),
    }
    try:
        result = await find_with_offset_pagination(MINIERP_ENTITIES["inventory_item"], options)
    except GraphQLQueryError:
        return {}

    f_id = MINIERP_FIELDS["inventory_item_id"]
    return {
        str(item.get(f_id)): item
        for item in result.get("items") or []
        if item.get(f_id) is not None
    }


async def _gql_product_details_in_order(
    order_number: str,
    company_id: int | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
) -> str:
    result, items, matched_order_number, matched_company = await _resolve_order_lines_lookup(
        order_number, company_id, page=page, page_size=page_size,
        select_fields=("product_name", "sku", "quantity", "unit_price", "line_total"),
    )
    if not items:
        return _json_tool_result(
            status="not_found", intent="product_details_in_order",
            message=f"No line items found for order {order_number}.", orderNumber=order_number, records=[],
            pagination={"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                        "returned": 0, "hasMore": bool(result.get("hasMore"))},
        )

    f_name = MINIERP_FIELDS["product_name"]
    f_sku = MINIERP_FIELDS["sku"]
    f_qty = MINIERP_FIELDS["quantity"]
    f_unit = MINIERP_FIELDS["unit_price"]
    f_tot = MINIERP_FIELDS["line_total"]
    f_inv_cd = MINIERP_FIELDS["inventory_cd"]
    f_inv_descr = MINIERP_FIELDS["inventory_descr"]
    inventory = await _gql_inventory_items_by_ids(
        [it.get(f_sku) for it in items],
        [matched_company] if matched_company is not None else _company_ids(None),
    )
    records = []
    for item in items:
        inventory_id = item.get(f_sku)
        inventory_item = inventory.get(str(inventory_id), {})
        records.append({
            "inventoryId": inventory_id,
            "sku": inventory_item.get(f_inv_cd) or str(inventory_id or ""),
            "description": inventory_item.get(f_inv_descr) or item.get(f_name),
            "lineDescription": item.get(f_name),
            "quantity": float(item.get(f_qty) or 0),
            "unitPrice": float(item.get(f_unit) or 0),
            "total": float(item.get(f_tot) or 0),
        })

    pagination = {"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                  "returned": len(records), "hasMore": bool(result.get("hasMore"))}
    return _json_tool_result(
        status="success", intent="product_details_in_order",
        message=f"Showing {len(records)} item{'s' if len(records) != 1 else ''} from order {matched_order_number or order_number}.",
        orderNumber=matched_order_number or order_number, companyId=matched_company,
        records=records, pagination=pagination,
        enrichment={"inventoryItemLookup": "applied" if inventory else "not_available"},
        nextAction=(f"More line items are available. Ask whether to show page {pagination['page'] + 1}."
                    if pagination["hasMore"] else None),
    )
