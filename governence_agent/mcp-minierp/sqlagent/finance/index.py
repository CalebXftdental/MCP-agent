"""Finance-domain miniERP tools: AP invoices, vendors, PO headers, GL account
transactions. Backed by the production miniERP GraphQL API via
sqlagent.graphql_client, same as mcp-minierp.

Scope note: this backend is company-wide (not region-restricted like
mcp-minierp's CA-only default) -- finance needs to see both company 2 (US) and
company 11 (CA) legal entities. Every tool accepts an optional company_id and
otherwise searches both.

Access note: as of this build, the credential behind this backend can read
Account, GLTran, Branch, APInvoice, Vendor, POOrder, ARSalesPrice. It cannot
read APBill, VendorClass, POLine, POReceipt (confirmed FORBIDDEN by direct
probe against db-api.frontierdental.com) -- so there is no line-item detail
for AP invoices or POs yet, and no payment-record table. Tools below are
scoped to what's actually reachable; extend MINIERP_ENTITIES/MINIERP_FIELDS
and add tools once those tables are granted.

ARSalesPrice note (2026-08): confirmed reachable ONLY under the "admin"
credential profile bound in this module (find_with_offset_pagination below) --
the default/calsoft profile used by mcp-minierp's orders/accounts/shipments
tools gets FORBIDDEN on this table. Same profile as every other tool in this
file, so nothing extra to configure; just don't move get_sales_price to a
different domain module without also moving the admin-profile binding.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from minierp_core import find_with_offset_pagination as _find_with_offset_pagination

# Finance uses the higher-privilege "administrator" miniERP account (AR/AP/GL/PO/
# Vendor). Bind every query in this domain to the "admin" credential profile
# (MINIERP_ADMIN_USERNAME/PASSWORD), so it works from the consolidated single-
# process server where per-process env can't select the account.
async def find_with_offset_pagination(table, options=None):
    return await _find_with_offset_pagination(table, options, profile="admin")

MINIERP_ENTITIES: dict[str, str] = {
    "baccount":  "baccount",
    "vendor":    "Vendor",
    "ap_invoice": "APInvoice",
    "po_order":  "POOrder",
    "account":   "Account",
    "gl_tran":   "GLTran",
    "ar_invoice": "ARInvoice",
    "ar_sales_price": "ARSalesPrice",
}

MINIERP_FIELDS: dict[str, str] = {
    # BAccount (vendor code resolution -- same table/fields as mcp-minierp)
    "baccount_id": "bAccountId",
    "acct_cd":     "AcctCD",
    "company_id":  "companyId",
    # Vendor (keyed by bAccountId, same identity as BAccount)
    "vendor_class_id":     "vendorClassId",
    "vendor_terms_id":     "termsId",
    "vendor_cury_id":      "curyId",
    "vendor_payment_method": "paymentMethodId",
    "vendor_1099":         "vendor1099",
    "vendor_retainage":    "retainageApply",
    # APInvoice
    "ap_ref_nbr":        "refNbr",
    "ap_doc_type":       "docType",
    "ap_invoice_date":   "invoiceDate",
    "ap_invoice_nbr":    "invoiceNbr",
    "ap_due_date":       "dueDate",
    "ap_line_total":     "curyLineTotal",
    "ap_tax_total":      "curyTaxTotal",
    "ap_pay_date":       "payDate",
    "ap_pay_type_id":    "payTypeId",
    "ap_terms_id":       "termsId",
    "ap_vendor_id":      "suppliedByVendorId",
    # POOrder
    "po_order_nbr":     "orderNbr",
    "po_status":        "status",
    "po_order_date":    "orderDate",
    "po_expected_date": "expectedDate",
    "po_order_total":   "curyOrderTotal",
    "po_vendor_id":     "vendorId",
    "po_ship_via":      "shipVia",
    "po_hold":          "hold",
    # Account (GL chart of accounts)
    "gl_account_id":    "accountId",
    "gl_account_cd":    "accountCd",
    "gl_description":   "description",
    "gl_account_type":  "type",
    "gl_active":        "active",
    # GLTran
    "gltran_account_id": "accountId",
    "gltran_module":     "module",
    "gltran_batch_nbr":  "batchNbr",
    "gltran_tran_date":  "tranDate",
    "gltran_debit_amt":  "debitAmt",
    "gltran_credit_amt": "creditAmt",
    "gltran_tran_desc":  "tranDesc",
    "gltran_ref_nbr":    "refNbr",
    # ARInvoice -- NOTE: no customer/bAccount link field exists on this table
    # (confirmed by probing "customerId"/"bAccountId"/"customerID"/"custId" --
    # all rejected as invalid columns). Lookup is by refNbr only, same trust
    # model as the order-number/PO-number/AP-invoice-number tools: no
    # ownership check, callers must already know the invoice number.
    "ar_ref_nbr":            "refNbr",
    "ar_doc_type":           "docType",
    "ar_invoice_date":       "invoiceDate",
    "ar_invoice_nbr":        "invoiceNbr",
    "ar_line_total":         "curyLineTotal",
    "ar_tax_total":          "curyTaxTotal",
    "ar_payment_total":      "curyPaymentTotal",
    "ar_unpaid_balance":     "curyUnpaidBalance",
    "ar_terms_id":           "termsId",
    "ar_credit_hold":        "creditHold",
    "ar_payment_method_id":  "paymentMethodId",
    # ARSalesPrice -- one row per price tier (price class / customer / break
    # quantity combination), unlike the header tables above.
    "sp_inventory_id":       "inventoryId",
    "sp_customer_id":        "customerId",
    "sp_cust_price_class_id": "custPriceClassId",
    "sp_sales_price":        "salesPrice",
    "sp_cury_id":            "curyId",
    "sp_uom":                "uom",
    "sp_effective_date":     "effectiveDate",
    "sp_expiration_date":    "expirationDate",
    "sp_price_type":         "priceType",
    "sp_break_qty":          "breakQty",
}

_DEFAULT_LIST_PAGE_SIZE = 10
_MAX_LIST_PAGE_SIZE = 25
_MAX_AGGREGATE_PAGES = 20
_AGGREGATE_PAGE_SIZE = 250


def _select_all_for(*field_keys: str) -> dict[str, bool]:
    return {MINIERP_FIELDS[k]: True for k in field_keys}


def _company_ids(company_id: int | None) -> list[int]:
    """Finance is company-wide: search both legal entities unless one is given."""
    if company_id is not None:
        try:
            return [int(company_id)]
        except (TypeError, ValueError):
            pass
    return [2, 11]


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
    return min(_MAX_LIST_PAGE_SIZE, max(1, page_size))


def _normalize_identifier(value: str | None) -> str | None:
    normalized = re.sub(r"[\s-]+", "", str(value or "").strip()).upper()
    return normalized or None


def _identifier_candidates(value: str | None) -> list[str]:
    raw = str(value or "").strip().upper()
    normalized = _normalize_identifier(value)
    candidates: list[str] = []
    for candidate in (raw, normalized):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _json_tool_result(**payload: Any) -> str:
    return json.dumps({"source": "miniERP-finance", **payload}, ensure_ascii=True)


def _format_date(date_str: str, *, end_of_day: bool = False) -> str:
    if "T" in date_str:
        return date_str
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    suffix = "23:59:59Z" if end_of_day else "00:00:00Z"
    return f"{dt:%Y-%m-%d}T{suffix}"


# ── BAccount resolution (vendor code -> bAccountId, same table as mcp-minierp) ──


async def _resolve_baccount_id(acct_cd: str) -> int | None:
    for candidate in _identifier_candidates(acct_cd):
        options = {
            "where": {
                MINIERP_FIELDS["acct_cd"]: candidate,
                MINIERP_FIELDS["company_id"]: {"in": _company_ids(None)},
            },
            "page": 1,
            "pageSize": 1,
        }
        result = await find_with_offset_pagination(MINIERP_ENTITIES["baccount"], options)
        items = result.get("items") or []
        if items:
            return items[0].get(MINIERP_FIELDS["baccount_id"])
    return None


# ── Vendor ────────────────────────────────────────────────────────────────────


async def get_vendor_details(vendor_code: str) -> str:
    baccount_id = await _resolve_baccount_id(vendor_code)
    if baccount_id is None:
        return _json_tool_result(
            status="not_found", intent="vendor_details",
            message=f"No vendor account found for {vendor_code}.",
            vendorCode=vendor_code,
        )

    options = {
        "select": _select_all_for(
            "vendor_class_id", "vendor_terms_id", "vendor_cury_id",
            "vendor_payment_method", "vendor_1099", "vendor_retainage",
        ),
        "where": {MINIERP_FIELDS["baccount_id"]: baccount_id},
        "page": 1,
        "pageSize": 1,
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["vendor"], options)
    items = result.get("items") or []
    if not items:
        return _json_tool_result(
            status="not_found", intent="vendor_details",
            message=f"No vendor profile found for {vendor_code}.",
            vendorCode=vendor_code,
        )

    item = items[0]
    return _json_tool_result(
        status="success", intent="vendor_details",
        vendorCode=vendor_code,
        vendorClassId=item.get(MINIERP_FIELDS["vendor_class_id"]),
        termsId=item.get(MINIERP_FIELDS["vendor_terms_id"]),
        curyId=item.get(MINIERP_FIELDS["vendor_cury_id"]),
        paymentMethodId=item.get(MINIERP_FIELDS["vendor_payment_method"]),
        vendor1099=bool(item.get(MINIERP_FIELDS["vendor_1099"])),
        retainageApply=bool(item.get(MINIERP_FIELDS["vendor_retainage"])),
    )


async def get_vendor_ap_invoices(
    vendor_code: str, page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE
) -> str:
    baccount_id = await _resolve_baccount_id(vendor_code)
    if baccount_id is None:
        return _json_tool_result(
            status="not_found", intent="vendor_ap_invoices",
            message=f"No vendor account found for {vendor_code}.",
            vendorCode=vendor_code, records=[],
        )

    options = {
        "select": _select_all_for(
            "ap_ref_nbr", "ap_doc_type", "ap_invoice_date", "ap_invoice_nbr",
            "ap_due_date", "ap_line_total", "ap_tax_total", "ap_pay_date",
        ),
        "where": {MINIERP_FIELDS["ap_vendor_id"]: baccount_id},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["ap_invoice"], options)
    items = result.get("items") or []
    if not items:
        return _json_tool_result(
            status="not_found", intent="vendor_ap_invoices",
            message=f"No AP invoices found for vendor {vendor_code}.",
            vendorCode=vendor_code, records=[],
        )

    f = MINIERP_FIELDS
    records = [
        {
            "invoiceNumber": it.get(f["ap_ref_nbr"]),
            "docType": it.get(f["ap_doc_type"]),
            "invoiceDate": it.get(f["ap_invoice_date"]),
            "dueDate": it.get(f["ap_due_date"]),
            "lineTotal": float(it.get(f["ap_line_total"]) or 0),
            "taxTotal": float(it.get(f["ap_tax_total"]) or 0),
            "paid": it.get(f["ap_pay_date"]) is not None,
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
        status="success", intent="vendor_ap_invoices",
        vendorCode=vendor_code, records=records, pagination=pagination,
    )


# ── APInvoice (header lookup by ref number -- not vendor-ownership-gated) ───────


async def get_ap_invoice_details(invoice_number: str, company_id: int | None = None) -> str:
    """Header-level AP invoice lookup by refNbr.

    Not gated to a specific vendor's identity by design -- callers must already
    know the invoice number, same trust model as mcp-minierp's order-number
    tools. If this needs vendor-ownership enforcement later, do it the same way
    get_order_details does: resolve the caller's own vendor bAccountId and
    compare against suppliedByVendorId before returning.
    """
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(invoice_number):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for(
                    "ap_ref_nbr", "ap_doc_type", "ap_invoice_date", "ap_invoice_nbr",
                    "ap_due_date", "ap_line_total", "ap_tax_total", "ap_pay_date",
                    "ap_pay_type_id", "ap_terms_id", "ap_vendor_id",
                ),
                "where": {
                    MINIERP_FIELDS["ap_ref_nbr"]: candidate,
                    MINIERP_FIELDS["company_id"]: company_candidate,
                },
                "page": 1,
                "pageSize": 1,
            }
            result = await find_with_offset_pagination(MINIERP_ENTITIES["ap_invoice"], options)
            items = result.get("items") or []
            if items:
                it = items[0]
                return _json_tool_result(
                    status="success", intent="ap_invoice_details",
                    invoiceNumber=it.get(f["ap_ref_nbr"]),
                    docType=it.get(f["ap_doc_type"]),
                    invoiceDate=it.get(f["ap_invoice_date"]),
                    dueDate=it.get(f["ap_due_date"]),
                    lineTotal=float(it.get(f["ap_line_total"]) or 0),
                    taxTotal=float(it.get(f["ap_tax_total"]) or 0),
                    paid=it.get(f["ap_pay_date"]) is not None,
                    paymentTypeId=it.get(f["ap_pay_type_id"]),
                    termsId=it.get(f["ap_terms_id"]),
                    vendorBAccountId=it.get(f["ap_vendor_id"]),
                    company=company_candidate,
                )
    return _json_tool_result(
        status="not_found", intent="ap_invoice_details",
        message=f"AP invoice {invoice_number} not found.",
        invoiceNumber=invoice_number,
    )


# ── ARInvoice (header lookup by ref number -- not customer-ownership-gated) ────
# Moved here from mcp-minierp-orders/-accounts during the 2026-07 domain split:
# ARInvoice is AR/finance-module data, same table family as APInvoice/GLTran
# above, not sales-order data. Still used by customer_service (narrow grant on
# just this tool, see governance_core/policy/departments.py) to answer
# "what's my balance" -- the tool itself has no notion of departments.


async def get_invoice_details(invoice_number: str, company_id: int | None = None) -> str:
    """Header-level AR invoice lookup by refNbr.

    Not gated to a specific customer's identity by design -- ARInvoice has no
    customer/bAccount link field in this schema (confirmed by probe), so
    ownership can't be verified the way order-number tools verify it. Callers
    must already know the invoice number."""
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(invoice_number):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for(
                    "ar_ref_nbr", "ar_doc_type", "ar_invoice_date", "ar_invoice_nbr",
                    "ar_line_total", "ar_tax_total", "ar_payment_total",
                    "ar_unpaid_balance", "ar_terms_id", "ar_credit_hold",
                    "ar_payment_method_id",
                ),
                "where": {
                    MINIERP_FIELDS["ar_ref_nbr"]: candidate,
                    MINIERP_FIELDS["company_id"]: company_candidate,
                },
                "page": 1,
                "pageSize": 1,
            }
            result = await find_with_offset_pagination(MINIERP_ENTITIES["ar_invoice"], options)
            items = result.get("items") or []
            if items:
                it = items[0]
                return _json_tool_result(
                    status="success", intent="invoice_details",
                    invoiceNumber=it.get(f["ar_ref_nbr"]),
                    docType=it.get(f["ar_doc_type"]),
                    invoiceDate=it.get(f["ar_invoice_date"]),
                    invoiceNbr=it.get(f["ar_invoice_nbr"]),
                    lineTotal=float(it.get(f["ar_line_total"]) or 0),
                    taxTotal=float(it.get(f["ar_tax_total"]) or 0),
                    paymentTotal=float(it.get(f["ar_payment_total"]) or 0),
                    unpaidBalance=float(it.get(f["ar_unpaid_balance"]) or 0),
                    termsId=it.get(f["ar_terms_id"]),
                    creditHold=bool(it.get(f["ar_credit_hold"])),
                    paymentMethodId=it.get(f["ar_payment_method_id"]),
                    company=company_candidate,
                )
    return _json_tool_result(
        status="not_found", intent="invoice_details",
        message=f"Invoice {invoice_number} not found.",
        invoiceNumber=invoice_number,
    )


# ── POOrder (header lookup by order number) ─────────────────────────────────────


async def get_po_order_status(po_number: str, company_id: int | None = None) -> str:
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(po_number):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for(
                    "po_order_nbr", "po_status", "po_order_date", "po_expected_date",
                    "po_order_total", "po_vendor_id", "po_ship_via", "po_hold",
                ),
                "where": {
                    MINIERP_FIELDS["po_order_nbr"]: candidate,
                    MINIERP_FIELDS["company_id"]: company_candidate,
                },
                "page": 1,
                "pageSize": 1,
            }
            result = await find_with_offset_pagination(MINIERP_ENTITIES["po_order"], options)
            items = result.get("items") or []
            if items:
                it = items[0]
                return _json_tool_result(
                    status="success", intent="po_order_status",
                    orderNumber=it.get(f["po_order_nbr"]),
                    orderStatus=it.get(f["po_status"]),
                    orderDate=it.get(f["po_order_date"]),
                    expectedDate=it.get(f["po_expected_date"]),
                    orderTotal=float(it.get(f["po_order_total"]) or 0),
                    vendorBAccountId=it.get(f["po_vendor_id"]),
                    shipVia=it.get(f["po_ship_via"]),
                    onHold=bool(it.get(f["po_hold"])),
                    company=company_candidate,
                )
    return _json_tool_result(
        status="not_found", intent="po_order_status",
        message=f"PO {po_number} not found.",
        orderNumber=po_number,
    )


# ── GL account transactions (client-side net movement -- API has no aggregate) ─


async def get_gl_account_transactions(
    account_cd: str,
    start_date: str | None = None,
    end_date: str | None = None,
    company_id: int | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
) -> str:
    f = MINIERP_FIELDS
    account_id: int | None = None
    matched_company: int | None = None
    for candidate in _identifier_candidates(account_cd):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for("gl_account_id", "gl_account_cd", "gl_description"),
                # accountCd is a fixed-width, space-padded field ("11110CAD  ")
                # -- exact match against a user-typed code would almost never
                # hit, so match by prefix instead.
                "where": {
                    MINIERP_FIELDS["gl_account_cd"]: {"startsWith": candidate},
                    MINIERP_FIELDS["company_id"]: company_candidate,
                },
                "page": 1,
                "pageSize": 1,
            }
            result = await find_with_offset_pagination(MINIERP_ENTITIES["account"], options)
            items = result.get("items") or []
            if items:
                account_id = items[0].get(f["gl_account_id"])
                matched_company = company_candidate
                break
        if account_id is not None:
            break

    if account_id is None:
        return _json_tool_result(
            status="not_found", intent="gl_account_transactions",
            message=f"GL account {account_cd} not found.",
            accountCd=account_cd, records=[],
        )

    where: dict[str, Any] = {
        MINIERP_FIELDS["gltran_account_id"]: account_id,
        MINIERP_FIELDS["company_id"]: matched_company,
    }
    if start_date:
        where[MINIERP_FIELDS["gltran_tran_date"]] = {"gte": _format_date(start_date)}
    if end_date:
        existing = where.get(MINIERP_FIELDS["gltran_tran_date"], {})
        if isinstance(existing, dict):
            existing["lte"] = _format_date(end_date, end_of_day=True)
            where[MINIERP_FIELDS["gltran_tran_date"]] = existing
        else:
            where[MINIERP_FIELDS["gltran_tran_date"]] = {"lte": _format_date(end_date, end_of_day=True)}

    options = {
        "select": _select_all_for(
            "gltran_tran_date", "gltran_module", "gltran_batch_nbr",
            "gltran_debit_amt", "gltran_credit_amt", "gltran_tran_desc", "gltran_ref_nbr",
        ),
        "where": where,
        "orderBy": {MINIERP_FIELDS["gltran_tran_date"]: "DESC"},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["gl_tran"], options)
    items = result.get("items") or []
    if not items:
        return _json_tool_result(
            status="not_found", intent="gl_account_transactions",
            message=f"No transactions found for {account_cd} in the given range.",
            accountCd=account_cd, records=[],
        )

    records = [
        {
            "date": it.get(f["gltran_tran_date"]),
            "module": it.get(f["gltran_module"]),
            "batchNbr": it.get(f["gltran_batch_nbr"]),
            "refNbr": it.get(f["gltran_ref_nbr"]),
            "description": it.get(f["gltran_tran_desc"]),
            "debit": float(it.get(f["gltran_debit_amt"]) or 0),
            "credit": float(it.get(f["gltran_credit_amt"]) or 0),
        }
        for it in items
    ]
    net_movement = round(sum(r["debit"] - r["credit"] for r in records), 2)
    pagination = {
        "page": result.get("page") or page,
        "pageSize": result.get("pageSize") or page_size,
        "returned": len(records),
        "hasMore": bool(result.get("hasMore")),
    }
    return _json_tool_result(
        status="success", intent="gl_account_transactions",
        accountCd=account_cd,
        company=matched_company,
        filters={"startDate": start_date, "endDate": end_date},
        records=records,
        netMovementThisPage=net_movement,
        pagination=pagination,
        note=(
            "netMovementThisPage sums only the returned page; paginate through "
            "hasMore for a full-range total."
            if pagination["hasMore"] else None
        ),
    )


# ── ARSalesPrice (list by inventory item -- multiple price tiers per item) ─────


async def get_sales_price(
    inventory_id: str,
    cust_price_class_id: str | None = None,
    customer_id: str | None = None,
    company_id: int | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
) -> str:
    """List sales price records for one inventory item.

    Unlike the header lookups above, ARSalesPrice legitimately returns more
    than one row per item (one per price class / customer / break-quantity
    tier), so this is a paginated list, not a single-record fetch. Narrow with
    cust_price_class_id and/or customer_id when the caller knows which tier
    they want."""
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(inventory_id):
        where: dict[str, Any] = {f["sp_inventory_id"]: candidate}
        if cust_price_class_id:
            where[f["sp_cust_price_class_id"]] = cust_price_class_id.strip().upper()
        if customer_id:
            where[f["sp_customer_id"]] = customer_id.strip()
        if company_id is not None:
            where[MINIERP_FIELDS["company_id"]] = company_id

        options = {
            "select": _select_all_for(
                "sp_inventory_id", "sp_customer_id", "sp_cust_price_class_id",
                "sp_sales_price", "sp_cury_id", "sp_uom",
                "sp_effective_date", "sp_expiration_date",
                "sp_price_type", "sp_break_qty",
            ),
            "where": where,
            "page": _clamp_page(page),
            "pageSize": _clamp_page_size(page_size),
        }
        result = await find_with_offset_pagination(MINIERP_ENTITIES["ar_sales_price"], options)
        items = result.get("items") or []
        if not items:
            continue

        records = [
            {
                "inventoryId": it.get(f["sp_inventory_id"]),
                "customerId": it.get(f["sp_customer_id"]),
                "custPriceClassId": it.get(f["sp_cust_price_class_id"]),
                "salesPrice": float(it.get(f["sp_sales_price"]) or 0),
                "curyId": it.get(f["sp_cury_id"]),
                "uom": it.get(f["sp_uom"]),
                "effectiveDate": it.get(f["sp_effective_date"]),
                "expirationDate": it.get(f["sp_expiration_date"]),
                "priceType": it.get(f["sp_price_type"]),
                "breakQty": it.get(f["sp_break_qty"]),
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
            status="success", intent="sales_price",
            inventoryId=inventory_id, records=records, pagination=pagination,
        )
    return _json_tool_result(
        status="not_found", intent="sales_price",
        message=f"No sales price records found for inventory item {inventory_id}.",
        inventoryId=inventory_id, records=[],
    )
