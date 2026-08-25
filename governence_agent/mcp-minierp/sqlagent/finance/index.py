"""Finance-domain miniERP tools: AP invoices, vendors, PO headers, GL account
transactions. Backed by the production miniERP GraphQL API via
sqlagent.graphql_client, same as mcp-minierp.

Scope note: this backend is company-wide (not region-restricted like
mcp-minierp's CA-only default) -- finance needs to see both company 2 (US) and
company 11 (CA) legal entities. Every tool accepts an optional company_id and
otherwise searches both.

Access note: as of this build, the credential behind this backend can read
Account, GLTran, Branch, APInvoice, Vendor, POOrder, ARSalesPrice, POLine,
ARAdjust, APAdjust. It cannot read APBill, VendorClass, POReceipt (confirmed
FORBIDDEN by direct probe against db-api.frontierdental.com). Tools below are
scoped to what's actually reachable; extend MINIERP_ENTITIES/MINIERP_FIELDS
and add tools once those tables are granted.

POLine/ARAdjust/APAdjust note (2026-08-17): re-probed directly against
db-api.frontierdental.com -- this module's original note (above) that POLine
was FORBIDDEN is now stale; it's granted under this same "admin" profile.
ARPayment/APPayment were also probed and ARE reachable, but carry no amount
or customer/vendor link field in this schema (only cash-account/deposit/card
metadata) -- useless for "who paid what, how much." The actual invoice-to-
payment application data (which invoice, which payment doc, what amount, and
critically customerId/vendorId directly on the row) lives on ARAdjust/APAdjust
instead (Acumatica's adjustment/application DACs: "adjd*" fields describe the
document being paid/adjusted, "adjg*" fields describe the payment/credit memo
applying it). get_ar_payment_history/get_ap_payment_history below are built on
*Adjust, not *Payment, for that reason.

ARSalesPrice note (2026-08): confirmed reachable ONLY under the "admin"
credential profile bound in this module (find_with_offset_pagination below) --
the default/calsoft profile used by mcp-minierp's orders/accounts/shipments
tools gets FORBIDDEN on this table. Same profile as every other tool in this
file, so nothing extra to configure; just don't move get_sales_price to a
different domain module without also moving the admin-profile binding.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from minierp_core import fetch_page_or_all as _fetch_page_or_all
from minierp_core import find_with_offset_pagination as _find_with_offset_pagination

from sqlagent.finance.schemas import (
    ApInvoiceDetailsResult,
    ApInvoicesDueSoonResult,
    ApPaymentHistoryResult,
    ArInvoicesPastDueResult,
    ArPaymentHistoryResult,
    BillLineItemsResult,
    CustomerInvoiceHistoryResult,
    GlAccountTransactionsResult,
    GlPeriodSummaryResult,
    InvoiceDetailsResult,
    InvoiceLineItemsResult,
    ItemMovementHistoryResult,
    PoLineItemsResult,
    PoOrderStatusResult,
    SalesPriceResult,
    VendorApInvoicesResult,
    VendorDetailsResult,
)

# Finance uses the higher-privilege "administrator" miniERP account (AR/AP/GL/PO/
# Vendor). Bind every query in this domain to the "admin" credential profile
# (MINIERP_ADMIN_USERNAME/PASSWORD), so it works from the consolidated single-
# process server where per-process env can't select the account.
async def find_with_offset_pagination(table, options=None):
    return await _find_with_offset_pagination(table, options, profile="admin")


async def fetch_page_or_all(table, options, *, fetch_all, page, page_size):
    return await _fetch_page_or_all(
        table, options, fetch_all=fetch_all, page=page, page_size=page_size,
        profile="admin", agg_page_size=_AGGREGATE_PAGE_SIZE, max_pages=_MAX_AGGREGATE_PAGES,
    )

MINIERP_ENTITIES: dict[str, str] = {
    "baccount":  "baccount",
    "vendor":    "Vendor",
    "ap_invoice": "APInvoice",
    "po_order":  "POOrder",
    "account":   "Account",
    "gl_tran":   "GLTran",
    "ar_invoice": "ARInvoice",
    "ar_sales_price": "ARSalesPrice",
    "po_line": "POLine",
    "ar_adjust": "ARAdjust",
    "ap_adjust": "APAdjust",
    "ar_tran": "ARTran",
    "ap_tran": "APTran",
    "so_invoice": "SOInvoice",
    "gl_history": "GLHistory",
    "in_tran": "INTran",
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
    # POLine -- confirmed by direct sampling that orderNbr (not poNbr, which is
    # null/unused in this tenant's data) is the join key back to POOrder.orderNbr.
    "pol_order_nbr":     "orderNbr",
    "pol_line_nbr":      "lineNbr",
    "pol_inventory_id":  "inventoryId",
    "pol_descr":         "tranDesc",
    "pol_order_qty":     "orderQty",
    "pol_received_qty":  "receivedQty",
    "pol_billed_qty":    "billedQty",
    "pol_open_qty":      "openQty",
    "pol_unit_cost":     "curyUnitCost",
    "pol_ext_cost":      "curyExtCost",
    "pol_uom":           "uom",
    "pol_promised_date": "promisedDate",
    "pol_closed":        "closed",
    "pol_cancelled":     "cancelled",
    "pol_completed":     "completed",
    # ARAdjust (invoice-to-payment application; customerId confirmed present
    # directly on this table even though ARInvoice itself has no customer link)
    "ara_customer_id":      "customerId",
    "ara_invoice_ref_nbr":  "adjdRefNbr",
    "ara_invoice_doc_type": "adjdDocType",
    "ara_payment_ref_nbr":  "adjgRefNbr",
    "ara_payment_doc_type": "adjgDocType",
    "ara_amount_applied":   "curyAdjdAmt",
    "ara_invoice_date":     "adjdDocDate",
    "ara_payment_date":     "adjgDocDate",
    "ara_released":         "released",
    "ara_voided":           "voided",
    "ara_hold":             "hold",
    # APAdjust (bill-to-payment application; vendorId confirmed present directly)
    "apa_vendor_id":          "vendorId",
    "apa_invoice_ref_nbr":    "adjdRefNbr",
    "apa_invoice_doc_type":   "adjdDocType",
    "apa_payment_ref_nbr":    "adjgRefNbr",
    "apa_payment_doc_type":   "adjgDocType",
    "apa_amount_applied":     "curyAdjdAmt",
    "apa_invoice_date":       "adjdDocDate",
    "apa_payment_date":       "adjgDocDate",
    "apa_released":           "released",
    "apa_voided":             "voided",
    "apa_hold":               "hold",
    "apa_payment_method_id":  "paymentMethodId",
    # ARTran (billed line detail for one AR invoice -- confirmed 2026-08-17
    # reachable; salesPersonId lives directly on the row, no separate
    # SalesPerson/EPEmployee lookup needed for rep attribution)
    "art_ref_nbr":          "refNbr",
    "art_line_nbr":         "lineNbr",
    "art_tran_type":        "tranType",
    "art_inventory_id":     "inventoryId",
    "art_descr":            "tranDesc",
    "art_qty":              "qty",
    "art_unit_price":       "unitPrice",
    "art_ext_price":        "curyExtPrice",
    "art_tax_category_id":  "taxCategoryId",
    "art_sales_person_id":  "salesPersonId",
    # APTran (billed line detail for one AP bill -- mirrors ARTran)
    "apt_ref_nbr":         "refNbr",
    "apt_line_nbr":        "lineNbr",
    "apt_tran_type":       "tranType",
    "apt_inventory_id":    "inventoryId",
    "apt_descr":           "tranDesc",
    "apt_qty":             "qty",
    "apt_unit_cost":       "unitCost",
    "apt_line_amt":        "curyLineAmt",
    "apt_tax_category_id": "taxCategoryId",
    "apt_po_nbr":          "poNbr",
    # SOInvoice -- confirmed 2026-08-17 to carry customerId directly, unlike
    # ARInvoice (see get_invoice_details' docstring for that gap). refNbr joins
    # to ARInvoice/ARAdjust.refNbr; soOrderNbr joins to SOOrder.orderNbr.
    "soi_ref_nbr":            "refNbr",
    "soi_doc_type":           "docType",
    "soi_customer_id":        "customerId",
    "soi_order_nbr":          "soOrderNbr",
    "soi_payment_amt":        "curyPaymentAmt",
    "soi_payment_method_id":  "paymentMethodId",
    # GLHistory (real period-level balances -- confirmed 2026-08-17; no
    # client-side aggregation over GLTran needed for a period summary)
    "glh_account_id":    "accountId",
    "glh_fin_period_id": "finPeriodId",
    "glh_ledger_id":     "ledgerId",
    "glh_sub_id":        "subId",
    "glh_balance_type":  "balanceType",
    "glh_beg_balance":   "finBegBalance",
    "glh_ptd_debit":     "finPtdDebit",
    "glh_ptd_credit":    "finPtdCredit",
    "glh_ytd_balance":   "finYtdBalance",
    # INTran -- inventory transaction history (receipts/issues/transfers).
    # Confirmed 2026-08-17: carries lotSerialNbr/expireDate per movement even
    # though INLotSerStatus (live lot status) is FORBIDDEN -- this is
    # transaction history, not current lot status; most sampled items have no
    # lot data at all (blank/null), which is real, not a bug. See
    # get_item_movement_history's own docstring for the framing this requires.
    "int_ref_nbr":         "refNbr",
    "int_tran_type":       "tranType",
    "int_doc_type":        "docType",
    "int_inventory_id":    "inventoryId",
    "int_qty":             "qty",
    "int_lot_serial_nbr":  "lotSerialNbr",
    "int_expire_date":     "expireDate",
    "int_tran_date":       "tranDate",
    "int_site_id":         "siteId",
    "int_location_id":     "locationId",
    "int_so_order_nbr":    "soOrderNbr",
    "int_po_receipt_nbr":  "poReceiptNbr",
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


def _clamp_bulk_page_size(value: Any, default: int = _AGGREGATE_PAGE_SIZE) -> int:
    """Same shape as _clamp_page_size, but for the company-wide bulk tools
    (get_ap_invoices_due_soon/get_ar_invoices_past_due), which reasonably want
    a bigger single-page ceiling than a per-record list tool -- matches their
    existing _AGGREGATE_PAGE_SIZE default rather than _MAX_LIST_PAGE_SIZE."""
    try:
        page_size = int(value)
    except (TypeError, ValueError):
        return default
    return min(_AGGREGATE_PAGE_SIZE, max(1, page_size))


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


async def get_vendor_details(vendor_code: str) -> VendorDetailsResult:
    baccount_id = await _resolve_baccount_id(vendor_code)
    if baccount_id is None:
        return VendorDetailsResult(
            status="not_found",
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
        return VendorDetailsResult(
            status="not_found",
            message=f"No vendor profile found for {vendor_code}.",
            vendorCode=vendor_code,
        )

    item = items[0]
    return VendorDetailsResult(
        status="success",
        vendorCode=vendor_code,
        vendorClassId=item.get(MINIERP_FIELDS["vendor_class_id"]),
        termsId=item.get(MINIERP_FIELDS["vendor_terms_id"]),
        curyId=item.get(MINIERP_FIELDS["vendor_cury_id"]),
        paymentMethodId=item.get(MINIERP_FIELDS["vendor_payment_method"]),
        vendor1099=bool(item.get(MINIERP_FIELDS["vendor_1099"])),
        retainageApply=bool(item.get(MINIERP_FIELDS["vendor_retainage"])),
    )


async def get_vendor_ap_invoices(
    vendor_code: str, page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    fetch_all: bool = False,
) -> VendorApInvoicesResult:
    """fetch_all=True returns this vendor's complete AP invoice history in one
    governed call (internally pages up to _MAX_AGGREGATE_PAGES real pages of
    _AGGREGATE_PAGE_SIZE each) instead of the caller looping page=1,2,3...
    itself -- prefer this over manual re-paging when the goal is "all of this
    vendor's invoices," not one page. `truncated=true` means even that cap
    wasn't enough; `page`/`page_size` are ignored when fetch_all is set."""
    baccount_id = await _resolve_baccount_id(vendor_code)
    if baccount_id is None:
        return VendorApInvoicesResult(
            status="not_found",
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
    items, pagination, truncated = await fetch_page_or_all(
        MINIERP_ENTITIES["ap_invoice"], options, fetch_all=fetch_all, page=page, page_size=page_size,
    )
    if not items:
        return VendorApInvoicesResult(
            status="not_found",
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
    return VendorApInvoicesResult(
        status="success",
        vendorCode=vendor_code, records=records, pagination=pagination, truncated=truncated,
    )


# ── APInvoice (header lookup by ref number -- not vendor-ownership-gated) ───────


async def get_ap_invoice_details(invoice_number: str, company_id: int | None = None) -> ApInvoiceDetailsResult:
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
                return ApInvoiceDetailsResult(
                    status="success",
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
    return ApInvoiceDetailsResult(
        status="not_found",
        message=f"AP invoice {invoice_number} not found.",
        invoiceNumber=invoice_number,
    )


# ── ARInvoice (header lookup by ref number -- not customer-ownership-gated) ────
# Moved here from mcp-minierp-orders/-accounts during the 2026-07 domain split:
# ARInvoice is AR/finance-module data, same table family as APInvoice/GLTran
# above, not sales-order data. Still used by customer_service (narrow grant on
# just this tool, see governance_core/policy/departments.py) to answer
# "what's my balance" -- the tool itself has no notion of departments.


async def get_invoice_details(invoice_number: str, company_id: int | None = None) -> InvoiceDetailsResult:
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
                return InvoiceDetailsResult(
                    status="success",
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
    return InvoiceDetailsResult(
        status="not_found",
        message=f"Invoice {invoice_number} not found.",
        invoiceNumber=invoice_number,
    )


# ── POOrder (header lookup by order number) ─────────────────────────────────────


async def get_po_order_status(po_number: str, company_id: int | None = None) -> PoOrderStatusResult:
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
                return PoOrderStatusResult(
                    status="success",
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
    return PoOrderStatusResult(
        status="not_found",
        message=f"PO {po_number} not found.",
        orderNumber=po_number,
    )


# ── GL account transactions (client-side net movement -- API has no aggregate) ─


async def _resolve_account_id(account_cd: str, company_id: int | None) -> tuple[int | None, int | None]:
    """Resolve a GL account code (prefix match, since accountCd is fixed-width
    and space-padded -- e.g. "11110CAD  " -- so an exact match against a
    user-typed code would almost never hit) to (accountId, matchedCompanyId).
    Shared by get_gl_account_transactions and get_gl_period_summary."""
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(account_cd):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for("gl_account_id", "gl_account_cd", "gl_description"),
                "where": {
                    f["gl_account_cd"]: {"startsWith": candidate},
                    f["company_id"]: company_candidate,
                },
                "page": 1,
                "pageSize": 1,
            }
            result = await find_with_offset_pagination(MINIERP_ENTITIES["account"], options)
            items = result.get("items") or []
            if items:
                return items[0].get(f["gl_account_id"]), company_candidate
    return None, None


async def get_gl_account_transactions(
    account_cd: str,
    start_date: str | None = None,
    end_date: str | None = None,
    company_id: int | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    fetch_all: bool = False,
) -> GlAccountTransactionsResult:
    """fetch_all=True accumulates every real transaction in range (up to the
    aggregate cap) in one governed call instead of paging manually; note that
    netMovementThisPage then sums the FULL accumulated range, not one page."""
    f = MINIERP_FIELDS
    account_id, matched_company = await _resolve_account_id(account_cd, company_id)

    if account_id is None:
        return GlAccountTransactionsResult(
            status="not_found",
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
    items, pagination, truncated = await fetch_page_or_all(
        MINIERP_ENTITIES["gl_tran"], options, fetch_all=fetch_all, page=page, page_size=page_size,
    )
    if not items:
        return GlAccountTransactionsResult(
            status="not_found",
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
    if pagination is not None:
        note = (
            "netMovementThisPage sums only the returned page; paginate through "
            "hasMore for a full-range total."
            if pagination["hasMore"] else None
        )
    else:
        note = (
            "netMovementThisPage sums every row fetch_all accumulated, not just one "
            "page; truncated=true means even that wasn't the full range."
            if truncated else None
        )
    return GlAccountTransactionsResult(
        status="success",
        accountCd=account_cd,
        company=matched_company,
        filters={"startDate": start_date, "endDate": end_date},
        records=records,
        netMovementThisPage=net_movement,
        pagination=pagination,
        note=note,
        truncated=truncated,
    )


# ── ARSalesPrice (list by inventory item -- multiple price tiers per item) ─────


async def get_sales_price(
    inventory_id: str,
    cust_price_class_id: str | None = None,
    customer_id: str | None = None,
    company_id: int | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    fetch_all: bool = False,
) -> SalesPriceResult:
    """List sales price records for one inventory item.

    Unlike the header lookups above, ARSalesPrice legitimately returns more
    than one row per item (one per price class / customer / break-quantity
    tier), so this is a paginated list, not a single-record fetch. Narrow with
    cust_price_class_id and/or customer_id when the caller knows which tier
    they want. fetch_all=True returns every matching price record in one
    governed call instead of paging manually."""
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
        items, pagination, truncated = await fetch_page_or_all(
            MINIERP_ENTITIES["ar_sales_price"], options, fetch_all=fetch_all, page=page, page_size=page_size,
        )
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
        return SalesPriceResult(
            status="success",
            inventoryId=inventory_id, records=records, pagination=pagination, truncated=truncated,
        )
    return SalesPriceResult(
        status="not_found",
        message=f"No sales price records found for inventory item {inventory_id}.",
        inventoryId=inventory_id, records=[],
    )


# ── Bulk/cross-record analytics (mirrors mcp-minierp/sqlagent/analytics.py's
# get_top_customers_by_spend/get_customer_order_recency pattern: page the raw
# table directly, aggregate/join client-side, in ONE governed call -- not a
# loop calling the single-record tools above once per vendor/invoice. See the
# module note at the top of this file re: which tables are actually reachable
# under the admin credential profile.) ────────────────────────────────────────


async def get_ap_invoices_due_soon(
    days_ahead: int = 14, company_id: int | None = None,
    page: int = 1, page_size: int = _AGGREGATE_PAGE_SIZE,
) -> ApInvoicesDueSoonResult:
    """AP invoices due within the next N days, across EVERY vendor -- an
    AP-aging / due-soon signal for a "My Workflow" filter node to act on.

    Unlike get_vendor_ap_invoices (one vendor per call), this pages the
    APInvoice table directly by due date, company-wide, then joins vendor
    code/name for whichever vendors appear -- no rollup needed since each
    invoice is already one row (unlike get_customer_order_recency, which
    rolls many orders up into one row per customer).

    Returns candidate rows only, `paid` included but NOT filtered out --
    deciding "unpaid AND due soon" is exactly what a filter node is for; this
    tool's job is fetching a bounded, real candidate set, not deciding.

    ONE real page per call (honest page/pageSize/hasMore, same contract as
    every other list tool in this file) -- NOT an auto-exhausting aggregate.
    That was this tool's behavior until 2026-08-21 (silently fetching up to
    5000 rows regardless of what was asked, with `page` meaning "start
    scanning from real offset (page-1)*250" rather than "give me chunk N") --
    confirmed by direct benchmark to be both a real correctness bug (page=1
    and page=2 covered overlapping ranges) and the root cause of a local
    copilot model's futile page-1,2,3... retry loop trying to reconstruct a
    truncated result (STAGE2_PLAN.md SS11.2/11.3). To get the FULL dataset in
    one governed call (a workflow-graph tool_call node, or any caller that
    genuinely needs everything, not a copilot sample), set `paginate: true`
    on the node -- the interpreter's existing generic exhaustion mechanism
    (_exhaust_tool_call) now works correctly against this tool's real
    `pagination.hasMore`, which it could not before this fix.
    """
    f = MINIERP_FIELDS
    now = datetime.utcnow()
    end = now + timedelta(days=max(0, int(days_ahead or 0)))
    options = {
        "select": _select_all_for(
            "ap_ref_nbr", "ap_doc_type", "ap_invoice_date", "ap_invoice_nbr",
            "ap_due_date", "ap_line_total", "ap_tax_total", "ap_pay_date", "ap_vendor_id",
        ),
        "where": {
            f["ap_due_date"]: {
                "gte": _format_date(now.strftime("%Y-%m-%d")),
                "lte": _format_date(end.strftime("%Y-%m-%d"), end_of_day=True),
            },
            f["company_id"]: {"in": _company_ids(company_id)},
        },
        "page": _clamp_page(page),
        "pageSize": _clamp_bulk_page_size(page_size),
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["ap_invoice"], options)
    items = result.get("items") or []
    if not items:
        return ApInvoicesDueSoonResult(status="not_found", invoices=[], pagination=None)

    vendor_ids = sorted({it.get(f["ap_vendor_id"]) for it in items if it.get(f["ap_vendor_id"]) is not None})
    vmap: dict = {}
    if vendor_ids:
        # "acctCd"/"acctName" (lowercase) -- confirmed by live probe to be the
        # real baccount field names under BOTH credential profiles. Deliberately
        # NOT reusing this file's own MINIERP_FIELDS["acct_cd"] ("AcctCD",
        # differently cased) -- that key is only ever used for an equality
        # WHERE filter elsewhere in this file (_resolve_baccount_id), never as
        # a SELECT projection, so its casing was never verified for that purpose.
        vres = await find_with_offset_pagination(MINIERP_ENTITIES["baccount"], {
            "select": {f["baccount_id"]: True, "acctCd": True, "acctName": True},
            "where": {f["baccount_id"]: {"in": vendor_ids}},
            "page": 1, "pageSize": max(len(vendor_ids), 10),
        })
        vmap = {v.get(f["baccount_id"]): v for v in vres.get("items", [])}

    invoices = [
        {
            "invoiceNumber": it.get(f["ap_ref_nbr"]),
            "docType": it.get(f["ap_doc_type"]),
            "invoiceDate": it.get(f["ap_invoice_date"]),
            "dueDate": it.get(f["ap_due_date"]),
            "lineTotal": float(it.get(f["ap_line_total"]) or 0),
            "taxTotal": float(it.get(f["ap_tax_total"]) or 0),
            "paid": it.get(f["ap_pay_date"]) is not None,
            # vendor may be absent from vmap if the baccount lookup's own page
            # cap (pageSize=len(vendor_ids)) somehow missed it -- code defensively.
            "vendorCode": (vmap.get(it.get(f["ap_vendor_id"])) or {}).get("acctCd"),
            "vendorName": (vmap.get(it.get(f["ap_vendor_id"])) or {}).get("acctName"),
        }
        for it in items
    ]
    pagination = {
        "page": result.get("page") or page,
        "pageSize": result.get("pageSize") or page_size,
        "returned": len(invoices),
        "hasMore": bool(result.get("hasMore")),
    }
    return ApInvoicesDueSoonResult(status="ok", invoices=invoices, pagination=pagination)


async def get_ar_invoices_past_due(
    min_invoice_age_days: int = 30, company_id: int | None = None,
    page: int = 1, page_size: int = _AGGREGATE_PAGE_SIZE,
) -> ArInvoicesPastDueResult:
    """AR invoices older than N days that may still be outstanding, across
    every customer -- an AR-aging / collections signal for a filter node to
    act on.

    NAMED "min_invoice_age_days", NOT "days overdue" or "days past due" --
    confirmed by direct probe (see get_invoice_details' own docstring/module
    note) that ARInvoice has no dueDate/curyDueDate/docDate column at all in
    this schema, only invoiceDate. This ages by invoice date as the closest
    honest proxy; it is NOT a true due-date calculation, and callers/reports
    should say "invoiced over N days ago, balance not yet confirmed clear" —
    not "past due" — unless a real due date becomes available later. `paid`
    status itself isn't directly exposed either (unlike AP's payDate); use
    `unpaidBalance > 0` as the "still owes something" signal in a downstream
    filter node -- this tool fetches candidates, it doesn't decide.

    IMPORTANT SCHEMA LIMIT (see the module docstring): ARInvoice ALSO has NO
    customer/bAccount link field in this schema -- confirmed by direct probe,
    not a decision made here. These rows can be totaled, aged, and flagged as
    a company-wide digest, but CANNOT be attributed to a customer or routed to
    an account manager. Do not add a customerId/customerName field to this
    tool's output -- there is no real column to source it from, and inventing
    one would be exactly the "confidently incorrect report" the governance
    design explicitly warns against. A true per-customer collections queue
    would need this table's schema (or a join table) to change first.

    ONE real page per call, same contract as every other list tool in this
    file -- NOT an auto-exhausting aggregate. See get_ap_invoices_due_soon's
    docstring for why this changed 2026-08-21 and how to get the full
    dataset in one governed call (`paginate: true` on a workflow-graph
    tool_call node).
    """
    f = MINIERP_FIELDS
    cutoff = datetime.utcnow() - timedelta(days=max(0, int(min_invoice_age_days or 0)))
    options = {
        "select": _select_all_for(
            "ar_ref_nbr", "ar_doc_type", "ar_invoice_date", "ar_invoice_nbr",
            "ar_line_total", "ar_tax_total", "ar_payment_total", "ar_unpaid_balance",
            "ar_terms_id", "ar_credit_hold",
        ),
        "where": {
            f["ar_invoice_date"]: {"lte": _format_date(cutoff.strftime("%Y-%m-%d"), end_of_day=True)},
            f["company_id"]: {"in": _company_ids(company_id)},
        },
        "page": _clamp_page(page),
        "pageSize": _clamp_bulk_page_size(page_size),
    }
    result = await find_with_offset_pagination(MINIERP_ENTITIES["ar_invoice"], options)
    items = result.get("items") or []
    if not items:
        return ArInvoicesPastDueResult(status="not_found", invoices=[], pagination=None)

    invoices = [
        {
            "invoiceNumber": it.get(f["ar_ref_nbr"]),
            "docType": it.get(f["ar_doc_type"]),
            "invoiceDate": it.get(f["ar_invoice_date"]),
            "lineTotal": float(it.get(f["ar_line_total"]) or 0),
            "taxTotal": float(it.get(f["ar_tax_total"]) or 0),
            "paymentTotal": float(it.get(f["ar_payment_total"]) or 0),
            "unpaidBalance": float(it.get(f["ar_unpaid_balance"]) or 0),
            "termsId": it.get(f["ar_terms_id"]),
            "creditHold": bool(it.get(f["ar_credit_hold"])),
        }
        for it in items
    ]
    pagination = {
        "page": result.get("page") or page,
        "pageSize": result.get("pageSize") or page_size,
        "returned": len(invoices),
        "hasMore": bool(result.get("hasMore")),
    }
    return ArInvoicesPastDueResult(status="ok", invoices=invoices, pagination=pagination)


# ── POLine (line-item detail for a PO -- mirrors mcp-minierp's
# get_product_details_in_order, header-only get_po_order_status's missing half) ─


async def get_po_line_items(
    po_number: str, company_id: int | None = None,
    page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE, fetch_all: bool = False,
) -> PoLineItemsResult:
    """List line items (product, quantities, cost) for one purchase order by
    PO number. fetch_all=True returns every line item in one governed call
    instead of paging manually."""
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(po_number):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for(
                    "pol_line_nbr", "pol_inventory_id", "pol_descr",
                    "pol_order_qty", "pol_received_qty", "pol_billed_qty",
                    "pol_open_qty", "pol_unit_cost", "pol_ext_cost", "pol_uom",
                    "pol_promised_date", "pol_closed", "pol_cancelled", "pol_completed",
                ),
                "where": {
                    f["pol_order_nbr"]: candidate,
                    f["company_id"]: company_candidate,
                },
                "orderBy": {f["pol_line_nbr"]: "ASC"},
                "page": _clamp_page(page),
                "pageSize": _clamp_page_size(page_size),
            }
            items, pagination, truncated = await fetch_page_or_all(
                MINIERP_ENTITIES["po_line"], options, fetch_all=fetch_all, page=page, page_size=page_size,
            )
            if items:
                records = [
                    {
                        "lineNumber": it.get(f["pol_line_nbr"]),
                        "inventoryId": it.get(f["pol_inventory_id"]),
                        "description": it.get(f["pol_descr"]),
                        "orderedQty": float(it.get(f["pol_order_qty"]) or 0),
                        "receivedQty": float(it.get(f["pol_received_qty"]) or 0),
                        "billedQty": float(it.get(f["pol_billed_qty"]) or 0),
                        "openQty": float(it.get(f["pol_open_qty"]) or 0),
                        "unitCost": float(it.get(f["pol_unit_cost"]) or 0),
                        "extCost": float(it.get(f["pol_ext_cost"]) or 0),
                        "uom": it.get(f["pol_uom"]),
                        "promisedDate": it.get(f["pol_promised_date"]),
                        "closed": bool(it.get(f["pol_closed"])),
                        "cancelled": bool(it.get(f["pol_cancelled"])),
                        "completed": bool(it.get(f["pol_completed"])),
                    }
                    for it in items
                ]
                return PoLineItemsResult(
                    status="success",
                    orderNumber=po_number, company=company_candidate,
                    records=records, pagination=pagination, truncated=truncated,
                )
    return PoLineItemsResult(
        status="not_found",
        message=f"No line items found for PO {po_number}.",
        orderNumber=po_number, records=[],
    )


# ── ARAdjust / APAdjust (payment-application history) -- see module docstring
# for why these, not ARPayment/APPayment, are the right tables for this. ──────


async def get_ar_payment_history(
    customer_id: str, page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    fetch_all: bool = False,
) -> ArPaymentHistoryResult:
    """List AR payment applications for a customer: which invoice was paid or
    credited by which payment/credit-memo document, when, and for how much.
    fetch_all=True returns this customer's complete payment history in one
    governed call instead of paging manually."""
    baccount_id = await _resolve_baccount_id(customer_id)
    if baccount_id is None:
        return ArPaymentHistoryResult(
            status="not_found",
            message=f"No customer account found for {customer_id}.",
            customerId=customer_id, records=[],
        )
    f = MINIERP_FIELDS
    options = {
        "select": _select_all_for(
            "ara_invoice_ref_nbr", "ara_invoice_doc_type",
            "ara_payment_ref_nbr", "ara_payment_doc_type", "ara_amount_applied",
            "ara_invoice_date", "ara_payment_date", "ara_released", "ara_voided", "ara_hold",
        ),
        "where": {f["ara_customer_id"]: baccount_id},
        "orderBy": {f["ara_payment_date"]: "DESC"},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    items, pagination, truncated = await fetch_page_or_all(
        MINIERP_ENTITIES["ar_adjust"], options, fetch_all=fetch_all, page=page, page_size=page_size,
    )
    if not items:
        return ArPaymentHistoryResult(
            status="not_found",
            message=f"No payment history found for customer {customer_id}.",
            customerId=customer_id, records=[],
        )
    records = [
        {
            "invoiceRefNbr": it.get(f["ara_invoice_ref_nbr"]),
            "invoiceDocType": it.get(f["ara_invoice_doc_type"]),
            "paymentRefNbr": it.get(f["ara_payment_ref_nbr"]),
            "paymentDocType": it.get(f["ara_payment_doc_type"]),
            "amountApplied": float(it.get(f["ara_amount_applied"]) or 0),
            "invoiceDate": it.get(f["ara_invoice_date"]),
            "paymentDate": it.get(f["ara_payment_date"]),
            "released": bool(it.get(f["ara_released"])),
            "voided": bool(it.get(f["ara_voided"])),
            "onHold": bool(it.get(f["ara_hold"])),
        }
        for it in items
    ]
    return ArPaymentHistoryResult(
        status="success",
        customerId=customer_id, records=records, pagination=pagination, truncated=truncated,
    )


async def get_ap_payment_history(
    vendor_code: str, page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    fetch_all: bool = False,
) -> ApPaymentHistoryResult:
    """List AP payment applications for a vendor: which bill was paid by which
    payment document, when, and for how much.

    fetch_all=True returns this vendor's complete payment history in one
    governed call (internally pages up to _MAX_AGGREGATE_PAGES real pages of
    _AGGREGATE_PAGE_SIZE each) instead of the caller looping page=1,2,3...
    itself -- prefer this over manual re-paging when the goal is "all of this
    vendor's payment history," not one page. `truncated=true` means even that
    cap wasn't enough; `page`/`page_size` are ignored when fetch_all is set."""
    baccount_id = await _resolve_baccount_id(vendor_code)
    if baccount_id is None:
        return ApPaymentHistoryResult(
            status="not_found",
            message=f"No vendor account found for {vendor_code}.",
            vendorCode=vendor_code, records=[],
        )
    f = MINIERP_FIELDS
    options = {
        "select": _select_all_for(
            "apa_invoice_ref_nbr", "apa_invoice_doc_type",
            "apa_payment_ref_nbr", "apa_payment_doc_type", "apa_amount_applied",
            "apa_invoice_date", "apa_payment_date", "apa_released", "apa_voided",
            "apa_hold", "apa_payment_method_id",
        ),
        "where": {f["apa_vendor_id"]: baccount_id},
        "orderBy": {f["apa_payment_date"]: "DESC"},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    items, pagination, truncated = await fetch_page_or_all(
        MINIERP_ENTITIES["ap_adjust"], options, fetch_all=fetch_all, page=page, page_size=page_size,
    )
    if not items:
        return ApPaymentHistoryResult(
            status="not_found",
            message=f"No payment history found for vendor {vendor_code}.",
            vendorCode=vendor_code, records=[],
        )
    records = [
        {
            "invoiceRefNbr": it.get(f["apa_invoice_ref_nbr"]),
            "invoiceDocType": it.get(f["apa_invoice_doc_type"]),
            "paymentRefNbr": it.get(f["apa_payment_ref_nbr"]),
            "paymentDocType": it.get(f["apa_payment_doc_type"]),
            "amountApplied": float(it.get(f["apa_amount_applied"]) or 0),
            "invoiceDate": it.get(f["apa_invoice_date"]),
            "paymentDate": it.get(f["apa_payment_date"]),
            "released": bool(it.get(f["apa_released"])),
            "voided": bool(it.get(f["apa_voided"])),
            "onHold": bool(it.get(f["apa_hold"])),
            "paymentMethodId": it.get(f["apa_payment_method_id"]),
        }
        for it in items
    ]
    return ApPaymentHistoryResult(
        status="success",
        vendorCode=vendor_code, records=records, pagination=pagination, truncated=truncated,
    )


# ── GLHistory (real period-level balances) ──────────────────────────────────


async def get_gl_period_summary(
    account_cd: str, fin_period_id: str = "", company_id: int | None = None,
    page: int = 1, page_size: int = 12, fetch_all: bool = False,
) -> GlPeriodSummaryResult:
    """Period-level GL balances (beginning balance, period debit/credit, YTD
    balance) for one account -- a direct rollup from GLHistory, not a
    client-side aggregation over get_gl_account_transactions' raw GLTran rows.
    fin_period_id, if given, is Acumatica's "YYYYMM" format (e.g. "202401").
    fetch_all=True returns every period on file in one governed call instead
    of paging manually."""
    account_id, matched_company = await _resolve_account_id(account_cd, company_id)
    if account_id is None:
        return GlPeriodSummaryResult(
            status="not_found",
            message=f"GL account {account_cd} not found.",
            accountCd=account_cd, records=[],
        )
    f = MINIERP_FIELDS
    where: dict[str, Any] = {
        f["glh_account_id"]: account_id,
        f["company_id"]: matched_company,
    }
    if fin_period_id:
        where[f["glh_fin_period_id"]] = fin_period_id.strip()
    options = {
        "select": _select_all_for(
            "glh_fin_period_id", "glh_ledger_id", "glh_sub_id", "glh_balance_type",
            "glh_beg_balance", "glh_ptd_debit", "glh_ptd_credit", "glh_ytd_balance",
        ),
        "where": where,
        "orderBy": {f["glh_fin_period_id"]: "DESC"},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    items, pagination, truncated = await fetch_page_or_all(
        MINIERP_ENTITIES["gl_history"], options, fetch_all=fetch_all, page=page, page_size=page_size,
    )
    if not items:
        return GlPeriodSummaryResult(
            status="not_found",
            message=f"No period history found for {account_cd}.",
            accountCd=account_cd, records=[],
        )
    records = [
        {
            "finPeriodId": it.get(f["glh_fin_period_id"]),
            "ledgerId": it.get(f["glh_ledger_id"]),
            "subId": it.get(f["glh_sub_id"]),
            "balanceType": it.get(f["glh_balance_type"]),
            "beginningBalance": float(it.get(f["glh_beg_balance"]) or 0),
            "periodDebit": float(it.get(f["glh_ptd_debit"]) or 0),
            "periodCredit": float(it.get(f["glh_ptd_credit"]) or 0),
            "ytdBalance": float(it.get(f["glh_ytd_balance"]) or 0),
        }
        for it in items
    ]
    return GlPeriodSummaryResult(
        status="success",
        accountCd=account_cd, company=matched_company,
        records=records, pagination=pagination, truncated=truncated,
    )


# ── ARTran / APTran (billed line-item detail) -- mirrors get_po_line_items,
# just for AR/AP documents instead of POs. ──────────────────────────────────


async def get_invoice_line_items(
    invoice_number: str, company_id: int | None = None,
    page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE, fetch_all: bool = False,
) -> InvoiceLineItemsResult:
    """List billed line items (product, qty, price, sales rep) for one AR
    invoice by invoice/reference number -- line-level detail get_invoice_details
    doesn't carry. fetch_all=True returns every line item in one governed
    call instead of paging manually."""
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(invoice_number):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for(
                    "art_line_nbr", "art_tran_type", "art_inventory_id", "art_descr",
                    "art_qty", "art_unit_price", "art_ext_price",
                    "art_tax_category_id", "art_sales_person_id",
                ),
                "where": {
                    f["art_ref_nbr"]: candidate,
                    f["company_id"]: company_candidate,
                },
                "orderBy": {f["art_line_nbr"]: "ASC"},
                "page": _clamp_page(page),
                "pageSize": _clamp_page_size(page_size),
            }
            items, pagination, truncated = await fetch_page_or_all(
                MINIERP_ENTITIES["ar_tran"], options, fetch_all=fetch_all, page=page, page_size=page_size,
            )
            if items:
                records = [
                    {
                        "lineNumber": it.get(f["art_line_nbr"]),
                        "docType": it.get(f["art_tran_type"]),
                        "inventoryId": it.get(f["art_inventory_id"]),
                        "description": it.get(f["art_descr"]),
                        "qty": float(it.get(f["art_qty"]) or 0),
                        "unitPrice": float(it.get(f["art_unit_price"]) or 0),
                        "extPrice": float(it.get(f["art_ext_price"]) or 0),
                        "taxCategoryId": it.get(f["art_tax_category_id"]),
                        "salesPersonId": it.get(f["art_sales_person_id"]),
                    }
                    for it in items
                ]
                return InvoiceLineItemsResult(
                    status="success",
                    invoiceNumber=invoice_number, company=company_candidate,
                    records=records, pagination=pagination, truncated=truncated,
                )
    return InvoiceLineItemsResult(
        status="not_found",
        message=f"No line items found for invoice {invoice_number}.",
        invoiceNumber=invoice_number, records=[],
    )


async def get_bill_line_items(
    invoice_number: str, company_id: int | None = None,
    page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE, fetch_all: bool = False,
) -> BillLineItemsResult:
    """List billed line items (product, qty, cost, linked PO) for one AP bill
    by invoice/reference number -- mirrors get_invoice_line_items for AP.
    fetch_all=True returns every line item in one governed call instead of
    paging manually."""
    f = MINIERP_FIELDS
    for candidate in _identifier_candidates(invoice_number):
        for company_candidate in _company_ids(company_id):
            options = {
                "select": _select_all_for(
                    "apt_line_nbr", "apt_tran_type", "apt_inventory_id", "apt_descr",
                    "apt_qty", "apt_unit_cost", "apt_line_amt",
                    "apt_tax_category_id", "apt_po_nbr",
                ),
                "where": {
                    f["apt_ref_nbr"]: candidate,
                    f["company_id"]: company_candidate,
                },
                "orderBy": {f["apt_line_nbr"]: "ASC"},
                "page": _clamp_page(page),
                "pageSize": _clamp_page_size(page_size),
            }
            items, pagination, truncated = await fetch_page_or_all(
                MINIERP_ENTITIES["ap_tran"], options, fetch_all=fetch_all, page=page, page_size=page_size,
            )
            if items:
                records = [
                    {
                        "lineNumber": it.get(f["apt_line_nbr"]),
                        "docType": it.get(f["apt_tran_type"]),
                        "inventoryId": it.get(f["apt_inventory_id"]),
                        "description": it.get(f["apt_descr"]),
                        "qty": float(it.get(f["apt_qty"]) or 0),
                        "unitCost": float(it.get(f["apt_unit_cost"]) or 0),
                        "lineAmt": float(it.get(f["apt_line_amt"]) or 0),
                        "taxCategoryId": it.get(f["apt_tax_category_id"]),
                        "poNumber": it.get(f["apt_po_nbr"]),
                    }
                    for it in items
                ]
                return BillLineItemsResult(
                    status="success",
                    invoiceNumber=invoice_number, company=company_candidate,
                    records=records, pagination=pagination, truncated=truncated,
                )
    return BillLineItemsResult(
        status="not_found",
        message=f"No line items found for bill {invoice_number}.",
        invoiceNumber=invoice_number, records=[],
    )


# ── SOInvoice (fixes "ARInvoice has no customer link" -- see get_invoice_details'
# own docstring for that limitation; SOInvoice.customerId is the real fix) ────


async def get_customer_invoice_history(
    customer_id: str, page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    fetch_all: bool = False,
) -> CustomerInvoiceHistoryResult:
    """List a customer's AR invoices via SOInvoice -- the one AR-adjacent table
    confirmed to carry a real customerId link (ARInvoice itself has none).
    fetch_all=True returns this customer's complete invoice history in one
    governed call instead of paging manually."""
    baccount_id = await _resolve_baccount_id(customer_id)
    if baccount_id is None:
        return CustomerInvoiceHistoryResult(
            status="not_found",
            message=f"No customer account found for {customer_id}.",
            customerId=customer_id, records=[],
        )
    f = MINIERP_FIELDS
    options = {
        "select": _select_all_for(
            "soi_ref_nbr", "soi_doc_type", "soi_order_nbr",
            "soi_payment_amt", "soi_payment_method_id",
        ),
        "where": {
            f["soi_customer_id"]: baccount_id,
            f["company_id"]: {"in": _company_ids(None)},
        },
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    items, pagination, truncated = await fetch_page_or_all(
        MINIERP_ENTITIES["so_invoice"], options, fetch_all=fetch_all, page=page, page_size=page_size,
    )
    if not items:
        return CustomerInvoiceHistoryResult(
            status="not_found",
            message=f"No invoice history found for customer {customer_id}.",
            customerId=customer_id, records=[],
        )
    records = [
        {
            "invoiceRefNbr": it.get(f["soi_ref_nbr"]),
            "docType": it.get(f["soi_doc_type"]),
            "orderNumber": it.get(f["soi_order_nbr"]),
            "paymentAmount": float(it.get(f["soi_payment_amt"]) or 0),
            "paymentMethodId": it.get(f["soi_payment_method_id"]),
        }
        for it in items
    ]
    return CustomerInvoiceHistoryResult(
        status="success",
        customerId=customer_id, records=records, pagination=pagination, truncated=truncated,
    )


# ── INTran (inventory transaction history -- NOT live lot status) ───────────


async def get_item_movement_history(
    inventory_id: str, start_date: str = "", end_date: str = "",
    company_id: int | None = None, page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE,
    fetch_all: bool = False,
) -> ItemMovementHistoryResult:
    """Inventory transaction history (receipts/issues/transfers) for one item,
    including lot/serial number and expiration date where the item is tracked
    that way. fetch_all=True returns every transaction in range in one
    governed call instead of paging manually.

    NOT live lot status. INLotSerStatus (current qty/status per lot) is
    confirmed FORBIDDEN under this credential -- this reconstructs movement
    history from INTran instead. lotSerialNbr/expireDate come back blank/null
    for items that aren't lot-tracked (confirmed true for most sampled items) --
    that's real, not a bug. Report this to users as transaction history, never
    as "current lot status" or "what's expiring soon" -- this tool cannot
    answer either of those without a live status table.
    """
    inv = (inventory_id or "").strip()
    if not inv:
        return ItemMovementHistoryResult(
            status="missing_identifier",
            message="An inventory_id is required.", missingFields=["inventory_id"],
            records=[],
        )
    f = MINIERP_FIELDS
    where: dict[str, Any] = {
        f["int_inventory_id"]: inv,
        f["company_id"]: {"in": _company_ids(company_id)},
    }
    if start_date:
        where[f["int_tran_date"]] = {**where.get(f["int_tran_date"], {}), "gte": _format_date(start_date)}
    if end_date:
        existing = where.get(f["int_tran_date"], {})
        if isinstance(existing, dict):
            existing["lte"] = _format_date(end_date, end_of_day=True)
            where[f["int_tran_date"]] = existing
        else:
            where[f["int_tran_date"]] = {"lte": _format_date(end_date, end_of_day=True)}
    options = {
        "select": _select_all_for(
            "int_ref_nbr", "int_tran_type", "int_doc_type", "int_qty",
            "int_lot_serial_nbr", "int_expire_date", "int_tran_date",
            "int_site_id", "int_location_id", "int_so_order_nbr", "int_po_receipt_nbr",
        ),
        "where": where,
        "orderBy": {f["int_tran_date"]: "DESC"},
        "page": _clamp_page(page),
        "pageSize": _clamp_page_size(page_size),
    }
    items, pagination, truncated = await fetch_page_or_all(
        MINIERP_ENTITIES["in_tran"], options, fetch_all=fetch_all, page=page, page_size=page_size,
    )
    if not items:
        return ItemMovementHistoryResult(
            status="not_found",
            message=f"No movement history found for item {inventory_id}.",
            inventoryId=inv, records=[],
        )
    records = [
        {
            "refNbr": it.get(f["int_ref_nbr"]),
            "tranType": it.get(f["int_tran_type"]),
            "docType": it.get(f["int_doc_type"]),
            "qty": float(it.get(f["int_qty"]) or 0),
            "lotSerialNbr": it.get(f["int_lot_serial_nbr"]) or None,
            "expireDate": it.get(f["int_expire_date"]),
            "tranDate": it.get(f["int_tran_date"]),
            "siteId": it.get(f["int_site_id"]),
            "locationId": it.get(f["int_location_id"]),
            "orderNumber": it.get(f["int_so_order_nbr"]),
            "poReceiptNumber": it.get(f["int_po_receipt_nbr"]),
        }
        for it in items
    ]
    return ItemMovementHistoryResult(
        status="success",
        truncated=truncated,
        inventoryId=inv, records=records, pagination=pagination,
        note="Transaction history, not live lot/serial status -- INLotSerStatus is not reachable under this credential.",
    )
