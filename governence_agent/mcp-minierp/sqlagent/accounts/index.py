"""Accounts-domain miniERP tools: contacts, addresses, customer billing profile.

Split out of the original mcp-minierp (2026-07) for the same reason as
mcp-minierp-orders: this is a shared data domain (BAccount/Customer/Contact/
Address), not any one department's private backend.
"""

from __future__ import annotations

import json
import os
from typing import Any

from minierp_core import find_with_offset_pagination

# "CA" -> company 11 only, "US" -> company 2 only, None -> both.
# MINIERP_REGION overrides the default below; set it to "ALL" (or "") to see
# both companies instead of Canada-only.
_REGION_ENV = os.getenv("MINIERP_REGION", "CA")
REGION: str | None = None if _REGION_ENV in ("", "ALL") else _REGION_ENV

MINIERP_ENTITIES: dict[str, str] = {
    "baccount": "baccount",
    "contact":  "contact",
    "address":  "address",
    "customer": "Customer",
}

MINIERP_FIELDS: dict[str, str] = {
    # Contact
    "contact_id":    "contactId",
    "contact_name":  "fullName",
    "contact_email": "eMail",
    "contact_phone": "phone1",
    "contact_role":  "contactType",
    "contact_acct":  "bAccountId",
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
    "company_id":         "companyId",
    # Customer (billing/credit profile on top of BAccount; keyed by bAccountId)
    "credit_limit":              "creditLimit",
    "credit_rule":               "creditRule",
    "credit_days_past_due":      "creditDaysPastDue",
    "terms_id":                  "termsId",
    "default_payment_method_id": "defPaymentMethodId",
    "statement_cycle_id":        "statementCycleId",
    "statement_type":            "statementType",
    "customer_category":        "customerCategory",
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


# ── Public tools ─────────────────────────────────────────────────────────────


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
                status="not_found", intent="account_contacts",
                message=f"No customer account found for {acct_cd}.", customerId=acct_cd, records=[],
            )
    acct_name_by_baccount = {
        item.get(MINIERP_FIELDS["baccount_id"]): item.get(MINIERP_FIELDS["acct_name"])
        for item in baccounts
    }
    primary_contact_ids = _dedupe_preserve_order([
        str(item.get(MINIERP_FIELDS["primary_contact_id"]) or "") for item in baccounts
    ])

    options: dict[str, Any] = {
        "select": _select_all_for(
            "contact_id", "contact_name", "contact_email", "contact_phone", "contact_role", "contact_acct",
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
                "contact_id", "contact_name", "contact_email", "contact_phone", "contact_role", "contact_acct",
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
            status="not_found", intent="account_contacts",
            message=f"No contacts found{' for ' + acct_cd if acct_cd else ''}.", customerId=acct_cd, records=[],
            pagination={"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                        "returned": 0, "hasMore": bool(result.get("hasMore"))},
        )

    f_id = MINIERP_FIELDS["contact_id"]
    f_name = MINIERP_FIELDS["contact_name"]
    f_role = MINIERP_FIELDS["contact_role"]
    f_email = MINIERP_FIELDS["contact_email"]
    f_phone = MINIERP_FIELDS["contact_phone"]
    records = []
    for item in items:
        records.append({
            "contactId": item.get(f_id),
            "name": item.get(f_name),
            "displayName": (
                acct_name_by_baccount.get(item.get(MINIERP_FIELDS["contact_acct"]))
                or item.get(f_name) or "Unnamed contact"
            ),
            "type": item.get(f_role),
            "email": item.get(f_email),
            "phone": item.get(f_phone),
        })
    pagination = {"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                  "returned": len(records), "hasMore": bool(result.get("hasMore"))}
    return _json_tool_result(
        status="success", intent="account_contacts",
        message=f"Showing {len(records)} contact{'s' if len(records) != 1 else ''}{' for ' + acct_cd if acct_cd else ''}.",
        customerId=acct_cd, records=records, pagination=pagination,
        nextAction=(f"More contacts are available. Ask whether to show page {pagination['page'] + 1}."
                    if pagination["hasMore"] else None),
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
                status="not_found", intent="account_addresses",
                message=f"No customer account found for {acct_cd}.", customerId=acct_cd, records=[],
            )

    options: dict[str, Any] = {
        "select": _select_all_for(
            "address_id", "address_type", "addr_line1", "addr_city", "addr_state", "addr_zip",
            "addr_country", "address_acct",
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
            status="not_found", intent="account_addresses",
            message=f"No addresses found{' for ' + acct_cd if acct_cd else ''}.", customerId=acct_cd, records=[],
            pagination={"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                        "returned": 0, "hasMore": bool(result.get("hasMore"))},
        )

    f_id = MINIERP_FIELDS["address_id"]
    f_t = MINIERP_FIELDS["address_type"]
    f_l1 = MINIERP_FIELDS["addr_line1"]
    f_ci = MINIERP_FIELDS["addr_city"]
    f_st = MINIERP_FIELDS["addr_state"]
    f_zip = MINIERP_FIELDS["addr_zip"]
    f_co = MINIERP_FIELDS["addr_country"]
    records = [
        {"addressId": item.get(f_id), "type": item.get(f_t), "street": item.get(f_l1),
         "city": item.get(f_ci), "state": item.get(f_st), "postalCode": item.get(f_zip),
         "country": item.get(f_co)}
        for item in items
    ]
    pagination = {"page": result.get("page") or page, "pageSize": result.get("pageSize") or page_size,
                  "returned": len(records), "hasMore": bool(result.get("hasMore"))}
    return _json_tool_result(
        status="success", intent="account_addresses",
        message=f"Showing {len(records)} address{'es' if len(records) != 1 else ''}{' for ' + acct_cd if acct_cd else ''}.",
        customerId=acct_cd, records=records, pagination=pagination,
        nextAction=(f"More addresses are available. Ask whether to show page {pagination['page'] + 1}."
                    if pagination["hasMore"] else None),
    )


async def _gql_customer_profile(acct_cd: str) -> str:
    """Billing/credit profile from the Customer table (extends BAccount).

    Resolved the same way every account-scoped tool resolves ownership:
    acct_cd -> BAccount -> bAccountId(s) -> filter Customer by that id. Never
    trust a bAccountId supplied directly by a caller.
    """
    baccount_ids = await _gql_baccount_ids_for_acct_cd(acct_cd, None)
    if not baccount_ids:
        return _json_tool_result(
            status="not_found", intent="customer_profile",
            message=f"No customer account found for {acct_cd}.", customerId=acct_cd,
        )

    options = {
        "select": _select_all_for(
            "credit_limit", "credit_rule", "credit_days_past_due", "terms_id",
            "default_payment_method_id", "statement_cycle_id", "statement_type", "customer_category",
        ),
        "where": {
            MINIERP_FIELDS["baccount_id"]: {"in": baccount_ids},
            MINIERP_FIELDS["company_id"]: {"in": _company_ids(None)},
        },
        "page": 1,
        "pageSize": 1,
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["customer"], options)
    items = result.get("items") or []
    if not items:
        return _json_tool_result(
            status="not_found", intent="customer_profile",
            message=f"No billing profile found for {acct_cd}.", customerId=acct_cd,
        )

    item = items[0]
    return _json_tool_result(
        status="success", intent="customer_profile",
        customerId=acct_cd,
        creditLimit=float(item.get(MINIERP_FIELDS["credit_limit"]) or 0),
        creditRule=item.get(MINIERP_FIELDS["credit_rule"]),
        creditDaysPastDue=item.get(MINIERP_FIELDS["credit_days_past_due"]),
        termsId=item.get(MINIERP_FIELDS["terms_id"]),
        defaultPaymentMethodId=item.get(MINIERP_FIELDS["default_payment_method_id"]),
        statementCycleId=item.get(MINIERP_FIELDS["statement_cycle_id"]),
        statementType=item.get(MINIERP_FIELDS["statement_type"]),
        customerCategory=item.get(MINIERP_FIELDS["customer_category"]),
    )
