"""Local SQL agent, middleware-aware and backed by the production miniERP GraphQL API.

Hits the real production GraphQL endpoint (db-api.frontierdental.com/graphql)
through sqlagent.graphql_client. Configure credentials in .env.local:
  MINIERP_USERNAME, MINIERP_PASSWORD   (or MINIERP_TOKEN)

Flow:
    query_agent(question)
        -> build ScopeContext from the current query only
        -> classify topic and set the allowed tool set
        -> deterministic tool selection (simulates LLM choice)
        -> middleware gate: is the tool allowed for this topic?
        -> middleware inject: merge customer_id into tool_args
        -> GraphQL query against miniERP
        -> return formatted result

Note on entity / field names: production uses Acumatica naming. The constants
in MINIERP_ENTITIES / MINIERP_FIELDS below are best-guess defaults. If a query
returns 'GraphQL errors: ... unknown field ...', update the relevant entry;
that is the only change needed.
"""

from __future__ import annotations

from calendar import month_name
from datetime import datetime
import json
import re
import time
from typing import Any

from middleware.scope_context import ScopeContext
from sqlagent.graphql_client import (
    GraphQLConfigError,
    GraphQLQueryError,
    find_with_offset_pagination,
    get_sales_order_data_by_order_number,
)

# ── Production miniERP entity / field names (Acumatica) ───────────────────────────────
# Update these if the real schema differs — every GraphQL call below reads
# from this map.

# ── Region filter ─────────────────────────────────────────────────────────────
# "CA" → Canada only (companyId 11)
# "US" → United States only (companyId 2)
# None → both companies (default behaviour)
REGION: str | None = "CA"

MINIERP_ENTITIES: dict[str, str] = {
    "baccount":         "baccount",
    "sales_order":      "soorder",
    "sales_order_line": "soline",
    "inventory_item":   "InventoryItem",
    "contact":          "contact",
    "address":          "address",
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
    "line_order_nbr": "orderNbr",
    "product_name":   "tranDesc",
    "sku":            "inventoryId",
    "quantity":       "shippedQty",
    "ordered_quantity": "orderQty",
    "shipped_quantity": "shippedQty",
    "unit_price":     "curyUnitPrice",
    "line_total":     "extPrice",
    "inventory_item_id": "inventoryId",
    "inventory_cd":      "inventoryCd",
    "inventory_descr":   "descr",
    # Contact
    "contact_id":     "contactId",
    "contact_name":   "fullName",
    "contact_email":  "eMail",
    "contact_phone":  "phone1",
    "contact_role":   "contactType",
    "contact_acct":   "bAccountId",
    # Address
    "address_id":   "addressId",
    "address_acct": "bAccountId",
    "address_type": "addressType",
    "addr_line1":   "addressLine1",
    "addr_city":    "city",
    "addr_state":   "state",
    "addr_zip":     "postalCode",
    "addr_country": "countryId",
    # BAccount
    "baccount_id":        "bAccountId",
    "acct_cd":            "AcctCD",
    "acct_name":          "acctName",
    "primary_contact_id": "primaryContactId",
    "customer_status":    "status",
}

