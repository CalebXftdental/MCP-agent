"""Typed response models for `sqlagent.finance.index` tools.

Each model is built directly from the exact JSON shape that tool already emits
via `_json_tool_result` (field names, `status`/`intent` literal values copied
verbatim, not redesigned) -- the point of this module is to let FastMCP derive
a real, named-field `outputSchema` per tool from the return-type annotation,
not to change what callers receive. `extra="forbid"` on every model so a stray
field is a loud construction-time error, never a silently dropped key.

Field names here MUST stay in lockstep with `policy/manifest.py`'s
`ToolPolicy.fields` for the corresponding tool -- redaction matches by bare
field name anywhere in the tree (see `policy/redaction.py`), so a rename here
without updating the manifest would silently stop redacting that field.

Every model adds a `missing_identifier` status branch even where the original
`_json_tool_result`-based function never needed one -- this lets `app.py`'s
wrapper-level "field is required" early return use the same typed model
instead of the cross-domain `_missing()`/`_MISSING_CUSTOMER` helper (which
hardcodes `source: "miniERP-orders"` even for finance tools -- a pre-existing
mislabel fixed as a required side effect of giving every branch one consistent
return type, not a redesign of what the field means).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    pageSize: int
    returned: int
    hasMore: bool


class DateRangeFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    startDate: str | None = None
    endDate: str | None = None


# ── get_vendor_details ──────────────────────────────────────────────────────


class VendorDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["vendor_details"] = "vendor_details"
    message: str | None = None
    missingFields: list[str] | None = None
    vendorCode: str | None = None
    vendorClassId: str | None = None
    termsId: str | None = None
    curyId: str | None = None
    paymentMethodId: str | None = None
    vendor1099: bool | None = None
    retainageApply: bool | None = None


# ── get_vendor_ap_invoices ───────────────────────────────────────────────────


class VendorApInvoiceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoiceNumber: str | None = None
    docType: str | None = None
    invoiceDate: str | None = None
    dueDate: str | None = None
    lineTotal: float
    taxTotal: float
    paid: bool


class VendorApInvoicesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["vendor_ap_invoices"] = "vendor_ap_invoices"
    message: str | None = None
    missingFields: list[str] | None = None
    vendorCode: str | None = None
    records: list[VendorApInvoiceRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_ap_invoice_details ───────────────────────────────────────────────────


class ApInvoiceDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["ap_invoice_details"] = "ap_invoice_details"
    message: str | None = None
    missingFields: list[str] | None = None
    invoiceNumber: str | None = None
    docType: str | None = None
    invoiceDate: str | None = None
    dueDate: str | None = None
    lineTotal: float | None = None
    taxTotal: float | None = None
    paid: bool | None = None
    paymentTypeId: str | None = None
    termsId: str | None = None
    vendorBAccountId: int | None = None
    company: int | None = None


# ── get_invoice_details ──────────────────────────────────────────────────────


class InvoiceDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["invoice_details"] = "invoice_details"
    message: str | None = None
    missingFields: list[str] | None = None
    invoiceNumber: str | None = None
    docType: str | None = None
    invoiceDate: str | None = None
    invoiceNbr: str | None = None
    lineTotal: float | None = None
    taxTotal: float | None = None
    paymentTotal: float | None = None
    unpaidBalance: float | None = None
    termsId: str | None = None
    creditHold: bool | None = None
    paymentMethodId: str | None = None
    company: int | None = None


# ── get_po_order_status ──────────────────────────────────────────────────────


class PoOrderStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["po_order_status"] = "po_order_status"
    message: str | None = None
    missingFields: list[str] | None = None
    orderNumber: str | None = None
    orderStatus: str | None = None
    orderDate: str | None = None
    expectedDate: str | None = None
    orderTotal: float | None = None
    vendorBAccountId: int | None = None
    shipVia: str | None = None
    onHold: bool | None = None
    company: int | None = None


# ── get_gl_account_transactions ──────────────────────────────────────────────


class GlTransactionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    module: str | None = None
    batchNbr: str | None = None
    refNbr: str | None = None
    description: str | None = None
    debit: float
    credit: float


class GlAccountTransactionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["gl_account_transactions"] = "gl_account_transactions"
    message: str | None = None
    missingFields: list[str] | None = None
    accountCd: str | None = None
    company: int | None = None
    filters: DateRangeFilters | None = None
    records: list[GlTransactionRecord] = []
    netMovementThisPage: float | None = None
    pagination: Pagination | None = None
    note: str | None = None
    truncated: bool | None = None


# ── get_sales_price ───────────────────────────────────────────────────────────


class SalesPriceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inventoryId: str | None = None
    customerId: str | None = None
    custPriceClassId: str | None = None
    salesPrice: float
    curyId: str | None = None
    uom: str | None = None
    effectiveDate: str | None = None
    expirationDate: str | None = None
    priceType: str | None = None
    breakQty: float | None = None


class SalesPriceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["sales_price"] = "sales_price"
    message: str | None = None
    missingFields: list[str] | None = None
    inventoryId: str | None = None
    records: list[SalesPriceRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_ap_invoices_due_soon ─────────────────────────────────────────────────


class ApInvoiceDueSoonRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoiceNumber: str | None = None
    docType: str | None = None
    invoiceDate: str | None = None
    dueDate: str | None = None
    lineTotal: float
    taxTotal: float
    paid: bool
    vendorCode: str | None = None
    vendorName: str | None = None


class ApInvoicesDueSoonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["ok", "not_found"]
    intent: Literal["ap_invoices_due_soon"] = "ap_invoices_due_soon"
    invoices: list[ApInvoiceDueSoonRecord]
    pagination: Pagination | None = None


# ── get_ar_invoices_past_due ─────────────────────────────────────────────────


class ArInvoicePastDueRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoiceNumber: str | None = None
    docType: str | None = None
    invoiceDate: str | None = None
    lineTotal: float
    taxTotal: float
    paymentTotal: float
    unpaidBalance: float
    termsId: str | None = None
    creditHold: bool


class ArInvoicesPastDueResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["ok", "not_found"]
    intent: Literal["ar_invoices_past_due"] = "ar_invoices_past_due"
    invoices: list[ArInvoicePastDueRecord]
    pagination: Pagination | None = None


# ── get_po_line_items ─────────────────────────────────────────────────────────


class PoLineItemRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lineNumber: int | None = None
    inventoryId: str | None = None
    description: str | None = None
    orderedQty: float
    receivedQty: float
    billedQty: float
    openQty: float
    unitCost: float
    extCost: float
    uom: str | None = None
    promisedDate: str | None = None
    closed: bool
    cancelled: bool
    completed: bool


class PoLineItemsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["po_line_items"] = "po_line_items"
    message: str | None = None
    missingFields: list[str] | None = None
    orderNumber: str | None = None
    company: int | None = None
    records: list[PoLineItemRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_ar_payment_history ───────────────────────────────────────────────────


class ArPaymentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoiceRefNbr: str | None = None
    invoiceDocType: str | None = None
    paymentRefNbr: str | None = None
    paymentDocType: str | None = None
    amountApplied: float
    invoiceDate: str | None = None
    paymentDate: str | None = None
    released: bool
    voided: bool
    onHold: bool


class ArPaymentHistoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["ar_payment_history"] = "ar_payment_history"
    message: str | None = None
    missingFields: list[str] | None = None
    customerId: str | None = None
    records: list[ArPaymentRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_ap_payment_history ───────────────────────────────────────────────────


class ApPaymentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoiceRefNbr: str | None = None
    invoiceDocType: str | None = None
    paymentRefNbr: str | None = None
    paymentDocType: str | None = None
    amountApplied: float
    invoiceDate: str | None = None
    paymentDate: str | None = None
    released: bool
    voided: bool
    onHold: bool
    paymentMethodId: str | None = None


class ApPaymentHistoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["ap_payment_history"] = "ap_payment_history"
    message: str | None = None
    missingFields: list[str] | None = None
    vendorCode: str | None = None
    records: list[ApPaymentRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_gl_period_summary ────────────────────────────────────────────────────


class GlPeriodRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finPeriodId: str | None = None
    ledgerId: str | None = None
    subId: str | None = None
    balanceType: str | None = None
    beginningBalance: float
    periodDebit: float
    periodCredit: float
    ytdBalance: float


class GlPeriodSummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["gl_period_summary"] = "gl_period_summary"
    message: str | None = None
    missingFields: list[str] | None = None
    accountCd: str | None = None
    company: int | None = None
    records: list[GlPeriodRecord] = []
    pagination: Pagination | None = None


# ── get_invoice_line_items ───────────────────────────────────────────────────


class ArInvoiceLineRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lineNumber: int | None = None
    docType: str | None = None
    inventoryId: str | None = None
    description: str | None = None
    qty: float
    unitPrice: float
    extPrice: float
    taxCategoryId: str | None = None
    salesPersonId: str | None = None


class InvoiceLineItemsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["invoice_line_items"] = "invoice_line_items"
    message: str | None = None
    missingFields: list[str] | None = None
    invoiceNumber: str | None = None
    company: int | None = None
    records: list[ArInvoiceLineRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_bill_line_items ──────────────────────────────────────────────────────


class ApBillLineRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lineNumber: int | None = None
    docType: str | None = None
    inventoryId: str | None = None
    description: str | None = None
    qty: float
    unitCost: float
    lineAmt: float
    taxCategoryId: str | None = None
    poNumber: str | None = None


class BillLineItemsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["bill_line_items"] = "bill_line_items"
    message: str | None = None
    missingFields: list[str] | None = None
    invoiceNumber: str | None = None
    company: int | None = None
    records: list[ApBillLineRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_customer_invoice_history ─────────────────────────────────────────────


class CustomerInvoiceHistoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoiceRefNbr: str | None = None
    docType: str | None = None
    orderNumber: str | None = None
    paymentAmount: float
    paymentMethodId: str | None = None


class CustomerInvoiceHistoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["customer_invoice_history"] = "customer_invoice_history"
    message: str | None = None
    missingFields: list[str] | None = None
    customerId: str | None = None
    records: list[CustomerInvoiceHistoryRecord] = []
    pagination: Pagination | None = None
    truncated: bool | None = None


# ── get_item_movement_history ────────────────────────────────────────────────


class ItemMovementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refNbr: str | None = None
    tranType: str | None = None
    docType: str | None = None
    qty: float
    lotSerialNbr: str | None = None
    expireDate: str | None = None
    tranDate: str | None = None
    siteId: str | None = None
    locationId: str | None = None
    orderNumber: str | None = None
    poReceiptNumber: str | None = None


class ItemMovementHistoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["miniERP-finance"] = "miniERP-finance"
    status: Literal["success", "not_found", "missing_identifier"]
    intent: Literal["item_movement_history"] = "item_movement_history"
    message: str | None = None
    missingFields: list[str] | None = None
    inventoryId: str | None = None
    records: list[ItemMovementRecord] = []
    pagination: Pagination | None = None
    note: str | None = None
    truncated: bool | None = None
