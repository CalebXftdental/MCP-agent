"""Category templates -- the access grouping (replaces the old "department" model).

A **category corresponds 1:1 to a data domain / backend** (`orders`, `accounts`,
`shipments`, `finance`) and grants that backend's tools + a data-classification
**level set**. A principal holds **a set of categories** (`ConsumerRecord.categories`)
and its effective grant is the **union** of them (see policy/resolve.py), minus any
per-principal deny-overrides.

This is the seed / dev default; in prod these move to the Cosmos `categories`
container and become admin-editable. Category ids match the manifest backend names'
domain suffix (orders/accounts/shipments/finance); `backend` is the full manifest
backend name (minierp_orders, ...).

Levels are a SET, not a linear ceiling -- PII (personal data) and SENSITIVE
(financials) are orthogonal, so a scalar "max" can't express real categories.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from policy.manifest import INTERNAL, PII, PUBLIC, SENSITIVE

_BUSINESS = frozenset({PUBLIC, INTERNAL})
_FIN = frozenset({PUBLIC, INTERNAL, SENSITIVE})
_PII = frozenset({PUBLIC, INTERNAL, PII})
_FULL = frozenset({PUBLIC, INTERNAL, PII, SENSITIVE})


@dataclass(frozen=True)
class Category:
    id: str
    display_name: str
    backend: str                          # the manifest backend this category maps to
    tools: frozenset[str] | str = "*"     # canonical tool names of `backend`, or "*" (all)
    levels: frozenset[str] = frozenset()  # data-classification levels this category may see
    data_domains: list[str] = field(default_factory=list)  # ERP tables (documentation)


CATEGORIES: dict[str, Category] = {
    "orders": Category(
        id="orders", display_name="Orders", backend="minierp_orders",
        tools="*", levels=_FIN,   # order/line data incl. totals; no PII fields in this domain
        data_domains=["SOOrder", "SOLine", "InventoryItem"],
    ),
    "accounts": Category(
        id="accounts", display_name="Accounts", backend="minierp_accounts",
        tools="*", levels=_FULL,  # contacts/addresses (PII) + billing/credit profile (SENSITIVE)
        data_domains=["BAccount", "Customer", "Contact", "Address"],
    ),
    "shipments": Category(
        id="shipments", display_name="Shipments", backend="minierp_shipments",
        tools="*", levels=_BUSINESS,  # tracking/shipment numbers; order totals redacted
        data_domains=["SOShipment", "SOOrder"],
    ),
    "finance": Category(
        id="finance", display_name="Finance", backend="minierp_finance",
        tools="*", levels=_FIN,   # AR/AP/GL/PO financial figures; no contact PII
        data_domains=["ARInvoice", "APInvoice", "Vendor", "POOrder", "GLTran", "Account"],
    ),
    # Cross-customer analytics (ranking/aggregates). Seeded but NOT assigned to any
    # principal by default -- an admin grants "analytics" explicitly to enable
    # get_top_customers_by_spend. FULL levels since rankings expose customer names
    # (PII) + spend (SENSITIVE).
    "analytics": Category(
        id="analytics", display_name="Analytics", backend="minierp_analytics",
        tools="*", levels=_FULL,
        data_domains=["SOOrder (cross-customer aggregates)"],
    ),
    "office": Category(
        id="office", display_name="Office Artifacts", backend="office",
        tools="*", levels=_FULL,
        data_domains=["Generated XLSX/PPTX/DOCX artifacts"],
    ),
    "email_draft": Category(
        id="email_draft", display_name="Email Drafts", backend="email",
        tools=frozenset({"create_email_draft"}), levels=_FULL,
        data_domains=["Generated email drafts"],
    ),
    "email_send_external": Category(
        id="email_send_external", display_name="External Email Send", backend="email",
        tools=frozenset({"send_email_draft"}), levels=_FULL,
        data_domains=["Approval-gated external delivery"],
    ),
    # Same tool as email_send_external (there is only one send_email_draft) -- the
    # internal/external split is a runtime recipient-domain check inside mcp-email
    # itself (EMAIL_INTERNAL_DOMAINS), not a distinct tool. Holding EITHER category
    # is enough to call send_email_draft at all; this one additionally lets the
    # backend skip the approval gate when every recipient is on an internal domain.
    "email_send_internal": Category(
        id="email_send_internal", display_name="Internal Email Send", backend="email",
        tools=frozenset({"send_email_draft"}), levels=_FULL,
        data_domains=["Send to approved internal domains, no approval required"],
    ),
    # Read-only: search_knowledge/answer_from_knowledge are the only tools this
    # backend exposes (no ingest tools exist -- see manifest.py). Proxies the
    # SAME Azure Blob/AI Search knowledge base AraTestEnvBE's ragAgent already
    # owns (KNOWLEDGE_RETRIEVAL_BASE_URL); new documents go through
    # AraTestEnvBE's own ingestion pipeline, never through this gateway.
    "knowledge": Category(
        id="knowledge", display_name="Knowledge Base (read-only)", backend="knowledge",
        tools="*", levels=_FULL,
        data_domains=["Indexed chunks (shared with AraTestEnvBE)", "Document Q&A"],
    ),
    "calendar_draft": Category(
        id="calendar_draft", display_name="Calendar Drafts", backend="calendar",
        tools=frozenset({"draft_calendar_invite"}), levels=_FULL,
        data_domains=["Generated calendar invite drafts"],
    ),
    "calendar_send_external": Category(
        id="calendar_send_external", display_name="External Calendar Create", backend="calendar",
        tools=frozenset({"send_calendar_invite"}), levels=_FULL,
        data_domains=["Approval-gated external calendar event creation"],
    ),
    # Read-only calendar access: meeting-prep briefs (built from OTHER already-
    # governed tool calls, e.g. accounts/orders) and listing your own upcoming
    # drafted invites. Deliberately separate from calendar_draft/calendar_send_*
    # (creating/sending) so a principal can be given visibility without also
    # getting invite-creation rights.
    "calendar": Category(
        id="calendar", display_name="Calendar Read", backend="calendar",
        tools=frozenset({"create_meeting_brief", "list_upcoming_meetings"}), levels=_FULL,
        data_domains=["Meeting-prep briefs", "Own upcoming drafted invites"],
    ),
    "code_planning": Category(
        id="code_planning", display_name="Code Planning", backend="code",
        tools="*", levels=_FULL,
        data_domains=["Repository metadata", "Read-only implementation plans", "Developer workflow templates"],
    ),
    # ── Gateway-level permission markers (no MCP backend/tools of their own) ──
    # These gate ROUTES, not tool calls -- resolve.py's EffectiveGrant machinery
    # still works for them (backend="" contributes an empty tools/levels set, so
    # holding one has zero effect on tool authorization); gateway/app.py checks
    # membership directly via `_effective_category_ids()`, the same helper the
    # self-service "My Access" picker already uses.
    "files": Category(
        id="files", display_name="File Uploads", backend="", tools=frozenset(), levels=frozenset(),
        data_domains=["Raw artifact upload (POST /artifacts) -- generated artifacts from office/email/calendar tools are already gated by THOSE categories"],
    ),
    "workflow_runner": Category(
        id="workflow_runner", display_name="Run Workflows", backend="", tools=frozenset(), levels=frozenset(),
        data_domains=["Run any active workflow template (still subject to that template's own requiredCategories)"],
    ),
    "workflow_admin": Category(
        id="workflow_admin", display_name="Manage Workflow Templates", backend="", tools=frozenset(), levels=frozenset(),
        data_domains=["Create/disable/edit workflow templates -- additive alongside the admin role, not a replacement for it"],
    ),
    "agent_admin": Category(
        id="agent_admin", display_name="Manage Agent Identities", backend="", tools=frozenset(), levels=frozenset(),
        data_domains=["Create/rotate/pause agent identities -- additive alongside the admin role, not a replacement for it"],
    ),
}


def get_category(category_id: str | None) -> Category | None:
    if not category_id:
        return None
    return CATEGORIES.get(category_id)