_DEFAULT_PAGE_SIZE = 50
_ORDER_NUMBER_PATTERN = re.compile(
    r"\b(?=[A-Z0-9-]*\d)(?:AR|SO|IN|CM|CR|AF|FI|BP|FW)[A-Z0-9-]{4,}\b",
    re.IGNORECASE,
)
_SHIPMENT_NUMBER_PATTERN = re.compile(
    r"\b(?=[A-Z0-9-]*\d)(?:SH|SHIP|SHP)[A-Z0-9-]{4,}\b",
    re.IGNORECASE,
)
_COMPANY_ID_PATTERN = re.compile(r"\bcompany(?:\s+id)?\s*[:#-]?\s*(2|11)\b", re.IGNORECASE)
_EXTRACTED_ORDER_ID_PATTERN = re.compile(r"\borderId:[ \t]*([A-Z0-9-]{5,})", re.IGNORECASE)
_EXTRACTED_SHIPMENT_ID_PATTERN = re.compile(r"\bshipment(?:Number|Id)?:[ \t]*([A-Z0-9-]{5,})", re.IGNORECASE)
_LABELED_ORDER_NUMBER_PATTERN = re.compile(
    r"\border(?:[ \t]*(?:number|nbr|#|id)|[ \t]*[:#-])[ \t]*([A-Z0-9-]{5,})\b",
    re.IGNORECASE,
)
_LABELED_SHIPMENT_NUMBER_PATTERN = re.compile(
    r"\bshipment(?:[ \t]*(?:number|#|id)|[ \t]*[:#-])[ \t]*([A-Z0-9-]{5,})\b",
    re.IGNORECASE,
)
_EXTRACTED_DATE_RANGE_PATTERN = re.compile(
    r"\bdateRange:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_MONTH_YEAR_PATTERN = re.compile(
    r"\b(?:in|during|for)?\s*("
    + "|".join(name for name in month_name if name)
    + r")\s+(\d{4})\b",
    re.IGNORECASE,
)
_EXTRACTED_STATUS_PATTERN = re.compile(r"\bstatus:\s*([A-Z])\b", re.IGNORECASE)
_MIN_TOTAL_PATTERN = re.compile(
    r"\b(?:more than|over|greater than|above|exceeding)\s*\$?\s*(\d+(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
_EXTRACTED_PAGE_PATTERN = re.compile(r"\bpage:\s*(\d+)\b", re.IGNORECASE)
_EXTRACTED_PAGE_SIZE_PATTERN = re.compile(r"\bpage\s*size:\s*(\d+)\b|\bpageSize:\s*(\d+)\b", re.IGNORECASE)
_PLAIN_PAGE_PATTERN = re.compile(r"\bpage\s+(\d+)\b", re.IGNORECASE)
_PLAIN_PAGE_SIZE_PATTERN = re.compile(r"\b(?:show|list|give me)\s+(\d+)\b", re.IGNORECASE)
_NEXT_PAGE_PATTERN = re.compile(r"\b(next page|show more|more results|load more|see more)\b", re.IGNORECASE)
_MIN_PAGE_SIZE = 1
_DEFAULT_LIST_PAGE_SIZE = 10
_MAX_CUSTOMER_PAGE_SIZE = 25
_AGGREGATE_PAGE_SIZE = 250
_MAX_AGGREGATE_PAGES = 20
_EXTRACTED_ACCTCD_PATTERN = re.compile(r"\bacctCd\s*(?:id)?:\s*([A-Z0-9]{3,20})", re.IGNORECASE)
_EXTRACTED_CUSTOMER_ID_PATTERN = re.compile(r"\bcustomerId:\s*([A-Z0-9]{3,20})", re.IGNORECASE)

_TOPIC_KEYWORDS = {
    "SHIPMENT": [
        "shipment", "tracking", "ship", "delivery", "shipped",
        "carrier", "freight", "package", "track",
    ],
    "PRODUCT": [
        "product", "item", "sku", "inventory", "quantity", "unit",
        "what did i order", "items in",
    ],
    "ORDER": [
        "order", "status", "invoice", "payment", "total",
        "amount", "balance", "purchase",
    ],
    "CONTACT": [
        "contact", "phone", "email", "address", "location",
    ],
}


# ── GraphQL tool execution ────────────────────────────────────────────────────────────


def _select_all_for(*field_keys: str) -> dict[str, bool]:
    """Build a GraphQL `select` dict from logical field keys."""
    return {MINIERP_FIELDS[k]: True for k in field_keys}


def _where_eq(field_key: str, value: Any) -> dict[str, Any]:
    return {MINIERP_FIELDS[field_key]: value}


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


def _extract_order_number(question: str) -> str | None:
    extracted_match = _EXTRACTED_ORDER_ID_PATTERN.search(question or "")
    if extracted_match:
        return extracted_match.group(1).upper()
    labeled_match = _LABELED_ORDER_NUMBER_PATTERN.search(question or "")
    if labeled_match:
        return labeled_match.group(1).upper()
    match = _ORDER_NUMBER_PATTERN.search(question or "")
    return match.group(0).upper() if match else None


def _extract_shipment_number(question: str) -> str | None:
    extracted_match = _EXTRACTED_SHIPMENT_ID_PATTERN.search(question or "")
    if extracted_match:
        return extracted_match.group(1).upper()
    labeled_match = _LABELED_SHIPMENT_NUMBER_PATTERN.search(question or "")
    if labeled_match:
        return labeled_match.group(1).upper()
    match = _SHIPMENT_NUMBER_PATTERN.search(question or "")
    return match.group(0).upper() if match else None


def _extract_company_id(question: str) -> int | None:
    match = _COMPANY_ID_PATTERN.search(question or "")
    return int(match.group(1)) if match else None


def _extract_date_range(question: str) -> tuple[str | None, str | None]:
    match = _EXTRACTED_DATE_RANGE_PATTERN.search(question or "")
    if match:
        return match.group(1), match.group(2)

    month_match = _MONTH_YEAR_PATTERN.search(question or "")
    if not month_match:
        return None, None

    month_name_value, year_value = month_match.groups()
    month_number = list(month_name).index(month_name_value.capitalize())
    start = datetime(int(year_value), month_number, 1)
    if month_number == 12:
        end = datetime(int(year_value) + 1, 1, 1)
    else:
        end = datetime(int(year_value), month_number + 1, 1)
    end = end.replace(day=1) - datetime.resolution
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _extract_order_status(question: str) -> str | None:
    extracted_match = _EXTRACTED_STATUS_PATTERN.search(question or "")
    if extracted_match:
        return extracted_match.group(1).upper()

    q = (question or "").lower()
    if "open order" in q or "open orders" in q:
        return "N"
    if "completed order" in q or "completed orders" in q:
        return "C"
    return None


def _extract_min_total(question: str) -> float | None:
    match = _MIN_TOTAL_PATTERN.search(question or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_account_code(question: str) -> str | None:
    for pattern in (_EXTRACTED_ACCTCD_PATTERN, _EXTRACTED_CUSTOMER_ID_PATTERN):
        match = pattern.search(question or "")
        if match:
            return match.group(1).upper()
    return None


def classify_topic(question: str) -> str:
    q = (question or "").lower()

    if "invoice" in q and (
        _ORDER_NUMBER_PATTERN.search(question or "")
        or _SHIPMENT_NUMBER_PATTERN.search(question or "")
        or "shipment" in q
        or "tracking" in q
    ):
        return "SHIPMENT"

    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(keyword in q for keyword in keywords):
            return topic
    return "ORDER"


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
    return min(_MAX_CUSTOMER_PAGE_SIZE, max(_MIN_PAGE_SIZE, page_size))


def _extract_pagination(question: str) -> dict[str, Any]:
    text = question or ""
    page_match = _EXTRACTED_PAGE_PATTERN.search(text) or _PLAIN_PAGE_PATTERN.search(text)
    size_match = _EXTRACTED_PAGE_SIZE_PATTERN.search(text)
    plain_size_match = _PLAIN_PAGE_SIZE_PATTERN.search(text)

    page_size_value = None
    if size_match:
        page_size_value = next((group for group in size_match.groups() if group), None)
    elif plain_size_match:
        page_size_value = plain_size_match.group(1)

    return {
        "page": _clamp_page(page_match.group(1), 1) if page_match else None,
        "page_size": _clamp_page_size(page_size_value) if page_size_value else None,
        "wants_next": bool(_NEXT_PAGE_PATTERN.search(text)),
    }


def _resolve_pagination(
    question: str,
    scope: ScopeContext | None,
    intent: str,
) -> dict[str, int]:
    extracted = _extract_pagination(question)
    previous = (scope.last_pagination or {}).get(intent, {}) if scope else {}
    page_size = _clamp_page_size(
        extracted.get("page_size") or previous.get("pageSize") or _DEFAULT_LIST_PAGE_SIZE
    )

    if extracted.get("wants_next") and previous:
        page = _clamp_page(int(previous.get("page", 1)) + 1)
    else:
        page = _clamp_page(extracted.get("page") or 1)

    return {"page": page, "page_size": page_size}


def _json_tool_result(**payload: Any) -> str:
    return json.dumps(
        {
            "source": "miniERP",
            **payload,
        },
        ensure_ascii=True,
    )


def _try_json_tool_result(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _display_order_status(raw_status: Any) -> str:
    status = str(raw_status or "").upper()
    return {
        "N": "Open",
        "C": "Completed",
        "H": "Hold",
        "B": "Back Order",
        "S": "Shipping",
        "I": "Invoiced",
        "L": "Cancelled",
        "X": "Cancelled",
    }.get(status, str(raw_status or "?"))


def _summarize_order_snapshot(item: dict[str, Any]) -> str:
    return (
        f"{item.get(MINIERP_FIELDS['order_number'], '?')} "
        f"({_display_order_status(item.get(MINIERP_FIELDS['order_status']))}, "
        f"${float(item.get(MINIERP_FIELDS['order_total']) or 0):.2f}, "
        f"{item.get(MINIERP_FIELDS['created_date'], '?')})"
    )


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


def _select_order_highlights(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []

    selected: list[dict[str, Any]] = []
    seen_orders: set[str] = set()

    def _add(item: dict[str, Any]) -> None:
        order_number = str(item.get(MINIERP_FIELDS["order_number"]) or "")
        if not order_number or order_number in seen_orders:
            return
        seen_orders.add(order_number)
        selected.append(item)

    for item in items[:3]:
        _add(item)
    for item in items:
        if _display_order_status(item.get(MINIERP_FIELDS["order_status"])) not in {"Completed", "Cancelled"}:
            _add(item)
    for item in items[-3:]:
        _add(item)
    return selected


def _filter_items_by_region(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Post-filter a result list by companyID when REGION is set.

    Used for API endpoints that don't accept a companyId parameter (e.g.
    getSalesOrderDataByOrderNumber). The companyID field in those responses
    uses the key 'companyID' (capital I, capital D).
    """
    if REGION is None:
        return items
    allowed = set(_company_ids(None))
    return [
        item for item in items
        if _coerce_company_id(item.get("companyID")) in allowed
    ]


def _format_minierp_date(date_str: str, *, end_of_day: bool = False) -> str:
    if "T" in date_str:
        return date_str
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    suffix = "23:59:59Z" if end_of_day else "00:00:00Z"
    return f"{dt:%Y-%m-%d}T{suffix}"


async def _gql_baccounts_for_acct_cd(
    acct_cd: str,
    company_ids: list | None,
) -> list[dict[str, Any]]:
    # The customer ID the user provides (e.g. LUTEST1208, PRIN100) IS the
    # BAccount AcctCD. Resolve it by AcctCD only and request NO explicit select —
    # this mirrors the proven query (which omits `select` and lets the API return
    # all fields in `items`), so a wrong select field name can't break it.
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


# ── Order ownership enforcement ──────────────────────────────────────────────
# A verified customer may only look up orders linked to their own account. We
# resolve their AcctCD -> bAccountId(s) via BAccount, then confirm the order
# carries one of those ids as its customer (a `customerId IN [...]` WHERE filter
# on SOOrder — the same linkage get_customer_orders_data already uses). The
# check FAILS CLOSED: if the owner can't be resolved or the query errors, access
# is denied rather than risk leaking another customer's order.

# Tools whose result is keyed on an order number and therefore must be
# ownership-gated. Shipment-number lookups are NOT here — a shipment number has
# no direct customer linkage in this schema (see note in the diagnosis).
_ORDER_OWNERSHIP_TOOLS = {
    "get_order_level_details",
    "get_product_details_in_order",
    "get_shipping_and_tracking_details_using_order_number",
}

# Tools that return account-level data (orders list, totals, contacts,
# addresses). These may ONLY serve the verified customer in scope — never an id
# supplied in the tool args, and never a company-wide fallback. A verified
# customer must be present or the lookup is denied.
_CUSTOMER_SCOPED_TOOLS = {
    "get_customer_orders_data",
    "get_customer_order_total_value",
    "get_data_from_contact_table",
    "get_data_from_address_table",
}


async def _order_belongs_to_customer(
    order_number: str,
    company_id: int | None,
    baccount_ids: list[Any],
) -> bool:
    """True only if an order with this number is linked to one of baccount_ids."""
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
                return False  # fail closed
            if result.get("items"):
                return True
    return False


async def _query_order_header_once(
    order_number: str,
    company_id: int,
) -> dict[str, Any] | None:
    # Query the order header by order number + company only — never by/with
    # customer_id. The ElevenLabs reference (Elevenlabs-Order.ts) does the same:
    # SOOrder is looked up purely on orderNbr + companyId. Including customerId
    # in the select risks an "unknown field" error since its naming is
    # inconsistent across companies in the production schema.
    options = {
        "select": _select_all_for(
            "order_number", "order_status",
            "order_total", "created_date", "company_id",
        ),
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
                    _coerce_company_id(item.get(MINIERP_FIELDS["company_id"]))
                    or company_candidate
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
    fallback = {
        "items": [],
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
        "hasMore": False,
    }
    last_result = fallback
    for candidate in _identifier_candidates(order_number):
        for company_candidate in _company_lookup_candidates(company_id):
            result = await _query_order_lines_once(
                candidate,
                company_candidate,
                page=page,
                page_size=page_size,
                select_fields=select_fields,
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
        order_number,
        company_id,
        page=1,
        page_size=30,
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


def _format_pending_lines_text(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return []
    lines = [f"  Pending lines: {len(records)} unshipped line(s)"]
    for item in records[:5]:
        lines.append(
            "    "
            f"{item.get('description') or 'Unnamed line'} | "
            f"Ordered: {float(item.get('orderedQty') or 0):.2f} | "
            f"Shipped: {float(item.get('shippedQty') or 0):.2f}"
        )
    if len(records) > 5:
        lines.append(f"    ... {len(records) - 5} more pending line(s)")
    return lines


async def _resolve_shipping_lookup_by_order_number(order_number: str) -> tuple[list[dict[str, Any]], str | None]:
    for candidate in _identifier_candidates(order_number):
        result = await get_sales_order_data_by_order_number(candidate)
        items = _filter_items_by_region(result.get("items") or [])
        if items:
            return items, candidate
    return [], None


async def _resolve_shipping_lookup_by_shipment_number(shipment_number: str) -> tuple[list[dict[str, Any]], str | None]:
    for candidate in _identifier_candidates(shipment_number):
        result = await get_sales_order_data_by_order_number(
            candidate,
            field="shipmentNumber",
        )
        items = _filter_items_by_region(result.get("items") or [])
        if items:
            return items, candidate
    return [], None


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
            status="not_found",
            intent="customer_orders",
            message=f"No customer account found for {acct_cd}.",
            customerId=acct_cd,
            records=[],
        )

    where: dict[str, Any] = {
        MINIERP_FIELDS["customer_id"]: {"in": baccount_ids},
        MINIERP_FIELDS["company_id"]: {"in": _company_ids(company_ids)},
    }
    if start_date:
        where[MINIERP_FIELDS["created_date"]] = {
            "gte": _format_minierp_date(start_date),
        }
    if end_date:
        existing = where.get(MINIERP_FIELDS["created_date"], {})
        if isinstance(existing, dict):
            existing["lte"] = _format_minierp_date(end_date, end_of_day=True)
            where[MINIERP_FIELDS["created_date"]] = existing
        else:
            where[MINIERP_FIELDS["created_date"]] = {
                "lte": _format_minierp_date(end_date, end_of_day=True),
            }
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
            status="not_found",
            intent="customer_orders",
            message=f"No orders found for customer {acct_cd}.",
            customerId=acct_cd,
            filters={
                "startDate": start_date,
                "endDate": end_date,
                "status": _display_order_status(order_status) if order_status else None,
                "minTotal": min_total,
            },
            records=[],
            pagination={
                "page": result.get("page") or page,
                "pageSize": result.get("pageSize") or page_size,
                "returned": 0,
                "hasMore": bool(result.get("hasMore")),
            },
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
        {
            "orderNumber": it.get(f_num),
            "status": _display_order_status(it.get(f_st)),
            "statusCode": it.get(f_st),
            "total": float(it.get(f_tot) or 0),
            "date": it.get(f_date),
        }
        for it in items
    ]
    pagination = {
        "page": result.get("page") or page,
        "pageSize": result.get("pageSize") or page_size,
        "returned": len(records),
        "hasMore": bool(result.get("hasMore")),
    }
    return _json_tool_result(
        status="success",
        intent="customer_orders",
        message=(
            f"Showing {len(records)} order{'s' if len(records) != 1 else ''} "
            f"for {acct_cd}"
            f"{' filtered by ' + ', '.join(filter_labels) if filter_labels else ''}."
        ),
        customerId=acct_cd,
        filters={
            "startDate": start_date,
            "endDate": end_date,
            "status": _display_order_status(order_status) if order_status else None,
            "minTotal": min_total,
        },
        records=records,
        pagination=pagination,
        nextAction=(
            f"More orders are available. Ask whether to show page {pagination['page'] + 1}."
            if pagination["hasMore"]
            else None
        ),
    )


async def _gql_order_header(order_number: str, company_id: int | None = None) -> dict[str, Any] | None:
    item, _matched_order_number, _matched_company = await _resolve_order_header_lookup(order_number, company_id)
    return item


async def _gql_order_details(order_number: str, company_id: int | None) -> str:
    head, matched_order_number, matched_company = await _resolve_order_header_lookup(order_number, company_id)
    if not head:
        return f"Order {order_number} not found."

    pending_lines = await _pending_line_records_for_order(
        matched_order_number or order_number,
        matched_company,
        head.get(MINIERP_FIELDS["order_status"]),
    )
    lines = [
        f"Order {head.get(MINIERP_FIELDS['order_number'])}:",
        f"  Status    : {_display_order_status(head.get(MINIERP_FIELDS['order_status']))}",
        f"  Total     : ${float(head.get(MINIERP_FIELDS['order_total']) or 0):.2f}",
        f"  Date      : {head.get(MINIERP_FIELDS['created_date'])}",
    ]
    if matched_company is not None:
        lines.append(f"  Company   : {matched_company}")
    lines.extend(_format_pending_lines_text(pending_lines))
    return "\n".join(lines) + "\n"


async def _gql_order_items_inline(order_number: str) -> str:
    options = {
        "select": _select_all_for("product_name", "sku", "quantity", "unit_price", "line_total"),
        "where": {
            MINIERP_FIELDS["line_order_nbr"]: order_number,
            MINIERP_FIELDS["company_id"]: {"in": _company_ids(None)},
        },
        "page": 1,
        "pageSize": _DEFAULT_PAGE_SIZE,
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["sales_order_line"], options)
    items = result.get("items") or []
    if not items:
        return ""

    f_name = MINIERP_FIELDS["product_name"]
    f_sku  = MINIERP_FIELDS["sku"]
    f_qty  = MINIERP_FIELDS["quantity"]
    f_unit = MINIERP_FIELDS["unit_price"]
    f_tot  = MINIERP_FIELDS["line_total"]
    return "\n".join(
        f"    {str(it.get(f_name, '?')):<30} x{float(it.get(f_qty) or 0):>6.2f}  "
        f"@ ${float(it.get(f_unit) or 0):>8.2f}  = ${float(it.get(f_tot) or 0):>10.2f}"
        for it in items
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
        order_number,
        company_id,
        page=page,
        page_size=page_size,
        select_fields=("product_name", "sku", "quantity", "unit_price", "line_total"),
    )
    if not items:
        return _json_tool_result(
            status="not_found",
            intent="product_details_in_order",
            message=f"No line items found for order {order_number}.",
            orderNumber=order_number,
            records=[],
            pagination={
                "page": result.get("page") or page,
                "pageSize": result.get("pageSize") or page_size,
                "returned": 0,
                "hasMore": bool(result.get("hasMore")),
            },
        )

    f_name = MINIERP_FIELDS["product_name"]
    f_sku  = MINIERP_FIELDS["sku"]
    f_qty  = MINIERP_FIELDS["quantity"]
    f_unit = MINIERP_FIELDS["unit_price"]
    f_tot  = MINIERP_FIELDS["line_total"]
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

    pagination = {
        "page": result.get("page") or page,
        "pageSize": result.get("pageSize") or page_size,
        "returned": len(records),
        "hasMore": bool(result.get("hasMore")),
    }
    return _json_tool_result(
        status="success",
        intent="product_details_in_order",
        message=f"Showing {len(records)} item{'s' if len(records) != 1 else ''} from order {matched_order_number or order_number}.",
        orderNumber=matched_order_number or order_number,
        companyId=matched_company,
        records=records,
        pagination=pagination,
        enrichment={
            "inventoryItemLookup": "applied" if inventory else "not_available",
        },
        nextAction=(
            f"More line items are available. Ask whether to show page {pagination['page'] + 1}."
            if pagination["hasMore"]
            else None
        ),
    )


async def _gql_shipping_by_order(order_number: str, company_id: int | None = None) -> str:
    items, matched_order_number = await _resolve_shipping_lookup_by_order_number(order_number)
    if not items:
        return f"No shipment found for order {order_number}."
    order_head, _header_order_number, matched_company = await _resolve_order_header_lookup(
        matched_order_number or order_number,
        company_id,
    )
    pending_lines = await _pending_line_records_for_order(
        matched_order_number or order_number,
        matched_company,
        order_head.get(MINIERP_FIELDS["order_status"]) if order_head else None,
    )
    shipment_numbers = _dedupe_preserve_order([str(item.get("shipmentNumber") or "") for item in items])
    tracking_numbers = _dedupe_preserve_order([str(item.get("trackingNumber") or "") for item in items])
    invoice_numbers = _dedupe_preserve_order([str(item.get("invoiceNumber") or "") for item in items])
    summary = [
        f"Shipping lookup for order {matched_order_number or order_number}:",
        f"  Shipment numbers : {', '.join(shipment_numbers) if shipment_numbers else 'None found'}",
        f"  Tracking numbers : {', '.join(tracking_numbers) if tracking_numbers else 'No tracking number assigned yet'}",
        f"  Invoice numbers  : {', '.join(invoice_numbers) if invoice_numbers else 'No invoice linked yet'}",
    ]
    if order_head:
        summary.append(f"  Order status     : {_display_order_status(order_head.get(MINIERP_FIELDS['order_status']))}")
        summary.append(f"  Order date       : {order_head.get(MINIERP_FIELDS['created_date'])}")
        summary.append(f"  Order total      : ${float(order_head.get(MINIERP_FIELDS['order_total']) or 0):.2f}")
    if matched_company is not None:
        summary.append(f"  Company          : {matched_company}")
    summary.extend(_format_pending_lines_text(pending_lines))
    detail_lines = []
    for index, item in enumerate(items, start=1):
        fields = [
            f"shipment={str(item.get('shipmentNumber') or 'N/A')}",
            f"tracking={str(item.get('trackingNumber') or 'N/A')}",
            f"invoice={str(item.get('invoiceNumber') or 'N/A')}",
            f"company={str(item.get('companyID') or 'N/A')}",
        ]
        detail_lines.append(f"  {index}. " + "; ".join(fields))
    return (
        "\n".join(summary) + "\n"
        f"Linked shipment records for order {matched_order_number or order_number} ({len(items)} found):\n"
        + "\n".join(detail_lines)
    )


async def _gql_shipping_by_shipment(
    shipment_number: str,
    owner_baccount_ids: list[Any] | None = None,
) -> str:
    items, matched_shipment_number = await _resolve_shipping_lookup_by_shipment_number(shipment_number)
    if not items:
        return f"No shipment found for shipment number {shipment_number}."
    sales_orders = _dedupe_preserve_order([str(item.get("salesOrderNumber") or "") for item in items])

    # Ownership enforcement: a shipment number has no direct customer link, so we
    # resolve it through its sales order(s). The shipment is only released if at
    # least one linked sales order belongs to the verified customer. Fail closed:
    # no linked SO, or none owned, => denied.
    if owner_baccount_ids is not None:
        owned = any([
            await _order_belongs_to_customer(so, None, owner_baccount_ids)
            for so in sales_orders if so
        ])
        if not owned:
            return _json_tool_result(
                status="not_found",
                intent="shipment_tracking",
                message=f"I couldn't find shipment {matched_shipment_number or shipment_number} on your account.",
                shipmentNumber=matched_shipment_number or shipment_number,
            )
    tracking_numbers = _dedupe_preserve_order([str(item.get("trackingNumber") or "") for item in items])
    invoice_numbers = _dedupe_preserve_order([str(item.get("invoiceNumber") or "") for item in items])
    order_head = None
    matched_company = _coerce_company_id(items[0].get("companyID")) if items else None
    pending_lines: list[dict[str, Any]] = []
    if len(sales_orders) == 1:
        order_head, matched_order_number, matched_company = await _resolve_order_header_lookup(
            sales_orders[0],
            matched_company,
        )
        pending_lines = await _pending_line_records_for_order(
            matched_order_number or sales_orders[0],
            matched_company,
            order_head.get(MINIERP_FIELDS["order_status"]) if order_head else None,
        )
    lines = [
        f"  {str(item.get('salesOrderNumber') or 'N/A'):<16} | "
        f"{str(item.get('trackingNumber') or 'N/A'):<24} | "
        f"{str(item.get('invoiceNumber') or 'N/A'):<12} | "
        f"{str(item.get('companyID') or 'N/A')}"
        for item in items
    ]
    summary = [
        f"Shipment lookup for {matched_shipment_number or shipment_number}:",
        f"  Sales orders    : {', '.join(sales_orders) if sales_orders else 'None found'}",
        f"  Tracking numbers: {', '.join(tracking_numbers) if tracking_numbers else 'No tracking number assigned yet'}",
        f"  Invoice numbers : {', '.join(invoice_numbers) if invoice_numbers else 'No invoice linked yet'}",
    ]
    if order_head:
        summary.append(f"  Order status     : {_display_order_status(order_head.get(MINIERP_FIELDS['order_status']))}")
    if matched_company is not None:
        summary.append(f"  Company          : {matched_company}")
    summary.extend(_format_pending_lines_text(pending_lines))
    return (
        "\n".join(summary) + "\n"
        f"Tracking for shipment {matched_shipment_number or shipment_number} ({len(items)} found):\n"
        "  Sales Order      | Tracking                 | Invoice      | Company\n"
        "  " + "-" * 72 + "\n" + "\n".join(lines)
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
            status="not_found",
            intent="customer_order_total",
            message=f"No customer account found for {acct_cd}.",
            customerId=acct_cd,
        )

    where: dict[str, Any] = {
        MINIERP_FIELDS["customer_id"]: {"in": baccount_ids},
        MINIERP_FIELDS["company_id"]: {"in": _company_ids(company_ids)},
        MINIERP_FIELDS["order_status"]: "C",
    }
    if start_date:
        where[MINIERP_FIELDS["created_date"]] = {
            "gte": _format_minierp_date(start_date),
        }
    if end_date:
        existing = where.get(MINIERP_FIELDS["created_date"], {})
        if isinstance(existing, dict):
            existing["lte"] = _format_minierp_date(end_date, end_of_day=True)
            where[MINIERP_FIELDS["created_date"]] = existing
        else:
            where[MINIERP_FIELDS["created_date"]] = {
                "lte": _format_minierp_date(end_date, end_of_day=True),
            }

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
        status="success",
        intent="customer_order_total",
        message=(
            f"Calculated completed-order total for {acct_cd}."
            if is_complete
            else f"Calculated a partial completed-order total for {acct_cd}; more orders remain."
        ),
        customerId=acct_cd,
        filters={
            "startDate": start_date,
            "endDate": end_date,
            "status": "Completed",
        },
        orderCount=order_count,
        grandTotal=round(grand_total, 2),
        currency="USD",
        isComplete=is_complete,
        pagination={
            "pagesFetched": pages_fetched,
            "pageSize": _AGGREGATE_PAGE_SIZE,
            "hasMore": has_more,
        },
    )


async def _gql_contacts(
    acct_cd: str | None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
) -> str:
    baccount_ids = None
    baccounts: list[dict[str, Any]] = []
    if acct_cd:
        baccounts = await _gql_baccounts_for_acct_cd(acct_cd, None)
        baccount_ids = [
            item.get(MINIERP_FIELDS["baccount_id"])
            for item in baccounts
            if item.get(MINIERP_FIELDS["baccount_id"]) is not None
        ]
        if not baccount_ids:
            return _json_tool_result(
                status="not_found",
                intent="account_contacts",
                message=f"No customer account found for {acct_cd}.",
                customerId=acct_cd,
                records=[],
            )
    acct_name_by_baccount = {
        item.get(MINIERP_FIELDS["baccount_id"]): item.get(MINIERP_FIELDS["acct_name"])
        for item in baccounts
    }
    primary_contact_ids = _dedupe_preserve_order([
        str(item.get(MINIERP_FIELDS["primary_contact_id"]) or "")
        for item in baccounts
    ])

    options: dict[str, Any] = {
        "select": _select_all_for(
            "contact_id", "contact_name", "contact_email",
            "contact_phone", "contact_role", "contact_acct",
        ),
        "where": {MINIERP_FIELDS["company_id"]: {"in": _company_ids(None)}},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    if baccount_ids:
        options["where"][MINIERP_FIELDS["contact_acct"]] = {"in": baccount_ids}

    result = await find_with_offset_pagination(MINIERP_ENTITIES["contact"], options)
    items = result.get("items") or []
    if not items and primary_contact_ids:
        fallback_options: dict[str, Any] = {
            "select": _select_all_for(
                "contact_id", "contact_name", "contact_email",
                "contact_phone", "contact_role", "contact_acct",
            ),
            "where": {
                MINIERP_FIELDS["company_id"]: {"in": _company_ids(None)},
                MINIERP_FIELDS["contact_id"]: {"in": [int(cid) for cid in primary_contact_ids if cid.isdigit()]},
            },
            "page": _clamp_page(page),
            "pageSize": _clamp_page_size(page_size),
        }
        fallback_result = await find_with_offset_pagination(MINIERP_ENTITIES["contact"], fallback_options)
        items = fallback_result.get("items") or []
        result = fallback_result
    if not items:
        return _json_tool_result(
            status="not_found",
            intent="account_contacts",
            message=f"No contacts found{' for ' + acct_cd if acct_cd else ''}.",
            customerId=acct_cd,
            records=[],
            pagination={
                "page": result.get("page") or page,
                "pageSize": result.get("pageSize") or page_size,
                "returned": 0,
                "hasMore": bool(result.get("hasMore")),
            },
        )

    f_id    = MINIERP_FIELDS["contact_id"]
    f_name  = MINIERP_FIELDS["contact_name"]
    f_role  = MINIERP_FIELDS["contact_role"]
    f_email = MINIERP_FIELDS["contact_email"]
    f_phone = MINIERP_FIELDS["contact_phone"]
    records = []
    for item in items:
        records.append({
            "contactId": item.get(f_id),
            "name": item.get(f_name),
            "displayName": (
                acct_name_by_baccount.get(item.get(MINIERP_FIELDS["contact_acct"]))
                or item.get(f_name)
                or "Unnamed contact"
            ),
            "type": item.get(f_role),
            "email": item.get(f_email),
            "phone": item.get(f_phone),
        })
    pagination = {
        "page": result.get("page") or page,
        "pageSize": result.get("pageSize") or page_size,
        "returned": len(records),
        "hasMore": bool(result.get("hasMore")),
    }
    return _json_tool_result(
        status="success",
        intent="account_contacts",
        message=f"Showing {len(records)} contact{'s' if len(records) != 1 else ''}{' for ' + acct_cd if acct_cd else ''}.",
        customerId=acct_cd,
        records=records,
        pagination=pagination,
        nextAction=(
            f"More contacts are available. Ask whether to show page {pagination['page'] + 1}."
            if pagination["hasMore"]
            else None
        ),
    )


async def _gql_addresses(
    acct_cd: str | None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
) -> str:
    baccount_ids = None
    if acct_cd:
        baccount_ids = await _gql_baccount_ids_for_acct_cd(acct_cd, None)
        if not baccount_ids:
            return _json_tool_result(
                status="not_found",
                intent="account_addresses",
                message=f"No customer account found for {acct_cd}.",
                customerId=acct_cd,
                records=[],
            )

    options: dict[str, Any] = {
        "select": _select_all_for(
            "address_id", "address_type", "addr_line1",
            "addr_city", "addr_state", "addr_zip", "addr_country",
            "address_acct",
        ),
        "where": {MINIERP_FIELDS["company_id"]: {"in": _company_ids(None)}},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    if baccount_ids:
        options["where"][MINIERP_FIELDS["address_acct"]] = {"in": baccount_ids}

    result = await find_with_offset_pagination(MINIERP_ENTITIES["address"], options)
    items = result.get("items") or []
    if not items:
        return _json_tool_result(
            status="not_found",
            intent="account_addresses",
            message=f"No addresses found{' for ' + acct_cd if acct_cd else ''}.",
            customerId=acct_cd,
            records=[],
            pagination={
                "page": result.get("page") or page,
                "pageSize": result.get("pageSize") or page_size,
                "returned": 0,
                "hasMore": bool(result.get("hasMore")),
            },
        )

    f_id  = MINIERP_FIELDS["address_id"]
    f_t   = MINIERP_FIELDS["address_type"]
    f_l1  = MINIERP_FIELDS["addr_line1"]
    f_ci  = MINIERP_FIELDS["addr_city"]
    f_st  = MINIERP_FIELDS["addr_state"]
    f_zip = MINIERP_FIELDS["addr_zip"]
    f_co  = MINIERP_FIELDS["addr_country"]
    records = [
        {
            "addressId": item.get(f_id),
            "type": item.get(f_t),
            "street": item.get(f_l1),
            "city": item.get(f_ci),
            "state": item.get(f_st),
            "postalCode": item.get(f_zip),
            "country": item.get(f_co),
        }
        for item in items
    ]
    pagination = {
        "page": result.get("page") or page,
        "pageSize": result.get("pageSize") or page_size,
        "returned": len(records),
        "hasMore": bool(result.get("hasMore")),
    }
    return _json_tool_result(
        status="success",
        intent="account_addresses",
        message=f"Showing {len(records)} address{'es' if len(records) != 1 else ''}{' for ' + acct_cd if acct_cd else ''}.",
        customerId=acct_cd,
        records=records,
        pagination=pagination,
        nextAction=(
            f"More addresses are available. Ask whether to show page {pagination['page'] + 1}."
            if pagination["hasMore"]
            else None
        ),
    )


async def _run_graphql_tool(
    tool_name: str,
    tool_args: dict,
    scope: ScopeContext | None,
) -> str:
    """Dispatch a logical tool to the GraphQL miniERP."""
    acct_cd: str | None = scope.customer_id if scope else None
    company_ids: list = _company_ids((scope.company_ids if scope else None) or None)
    start_date, end_date = _extract_date_range(tool_args.get("query_text") or "")
    order_status = _extract_order_status(tool_args.get("query_text") or "")
    min_total = _extract_min_total(tool_args.get("query_text") or "")

    def _order_num() -> str:
        inp = tool_args.get("input", {})
        return inp.get("order_number") or tool_args.get("order_number")

    def _company_id() -> int | None:
        inp = tool_args.get("input", {})
        return inp.get("company_id") or tool_args.get("company_id")

    # ── Ownership gate ───────────────────────────────────────────────────────
    # [US-REMOVAL] Customer-ID verification disabled for order-number lookups.
    # Previously any order-number lookup required a verified customer ID in scope AND
    # verified that the order belonged to that customer; with no acct_cd it returned
    # "A verified customer ID is required before I can look up an order.", which is what
    # forced the bot to ask for a Customer ID on order checks. An order number alone is
    # now sufficient — the individual handlers below (get_order_level_details, etc.) only
    # need the order number and do not use acct_cd.
    # SECURITY NOTE: this removes the "does this order belong to the caller" check, so any
    # order can be looked up by its number. Restore the block below to re-enable ownership.
    #
    # if tool_name in _ORDER_OWNERSHIP_TOOLS:
    #     gated_order_number = _order_num()
    #     if gated_order_number:
    #         if not acct_cd:
    #             return _json_tool_result(
    #                 status="missing_identifier",
    #                 intent=tool_name,
    #                 message="A verified customer ID is required before I can look up an order.",
    #                 missingFields=["customerId"],
    #             )
    #         owner_baccount_ids = await _gql_baccount_ids_for_acct_cd(acct_cd, company_ids)
    #         if not await _order_belongs_to_customer(gated_order_number, _company_id(), owner_baccount_ids):
    #             return _json_tool_result(
    #                 status="not_found",
    #                 intent=tool_name,
    #                 message=f"I couldn't find order {gated_order_number} on your account.",
    #                 orderNumber=gated_order_number,
    #                 customerId=acct_cd,
    #             )

    # ── Customer-scope gate ──────────────────────────────────────────────────
    # Account-level tools serve ONLY the verified customer in scope. Require a
    # verified customer (no company-wide fallback); the id is forced to scope
    # below so an injected tool_args id cannot redirect the lookup.
    if tool_name in _CUSTOMER_SCOPED_TOOLS and not acct_cd:
        return _json_tool_result(
            status="missing_identifier",
            intent=tool_name,
            message="A verified customer ID is required before I can look up account data.",
            missingFields=["customerId"],
        )

    if tool_name == "get_customer_orders_data":
        cid = acct_cd  # forced to verified scope — ignore any injected override
        if not cid:
            return _json_tool_result(
                status="missing_identifier",
                intent="customer_orders",
                message="No customer ID in scope. Provide an account code to look up orders.",
                missingFields=["acctCd"],
            )
        cids = tool_args.get("companyId") or company_ids
        return await _gql_orders_for_customer(
            cid,
            cids,
            start_date=tool_args.get("start_date") or start_date,
            end_date=tool_args.get("end_date") or end_date,
            order_status=tool_args.get("order_status") or order_status,
            min_total=tool_args.get("min_total"),
            page=tool_args.get("page", 1),
            page_size=tool_args.get("page_size", _DEFAULT_LIST_PAGE_SIZE),
        )

    if tool_name == "get_order_level_details":
        order_number = _order_num()
        if not order_number:
            return "Missing required order_number. Ask the customer for the order number before looking up order details."
        return await _gql_order_details(order_number, _company_id())

    if tool_name == "get_product_details_in_order":
        order_number = _order_num()
        if not order_number:
            return _json_tool_result(
                status="missing_identifier",
                intent="product_details_in_order",
                message="Missing required order_number. Ask the customer for the order number before looking up product details in an order.",
                missingFields=["order_number"],
            )
        return await _gql_product_details_in_order(
            order_number,
            company_id=_company_id(),
            page=tool_args.get("page", 1),
            page_size=tool_args.get("page_size", _DEFAULT_LIST_PAGE_SIZE),
        )

    if tool_name == "get_shipping_and_tracking_details_using_order_number":
        order_number = _order_num()
        if not order_number:
            return "Missing required order_number. Ask the customer for the order number before looking up shipment tracking."
        return await _gql_shipping_by_order(order_number, _company_id())

    if tool_name == "get_shipping_tracking_details_using_shipment_number":
        snum = tool_args.get("shipment_number")
        if not snum:
            return "Missing required shipment_number. Ask the customer for the shipment number before looking up tracking."
        if not acct_cd:
            return _json_tool_result(
                status="missing_identifier",
                intent=tool_name,
                message="A verified customer ID is required before I can look up a shipment.",
                missingFields=["customerId"],
            )
        owner_baccount_ids = await _gql_baccount_ids_for_acct_cd(acct_cd, company_ids)
        return await _gql_shipping_by_shipment(snum, owner_baccount_ids=owner_baccount_ids)

    if tool_name == "get_customer_order_total_value":
        cid = acct_cd  # forced to verified scope — ignore any injected override
        if not cid:
            return _json_tool_result(
                status="missing_identifier",
                intent="customer_order_total",
                message="No customer ID in scope. Provide an account code to calculate totals.",
                missingFields=["acctCd"],
            )
        cids = tool_args.get("company_ids") or company_ids
        return await _gql_customer_order_total(
            cid, cids,
            start_date=tool_args.get("start_date"),
            end_date=tool_args.get("end_date"),
        )

    if tool_name == "get_data_from_contact_table":
        return await _gql_contacts(
            acct_cd,
            page=tool_args.get("page", 1),
            page_size=tool_args.get("page_size", _DEFAULT_LIST_PAGE_SIZE),
        )

    if tool_name == "get_data_from_address_table":
        return await _gql_addresses(
            acct_cd,
            page=tool_args.get("page", 1),
            page_size=tool_args.get("page_size", _DEFAULT_LIST_PAGE_SIZE),
        )

    return f"[GraphQL] Unknown tool '{tool_name}'"


# ── Deterministic tool selection (simulates LLM choice) ──────────────────────────────


def _pick_tool(topic: str, question: str) -> tuple[str, dict]:
    q = question.lower()
    order_number = _extract_order_number(question)
    shipment_number = _extract_shipment_number(question)
    company_id = _extract_company_id(question)
    start_date, end_date = _extract_date_range(question)
    order_status = _extract_order_status(question)
    min_total = _extract_min_total(question)

    if topic == "SHIPMENT":
        if shipment_number:
            return "get_shipping_tracking_details_using_shipment_number", {
                "shipment_number": shipment_number,
            }
        if not order_number:
            return "get_shipping_and_tracking_details_using_order_number", {
                "input": {"order_number": None, "company_id": company_id}
            }
        return "get_shipping_and_tracking_details_using_order_number", {
            "input": {"order_number": order_number, "company_id": company_id}
        }

    if topic == "PRODUCT":
        return "get_product_details_in_order", {
            "input": {"order_number": order_number, "company_id": company_id,
                      "page": 1, "page_size": 40}
        }

    if topic == "ORDER":
        is_pagination_followup = bool(_NEXT_PAGE_PATTERN.search(question) or _PLAIN_PAGE_PATTERN.search(question))
        is_filtered_order_list = (
            order_status is not None
            or min_total is not None
            or start_date is not None
            or end_date is not None
        )
        asks_for_aggregate_total = (
            ("total spend" in q)
            or ("total value" in q)
            or ("how much have i spent" in q)
            or ("how much did" in q and "spend" in q)
        )
        if asks_for_aggregate_total:
            return "get_customer_order_total_value", {
                "acctCd": None, "company_ids": None,
                "start_date": None, "end_date": None,
            }
        if order_number and not is_filtered_order_list and not is_pagination_followup:
            return "get_order_level_details", {
                "input": {"order_number": order_number, "company_id": company_id}
            }
        if (
            "customer" in q
            or "orders for" in q
            or "my orders" in q
            or "account " in q
            or is_filtered_order_list
            or is_pagination_followup
        ):
            return "get_customer_orders_data", {
                "value": None, "get_data_by": "acctCd", "companyId": [2, 11],
                "start_date": start_date,
                "end_date": end_date,
                "order_status": order_status,
                "min_total": min_total,
                "query_text": question,
            }
        return "get_order_level_details", {
            "input": {"order_number": order_number, "company_id": company_id}
        }

    if topic == "CONTACT":
        if "address" in q:
            return "get_data_from_address_table", {}
        return "get_data_from_contact_table", {}

    return "get_order_level_details", {
        "input": {"order_number": order_number, "company_id": company_id}
    }


# ── Public entry point ────────────────────────────────────────────────────────────────


async def query_agent(question: str, scope_context: ScopeContext | None = None) -> str:
    """Direct query agent backed by production miniERP GraphQL."""

    # 1. Build ScopeContext for this request only.

    extracted_cid = _extract_account_code(question)
    if extracted_cid:
        if scope_context is None:
            scope_context = ScopeContext(customer_id=extracted_cid)
        elif not scope_context.customer_id:
            scope_context.customer_id = extracted_cid

    _t_sql = time.time()
    _t = time.time()
    topic = classify_topic(question)
    print(f"[TIMER][SQL] Topic classify: {time.time() - _t:.3f}s  -> {topic}")
    if scope_context is None:
        scope_context = ScopeContext(topic=topic)
    else:
        scope_context.topic = topic

    # 2. Tool selection
    _t = time.time()
    selected_tool, tool_args = _pick_tool(topic, question)
    paginated_intents = {
        "get_customer_orders_data": "customer_orders",
        "get_product_details_in_order": "product_details_in_order",
        "get_data_from_contact_table": "account_contacts",
        "get_data_from_address_table": "account_addresses",
    }
    if selected_tool in paginated_intents:
        tool_args = {
            **tool_args,
            **_resolve_pagination(question, scope_context, paginated_intents[selected_tool]),
        }
    print(f"[TIMER][SQL] Tool select:    {time.time() - _t:.3f}s  -> {selected_tool}")


    # 3. GraphQL query
    try:
        _t = time.time()
        erp_result = await _run_graphql_tool(selected_tool, tool_args, scope_context)
        print(f"[TIMER][SQL] GraphQL query:  {time.time() - _t:.3f}s")
    except Exception as exc:
        print(f"[TIMER][SQL] ERROR ({type(exc).__name__}): {exc}")
        # Catch-all so NO lookup failure ever escapes as a raw crash. The narrow
        # (GraphQLConfigError, GraphQLQueryError) tuple let GraphQLAuthError and
        # AttributeError/KeyError (e.g. a null/empty response from the order
        # lookup) slip through to the orchestrator, where they surfaced as
        # "Error querying database: ..." and Pass 2 rendered the generic
        # "temporary system issue" message. Degrade gracefully instead.
        erp_result = _json_tool_result(
            status="error",
            intent=selected_tool,
            errorCode=type(exc).__name__,
            message="The lookup could not be completed.",
            details=str(exc),
        )
    print(f"[TIMER][SQL] TOTAL:          {time.time() - _t_sql:.3f}s")
    return erp_result
