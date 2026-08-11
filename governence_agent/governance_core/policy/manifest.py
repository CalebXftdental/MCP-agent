"""Tool manifest -- the single source the PDP reads to reason about a tool.

For every backend tool it declares:
  - backend:          which MCP backend owns it (e.g. "minierp_orders")
  - intent:           stable intent label used in refusal payloads
  - account_scoped:   True if the call requires a customer in session scope
                      (deny before ever hitting the backend if absent)
  - fields:           {output_field_name: classification level}

Field names MUST match the keys the backend actually returns (see each
backend's sqlagent/structured.py and index.py's `_json_tool_result` shapes).
Any returned field NOT listed here is treated as INTERNAL (visible to any
consumer entitled to INTERNAL). Only fields that need protection must be listed.

The miniERP tool surface is served by ONE consolidated backend process
(mcp-minierp/), but `backend` below still uses four logical domain tags --
minierp_orders, minierp_accounts, minierp_shipments, minierp_finance -- because
that's what the PDP and category grants (departments.py) authorize by. Each
canonical tool name is unique across all four domains (see canonical()), so
`backend` here is the single source of truth for which domain owns a given
tool, independent of how many processes actually serve it.

The gateway exposes each tool namespaced as "<backend>_<canonical>"
(e.g. minierp_orders_get_customer_orders) -- see namespaced()/canonical().
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Classification levels ─────────────────────────────────────────────────────
# A requester sees a field only if its level is in that requester's entitlement
# set (see entitlements.py). Levels are labels, not a strict hierarchy.
PUBLIC = "PUBLIC"        # safe for anyone (status messages, labels)
INTERNAL = "INTERNAL"    # business data, non-sensitive (order numbers, dates, status)
PII = "PII"              # personal data (names, emails, phones, street addresses)
SENSITIVE = "SENSITIVE"  # financial figures (order totals, spend)
PCI = "PCI"              # card data (none today; reserved)

# Every classification level -- the set a genuinely unrestricted principal
# (an admin relying on resolve.py's category-less "allow all tools"
# fallback) needs in `allowed_levels` to actually see unredacted data, not
# just have tool calls succeed while every classified field comes back
# masked because `allowed_levels` was left at its empty default.
ALL_LEVELS = frozenset({PUBLIC, INTERNAL, PII, SENSITIVE, PCI})

# Default redaction action per level. PII/PCI are masked (shape preserved),
# everything else is dropped. A tool field may override via FieldRule.action.
_DEFAULT_ACTION = {PII: "mask", PCI: "mask", SENSITIVE: "drop", INTERNAL: "drop", PUBLIC: "drop"}


def default_action(level: str) -> str:
    return _DEFAULT_ACTION.get(level, "drop")


# Risk tiers (expansion.md §7.3) -- a coarse consequence label per tool, orthogonal
# to field classification. "Read" tiers describe what the tool SEES; "export"/"send"/
# "write"/"code_exec" describe what the tool DOES to the outside world. Used to drive
# approval gating (see gateway/app.py's broad-export + send checks, which read
# ToolPolicy.risk/approval_required/max_rows_without_approval rather than
# hardcoding a threshold per call site).
READ_LOW = "read_low"              # single-entity lookup, no PII/SENSITIVE fields
READ_SENSITIVE = "read_sensitive"  # PII or financial fields in the response
EXPORT = "export"                  # file generation / multi-row dataset export
SEND = "send"                      # leaves the system (email, calendar invite)
WRITE = "write"                    # creates/modifies data in a connected system
CODE_EXEC = "code_exec"            # opencode/shell-driven automation


@dataclass(frozen=True)
class ToolPolicy:
    backend: str
    intent: str
    account_scoped: bool = False
    fields: dict[str, str] = field(default_factory=dict)
    description: str = ""   # one plain-English line for non-technical UIs (self-service
                             # request-access picker, "My Access"); NOT the MCP-facing
                             # docstring in gateway/app.py (that's written for an LLM
                             # caller and mixes in tool-selection guidance)
    risk: str = READ_LOW
    approval_required: bool = False           # this tool's effect always needs a prior approval
    max_rows_without_approval: int | None = None  # None = no row-count gate for this tool


# canonical tool name -> policy
TOOL_POLICIES: dict[str, ToolPolicy] = {
    "get_customer_orders": ToolPolicy(
        backend="minierp_orders",
        intent="customer_orders",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="Lists a customer's orders — can filter by date, status, or minimum amount.",
        fields={
            "customerId": INTERNAL,
            "orderNumber": INTERNAL,
            "status": INTERNAL,
            "statusCode": INTERNAL,
            "date": INTERNAL,
            "total": SENSITIVE,
        },
    ),
    "get_customer_order_total": ToolPolicy(
        backend="minierp_orders",
        intent="customer_order_total",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="Adds up how much a customer has spent across their completed orders.",
        fields={
            "customerId": INTERNAL,
            "orderCount": INTERNAL,
            "grandTotal": SENSITIVE,
        },
    ),
    "get_customer_profile": ToolPolicy(
        backend="minierp_accounts",
        intent="customer_profile",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="Shows a customer's billing profile — credit limit, payment terms, account type.",
        fields={
            "customerId": INTERNAL,
            "creditLimit": SENSITIVE,
            "creditRule": INTERNAL,
            "creditDaysPastDue": INTERNAL,
            "termsId": INTERNAL,
            "defaultPaymentMethodId": INTERNAL,
            "statementCycleId": INTERNAL,
            "statementType": INTERNAL,
            "customerCategory": INTERNAL,
        },
    ),
    "get_invoice_details": ToolPolicy(
        backend="minierp_finance",
        intent="invoice_details",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Looks up one invoice — amount, date, and what's still owed.",
        fields={
            "invoiceNumber": INTERNAL,
            "docType": INTERNAL,
            "invoiceDate": INTERNAL,
            "invoiceNbr": INTERNAL,
            "lineTotal": SENSITIVE,
            "taxTotal": SENSITIVE,
            "paymentTotal": SENSITIVE,
            "unpaidBalance": SENSITIVE,
            "termsId": INTERNAL,
            "creditHold": INTERNAL,
            "paymentMethodId": INTERNAL,
        },
    ),
    "get_contacts": ToolPolicy(
        backend="minierp_accounts",
        intent="account_contacts",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="Lists the people on a customer's account, with name, role, phone, and email.",
        fields={
            "customerId": INTERNAL,
            "contactId": INTERNAL,
            "name": PII,
            "displayName": INTERNAL,
            "type": INTERNAL,
            "email": PII,
            "phone": PII,
        },
    ),
    "get_addresses": ToolPolicy(
        backend="minierp_accounts",
        intent="account_addresses",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="Gives you the addresses on file for a customer.",
        fields={
            "customerId": INTERNAL,
            "addressId": INTERNAL,
            "type": INTERNAL,
            "street": PII,
            "city": INTERNAL,
            "state": INTERNAL,
            "postalCode": PII,
            "country": INTERNAL,
        },
    ),
    # ── Secondary resolvers (entry points; not account-scoped -- they RESOLVE a
    #    customer_id, so they cannot require one). Homed on minierp_accounts so
    #    the accounts category (which grants PII) authorizes + redacts them. ──
    "find_customer": ToolPolicy(
        backend="minierp_accounts",
        intent="find_customer",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Finds a customer by name, email, phone, or account number.",
        fields={
            "customerId": INTERNAL,
            "bAccountId": INTERNAL,
            "name": PII,
            "email": PII,
            "phone": PII,
            "companyId": INTERNAL,
            "status": INTERNAL,
        },
    ),
    "resolve_contact": ToolPolicy(
        backend="minierp_accounts",
        intent="resolve_contact",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Finds a person by name, email, or phone, and which account they belong to.",
        fields={
            "contactId": INTERNAL,
            "bAccountId": INTERNAL,
            "companyId": INTERNAL,
            "name": PII,
            "title": PII,
            "email": PII,
            "phone": PII,
        },
    ),
    "get_order_addresses": ToolPolicy(
        backend="minierp_accounts",
        intent="order_addresses",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Gives you the shipping and billing addresses for one order.",
        fields={
            "orderNumber": INTERNAL,
            "street": PII,
            "city": INTERNAL,
            "state": INTERNAL,
            "postalCode": PII,
            "country": INTERNAL,
        },
    ),
    # ── Tertiary composites (360 views). Cross-domain, so they emit BOTH PII and
    #    SENSITIVE fields in one payload -- every such key MUST be classified here
    #    or redaction leaks it (the composite widens the redaction surface). Homed
    #    on minierp_accounts so the accounts category (FULL level set) grants them. ──
    "get_customer_overview": ToolPolicy(
        backend="minierp_accounts",
        intent="customer_overview",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="A full snapshot of a customer: profile, main contact, addresses, recent orders, and spend.",
        fields={
            # accounts/contacts/addresses PII
            "name": PII, "email": PII, "phone": PII,
            "street": PII, "postalCode": PII,
            # profile + orders + spend SENSITIVE
            "creditLimit": SENSITIVE, "total": SENSITIVE, "grandTotal": SENSITIVE,
        },
    ),
    "get_order_overview": ToolPolicy(
        backend="minierp_accounts",
        intent="order_overview",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="A full snapshot of one order: line items, shipping/billing address, and tracking.",
        fields={
            # order + line-item + shipment SENSITIVE
            "total": SENSITIVE, "unitPrice": SENSITIVE, "orderTotal": SENSITIVE,
            # ship/bill address PII
            "street": PII, "postalCode": PII,
        },
    ),
    # ── Quaternary: aggregations + reverse lookups ─────────────────────────────
    "get_customer_order_summary": ToolPolicy(
        backend="minierp_orders",
        intent="customer_order_summary",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="Summarizes a customer's order history — counts, average order size, and spend over time.",
        fields={"grandTotal": SENSITIVE, "averageOrderValue": SENSITIVE, "total": SENSITIVE},
    ),
    "get_product_sales": ToolPolicy(
        backend="minierp_orders",
        intent="product_sales",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Shows how well a product is selling — quantity sold and revenue.",
        fields={"totalRevenue": SENSITIVE},
    ),
    "get_orders_by_product": ToolPolicy(
        backend="minierp_orders",
        intent="orders_by_product",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Lists which orders (and customers) bought a given product.",
        fields={"lineTotal": SENSITIVE},
    ),
    "get_customers_by_region": ToolPolicy(
        backend="minierp_accounts",
        intent="customers_by_region",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Lists customers in a given country, state, or city.",
        fields={"name": PII},
    ),
    "get_customer_shipment_status": ToolPolicy(
        backend="minierp_shipments",
        intent="customer_shipment_status",
        account_scoped=True,
        risk=READ_SENSITIVE,
        description="Shows the shipping/tracking status of a customer's most recent orders.",
        fields={"orderTotal": SENSITIVE},
    ),
    # Cross-customer ranking -- GATED: only the analytics category (minierp_analytics
    # backend) grants it; no default category does, so it's off unless an admin
    # explicitly assigns analytics to a principal.
    "get_top_customers_by_spend": ToolPolicy(
        backend="minierp_analytics",
        intent="top_customers_by_spend",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=50,
        description="Ranks customers by total spend over a period (a cross-customer report).",
        fields={"name": PII, "totalSpend": SENSITIVE},
    ),
    # Cross-customer recency -- same gating rationale as get_top_customers_by_spend
    # (minierp_analytics backend, off unless "analytics" is explicitly granted).
    # Every meaningful output field is listed here (not just the PII/SENSITIVE
    # ones), including plain INTERNAL fields -- unlike most manifest entries,
    # because this tool exists specifically to feed a "My Workflow" filter node,
    # and only fields listed here show up as bindable outputs in the graph
    # builder's node-source dropdown (see gateway/backend/workflow_graphs.py's
    # outputFields = sorted(policy.fields.keys())).
    "get_customer_order_recency": ToolPolicy(
        backend="minierp_analytics",
        intent="customer_order_recency",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=50,
        description="Cross-customer order recency by territory -- who hasn't reordered recently (a win-back / reorder-due signal). phone is a best-effort contact number (lowest-id contact on file with one), not a verified primary contact.",
        fields={
            "name": PII, "phone": PII, "grandTotal": SENSITIVE,
            "customerId": INTERNAL, "status": INTERNAL, "orderCount": INTERNAL,
            "firstOrderDate": INTERNAL, "lastOrderDate": INTERNAL,
        },
    ),
    "get_shipping_by_shipment": ToolPolicy(
        backend="minierp_shipments",
        intent="shipment_tracking",
        account_scoped=True,
        risk=READ_LOW,
        description="Looks up tracking and invoice info for one shipment.",
        fields={
            "shipmentNumber": INTERNAL,
            "salesOrders": INTERNAL,
            "trackingNumbers": INTERNAL,
            "invoiceNumbers": INTERNAL,
            "trackingNumber": INTERNAL,
            "invoiceNumber": INTERNAL,
        },
    ),
    "get_order_details": ToolPolicy(
        backend="minierp_orders",
        intent="order_details",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Looks up the basic details of one order — status, date, and total.",
        fields={
            "orderNumber": INTERNAL,
            "orderStatus": INTERNAL,
            "statusCode": INTERNAL,
            "date": INTERNAL,
            "total": SENSITIVE,
        },
    ),
    "get_product_details_in_order": ToolPolicy(
        backend="minierp_orders",
        intent="product_details_in_order",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Lists the specific products, quantities, and prices inside one order.",
        fields={
            "orderNumber": INTERNAL,
            "inventoryId": INTERNAL,
            "sku": INTERNAL,
            "description": INTERNAL,
            "lineDescription": INTERNAL,
            "quantity": INTERNAL,
            "unitPrice": SENSITIVE,
            "total": SENSITIVE,
        },
    ),
    "get_shipping_by_order": ToolPolicy(
        backend="minierp_shipments",
        intent="shipment_tracking",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Looks up the shipment and tracking numbers linked to one order.",
        fields={
            "orderNumber": INTERNAL,
            "shipmentNumbers": INTERNAL,
            "trackingNumbers": INTERNAL,
            "invoiceNumbers": INTERNAL,
            "orderStatus": INTERNAL,
            "orderDate": INTERNAL,
            "orderTotal": SENSITIVE,
            "trackingNumber": INTERNAL,
            "invoiceNumber": INTERNAL,
        },
    ),
    # ── mcp-minierp-finance ──────────────────────────────────────────────────
    "get_vendor_details": ToolPolicy(
        backend="minierp_finance",
        intent="vendor_details",
        account_scoped=False,
        risk=READ_LOW,
        description="Shows a vendor's profile — payment terms, currency, and default payment method.",
        fields={
            "vendorCode": INTERNAL,
            "vendorClassId": INTERNAL,
            "termsId": INTERNAL,
            "curyId": INTERNAL,
            "paymentMethodId": INTERNAL,
            "vendor1099": INTERNAL,
            "retainageApply": INTERNAL,
        },
    ),
    "get_vendor_ap_invoices": ToolPolicy(
        backend="minierp_finance",
        intent="vendor_ap_invoices",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Lists the bills we owe a vendor.",
        fields={
            "vendorCode": INTERNAL,
            "invoiceNumber": INTERNAL,
            "docType": INTERNAL,
            "invoiceDate": INTERNAL,
            "dueDate": INTERNAL,
            "lineTotal": SENSITIVE,
            "taxTotal": SENSITIVE,
            "paid": INTERNAL,
        },
    ),
    "get_ap_invoice_details": ToolPolicy(
        backend="minierp_finance",
        intent="ap_invoice_details",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Looks up one vendor bill — amount, due date, and whether it's been paid.",
        fields={
            "invoiceNumber": INTERNAL,
            "docType": INTERNAL,
            "invoiceDate": INTERNAL,
            "dueDate": INTERNAL,
            "lineTotal": SENSITIVE,
            "taxTotal": SENSITIVE,
            "paid": INTERNAL,
            "paymentTypeId": INTERNAL,
            "termsId": INTERNAL,
            "vendorBAccountId": INTERNAL,
        },
    ),
    # Cross-vendor bulk AP-aging -- same trust boundary as get_vendor_ap_invoices
    # (finance category already grants "*" on minierp_finance; company-wide AP
    # visibility isn't a new capability class the way cross-customer spend
    # ranking is for orders/analytics, just a bulk version of what finance
    # already sees one vendor at a time), so no extra gating category needed.
    # vendorName is INTERNAL, not PII: every other vendor-identity field in this
    # domain (vendorCode, vendorClassId, ...) is already INTERNAL, and the
    # "finance" category's own levels set (_FIN, categories.py) deliberately
    # excludes PII ("no contact PII") -- classifying it PII would silently mask
    # the one field this report exists to show a finance user (who they owe).
    "get_ap_invoices_due_soon": ToolPolicy(
        backend="minierp_finance",
        intent="ap_invoices_due_soon",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Lists AP invoices due within N days, across every vendor (an AP-aging digest).",
        fields={
            "vendorCode": INTERNAL, "vendorName": INTERNAL,
            "invoiceNumber": INTERNAL, "docType": INTERNAL, "invoiceDate": INTERNAL, "dueDate": INTERNAL,
            "lineTotal": SENSITIVE, "taxTotal": SENSITIVE, "paid": INTERNAL,
        },
    ),
    # Cross-customer bulk AR-aging -- same trust boundary as get_invoice_details.
    # No customerId/customerName field exists (ARInvoice has no customer link in
    # this schema -- see the underlying tool's own docstring), so this can never
    # carry PII regardless of who calls it.
    "get_ar_invoices_past_due": ToolPolicy(
        backend="minierp_finance",
        intent="ar_invoices_past_due",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Lists AR invoices older than N days that may still be outstanding, across every customer (an AR-aging digest; not attributable to a customer).",
        fields={
            "invoiceNumber": INTERNAL, "docType": INTERNAL, "invoiceDate": INTERNAL,
            "lineTotal": SENSITIVE, "taxTotal": SENSITIVE, "paymentTotal": SENSITIVE, "unpaidBalance": SENSITIVE,
            "termsId": INTERNAL, "creditHold": INTERNAL,
        },
    ),
    "get_po_order_status": ToolPolicy(
        backend="minierp_finance",
        intent="po_order_status",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Shows the status of a purchase order — where it stands, vendor, and total.",
        fields={
            "orderNumber": INTERNAL,
            "orderStatus": INTERNAL,
            "orderDate": INTERNAL,
            "expectedDate": INTERNAL,
            "orderTotal": SENSITIVE,
            "vendorBAccountId": INTERNAL,
            "shipVia": INTERNAL,
            "onHold": INTERNAL,
        },
    ),
    "get_gl_account_transactions": ToolPolicy(
        backend="minierp_finance",
        intent="gl_account_transactions",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Lists the accounting transactions posted to one ledger account.",
        fields={
            "accountCd": INTERNAL,
            "date": INTERNAL,
            "module": INTERNAL,
            "batchNbr": INTERNAL,
            "refNbr": INTERNAL,
            "description": INTERNAL,
            "debit": SENSITIVE,
            "credit": SENSITIVE,
            "netMovementThisPage": SENSITIVE,
        },
    ),
    "get_sales_price": ToolPolicy(
        backend="minierp_finance",
        intent="sales_price",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Looks up the negotiated sales price(s) for a product, by price class and/or customer.",
        fields={
            "inventoryId": INTERNAL,
            "customerId": INTERNAL,
            "custPriceClassId": INTERNAL,
            "salesPrice": SENSITIVE,
            "curyId": INTERNAL,
            "uom": INTERNAL,
            "effectiveDate": INTERNAL,
            "expirationDate": INTERNAL,
            "priceType": INTERNAL,
            "breakQty": INTERNAL,
        },
    ),
    # Office artifact generation. These tools receive already-governed,
    # already-redacted structured data and persist generated files through the
    # artifact store. The artifact's own classification is returned so the UI can
    # badge/download/audit it and future approval gates can reason over it.
    "create_excel_report": ToolPolicy(
        backend="office",
        intent="create_excel_report",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=100,
        description="Creates an XLSX report artifact from structured table data.",
        fields={
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "create_powerpoint_deck": ToolPolicy(
        backend="office",
        intent="create_powerpoint_deck",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=100,
        description="Creates a PPTX deck artifact from structured sections.",
        fields={
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "create_word_report": ToolPolicy(
        backend="office",
        intent="create_word_report",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=100,
        description="Creates a DOCX report artifact from structured sections and tables.",
        fields={
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "create_pdf_packet": ToolPolicy(
        backend="office",
        intent="create_pdf_packet",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=100,
        description="Creates a PDF review packet artifact from structured sections and tables.",
        fields={
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "convert_artifact": ToolPolicy(
        backend="office",
        intent="convert_artifact",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=100,
        description="Converts an existing governed artifact into TXT or PDF format.",
        fields={
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "sourceArtifactIds": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "edit_office_document": ToolPolicy(
        backend="office",
        intent="edit_office_document",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=100,
        description="Applies a small whitelisted set of edits (e.g. set a cell, replace text) to an existing governed XLSX/DOCX artifact via ONLYOFFICE Document Builder, saved as a new version.",
        fields={
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "versionId": INTERNAL,
            "classification": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "extract_tables_from_document": ToolPolicy(
        backend="office",
        intent="extract_tables_from_document",
        account_scoped=False,
        risk=EXPORT,
        max_rows_without_approval=100,
        description="Extracts table-like data from a governed XLSX or DOCX artifact.",
        fields={
            "sourceArtifactId": INTERNAL,
            "filename": INTERNAL,
            "tableCount": INTERNAL,
            "tables": INTERNAL,
            "textPreview": INTERNAL,
            "artifact": INTERNAL,
        },
    ),
    "create_email_draft": ToolPolicy(
        backend="email",
        intent="create_email_draft",
        account_scoped=False,
        risk=WRITE,
        description="Creates a durable email draft artifact without sending it.",
        fields={
            "draftId": INTERNAL,
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "send_email_draft": ToolPolicy(
        backend="email",
        intent="send_email_draft",
        account_scoped=False,
        risk=SEND,
        approval_required=True,  # default; the email backend itself waives this for
                                  # all-internal-domain recipients under the
                                  # email_send_internal category (see EMAIL_INTERNAL_DOMAINS)
        description="Queues an approved email draft for connector-backed delivery.",
        fields={
            "sendId": INTERNAL,
            "sendStatus": INTERNAL,
            "draftId": INTERNAL,
            "approvalId": INTERNAL,
            "provider": INTERNAL,
            "message": INTERNAL,
        },
    ),
    # Deliberately no ingest_knowledge_text/ingest_knowledge_file entries: the
    # knowledge backend is read-only, mirroring AraTestEnvBE's existing Azure
    # Blob/AI Search knowledge base rather than accepting writes of its own.
    # New documents get added through AraTestEnvBE's own ingestion pipeline, not
    # through this governed gateway.
    "search_knowledge": ToolPolicy(
        backend="knowledge",
        intent="search_knowledge",
        account_scoped=False,
        risk=READ_LOW,
        description="Searches indexed local document chunks with citations.",
        fields={
            "query": INTERNAL,
            "results": INTERNAL,
            "documentId": INTERNAL,
            "documentTitle": INTERNAL,
            "filename": INTERNAL,
            "text": INTERNAL,
            "score": INTERNAL,
            "classification": INTERNAL,
        },
    ),
    "answer_from_knowledge": ToolPolicy(
        backend="knowledge",
        intent="answer_from_knowledge",
        account_scoped=False,
        risk=READ_LOW,
        description="Builds a citation-backed extractive answer from indexed local documents.",
        fields={
            "query": INTERNAL,
            "answer": INTERNAL,
            "citations": INTERNAL,
            "documentId": INTERNAL,
            "documentTitle": INTERNAL,
            "score": INTERNAL,
        },
    ),
    "draft_calendar_invite": ToolPolicy(
        backend="calendar",
        intent="draft_calendar_invite",
        account_scoped=False,
        risk=WRITE,
        description="Creates a durable .ics calendar invite draft without creating an external event.",
        fields={
            "draftId": INTERNAL,
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "title": INTERNAL,
            "start": INTERNAL,
            "end": INTERNAL,
            "timezone": INTERNAL,
            "attendees": PII,
            "location": INTERNAL,
        },
    ),
    "send_calendar_invite": ToolPolicy(
        backend="calendar",
        intent="send_calendar_invite",
        account_scoped=False,
        risk=SEND,
        approval_required=True,
        description="Queues an approved calendar invite for connector-backed event creation.",
        fields={
            "sendId": INTERNAL,
            "sendStatus": INTERNAL,
            "draftId": INTERNAL,
            "approvalId": INTERNAL,
            "provider": INTERNAL,
            "message": INTERNAL,
        },
    ),
    "create_meeting_brief": ToolPolicy(
        backend="calendar",
        intent="create_meeting_brief",
        account_scoped=False,
        risk=READ_SENSITIVE,
        description="Creates a meeting-prep brief from already-governed customer/order/shipment notes.",
        fields={
            "artifactId": INTERNAL,
            "filename": INTERNAL,
            "downloadUrl": INTERNAL,
            "classification": INTERNAL,
            "sizeBytes": INTERNAL,
        },
    ),
    "list_upcoming_meetings": ToolPolicy(
        backend="calendar",
        intent="list_upcoming_meetings",
        account_scoped=False,
        risk=READ_LOW,
        description="Lists this user's drafted calendar invites coming up in the next N days.",
        fields={
            "meetings": INTERNAL,
            "title": INTERNAL,
            "start": INTERNAL,
            "end": INTERNAL,
            "location": INTERNAL,
            "attendees": PII,
        },
    ),
    "opencode_plan_change": ToolPolicy(
        backend="code",
        intent="opencode_plan_change",
        account_scoped=False,
        risk=CODE_EXEC,
        description="Creates a read-only opencode implementation plan for a requested codebase change.",
        fields={
            "planId": INTERNAL,
            "planType": INTERNAL,
            "riskLevel": INTERNAL,
            "summary": INTERNAL,
            "proposedFiles": INTERNAL,
            "proposedCommands": INTERNAL,
            "findings": INTERNAL,
            "requiresApproval": INTERNAL,
            "message": INTERNAL,
        },
    ),
    "opencode_review_repo": ToolPolicy(
        backend="code",
        intent="opencode_review_repo",
        account_scoped=False,
        risk=CODE_EXEC,
        description="Creates a read-only repository review plan focused on governance, approval gates, and smoke coverage.",
        fields={
            "planId": INTERNAL,
            "planType": INTERNAL,
            "riskLevel": INTERNAL,
            "summary": INTERNAL,
            "proposedFiles": INTERNAL,
            "proposedCommands": INTERNAL,
            "findings": INTERNAL,
            "requiresApproval": INTERNAL,
            "message": INTERNAL,
        },
    ),
    "opencode_generate_template": ToolPolicy(
        backend="code",
        intent="opencode_generate_template",
        account_scoped=False,
        risk=CODE_EXEC,
        description="Generates a governed workflow/template draft without applying code changes.",
        fields={
            "planId": INTERNAL,
            "planType": INTERNAL,
            "riskLevel": INTERNAL,
            "summary": INTERNAL,
            "proposedFiles": INTERNAL,
            "proposedCommands": INTERNAL,
            "findings": INTERNAL,
            "templateDraft": INTERNAL,
            "requiresApproval": INTERNAL,
            "message": INTERNAL,
        },
    ),
}


def namespaced(canonical: str, backend: str | None = None) -> str:
    """minierp + get_customer_orders -> 'minierp_get_customer_orders'.

    Underscore (not dot) so the name matches the MCP/Anthropic tool-name
    charset ^[a-zA-Z0-9_-]+$ that clients (including Claude) enforce.
    """
    b = backend or TOOL_POLICIES[canonical].backend
    return f"{b}_{canonical}"


def canonical(namespaced_name: str) -> str | None:
    """'minierp_get_customer_orders' -> 'get_customer_orders' (None if unknown)."""
    for name, policy in TOOL_POLICIES.items():
        if namespaced_name == f"{policy.backend}_{name}":
            return name
    return None


def get(canonical_name: str) -> ToolPolicy | None:
    return TOOL_POLICIES.get(canonical_name)


def tools_for_backend(backend: str) -> set[str]:
    """Canonical tool names a backend owns -- used to expand a '*' category grant."""
    return {name for name, policy in TOOL_POLICIES.items() if policy.backend == backend}


def backends() -> set[str]:
    return {policy.backend for policy in TOOL_POLICIES.values()}
