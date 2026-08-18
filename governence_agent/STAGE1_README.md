# Stage 1 — Governance Gateway + miniERP backend

Implements the federating-gateway design (see `design_plan.md`). All callers —
human or agent — talk MCP to the **gateway only**. The gateway authenticates
them, runs the deterministic PDP, resolves session scope, calls the owning
**backend** over MCP, redacts the result per requester, and audits.

```
caller (MCP client, per-consumer key)
        │  Streamable HTTP  →  gateway/          :8020  /mcp   (PEP: edge auth, PDP, redaction, audit)
        │                          │  MCP client (outbound)
        │                          ├─▶ mcp-minierp/    :8021 /mcp  (orders, accounts, finance, shipments, analytics)
        │                          ├─▶ mcp-office/     :8030 /mcp  (Excel/PowerPoint/Word/PDF artifacts)
        │                          ├─▶ mcp-email/      :8040 /mcp  (draft + approval-gated send)
        │                          ├─▶ mcp-knowledge/  :8050 /mcp  (RAG search / extractive Q&A)
        │                          ├─▶ mcp-calendar/   :8060 /mcp  (invite drafts, meeting briefs)
        │                          └─▶ mcp-code/       :8070 /mcp  (read-only opencode planning)
        │                                     │
        │                                     ▼
        │                          db-api.frontierdental.com (GraphQL) / local stores
```

`mcp-minierp` is ONE process serving all miniERP domains (orders/accounts/
finance/shipments/analytics) — the four per-domain backends it replaced
(`mcp-minierp-orders/-accounts/-finance/-shipments`) were deleted; nothing
referenced them once the consolidation shipped. `policy/manifest.py` still
tags each tool with a logical domain (`minierp_orders`, `minierp_finance`, …)
for authorization purposes, independent of how many processes actually serve
them.

## Connecting an agent

**Endpoint:** every caller — human dashboard, chatbot, or an external MCP
agent — talks to `POST http://<gateway-host>:8020/mcp` (Streamable HTTP).
**Never connect directly to a backend's port (8021/8030/8040/8050/8060/8070)**
— those hold real credentials, do no auth/redaction/audit themselves, and are
meant to be reachable only from the gateway (see "Prod networking" below).

**Auth (required on every call):** send either header —
`Authorization: Bearer <key>` or `X-Governance-Key: <key>` — where `<key>` is
one consumer's `GOVERNANCE_KEY_<NAME>` value (`gateway/.env.local.example`
seeds dev keys for `chatbot`/`email_bot`/`analytics`). `<NAME>` lowercased
becomes your consumer identity and determines your entitlements (below) and
category grants. Calls without a valid key are rejected before any tool runs
(`governance_core/edge.py`).

**`session_id` (required on every tool call):** any unique string you
generate (e.g. a UUID) for the conversation. Reuse the *same* value for every
tool call within one conversation — the gateway uses it to (1) remember a
resolved `customer_id` across calls so you don't have to re-supply it every
time, and (2) correlate calls in the audit trail. A new value starts a fresh
session with no remembered customer. This is also documented on the
`session_id` field of every tool's own input schema now, not just here.

**Resolving a customer first:** most account-scoped tools take an optional
`customer_id` that, once supplied, is remembered for the rest of the session.
If you only have a name, email, or phone number, call
`minierp_accounts_find_customer` (or `minierp_accounts_resolve_contact` for a
person) first and use the `customerId` it returns for every following call in
that session.

**Prefer the overview tools when you need several facts about one
customer/order** — `minierp_accounts_get_customer_overview` and
`minierp_accounts_get_order_overview` bundle what would otherwise be 3-4
separate calls into one.

## Full tool catalogue (44 tools)

All names are namespaced `<backend>_<canonical>` so multiple backends can be
federated behind one MCP surface without collisions.

### miniERP — orders (`minierp_orders_*`)
| Tool | What it does |
|---|---|
| `get_customer_orders` | List a customer's sales orders, filterable by date/status/min total. |
| `get_customer_order_total` | Total spend across completed orders, optional date range. |
| `get_order_details` | Header (status, total, date) for one order by order number. |
| `get_product_details_in_order` | Line items inside one order. |
| `get_customer_order_summary` | Aggregated order stats: counts by status, totals, monthly buckets. |
| `get_product_sales` | How a product is selling: quantity/revenue/distinct orders & customers. |
| `get_orders_by_product` | Which orders/customers bought a given inventory item. |

### miniERP — accounts (`minierp_accounts_*`)
| Tool | What it does |
|---|---|
| `find_customer` | Resolve a customer by name/email/phone/acctCd. **Call first** when you don't already have a `customer_id`. |
| `resolve_contact` | Resolve a person (not the account) by name/email/phone. |
| `get_customer_profile` | Billing/credit profile: credit limit, terms, payment method, statement cycle. |
| `get_contacts` | Contacts on a customer's account. |
| `get_addresses` | Addresses on file for a customer. |
| `get_order_addresses` | Ship-to/bill-to addresses for one order. |
| `get_customer_overview` | 360 view: profile + primary contact + addresses + recent orders + spend. Prefer this over separate calls. |
| `get_order_overview` | 360 view of one order: header + lines + addresses + shipment status. Prefer this over separate calls. |
| `get_customers_by_region` | Customers filtered by country/state/city. |

### miniERP — shipments (`minierp_shipments_*`)
| Tool | What it does |
|---|---|
| `get_customer_shipment_status` | Shipment/tracking status of a customer's most recent orders. |
| `get_shipping_by_shipment` | Tracking/invoice details by shipment number (requires a customer in scope; ownership-checked). |
| `get_shipping_by_order` | Shipment/tracking/invoice numbers linked to a sales order. |

### miniERP — analytics (`minierp_analytics_*`)
| Tool | What it does |
|---|---|
| `get_top_customers_by_spend` | Rank customers by spend over a period. Cross-customer — requires the analytics entitlement (`SENSITIVE`); other consumers get a policy denial, not an error. |

### miniERP — finance (`minierp_finance_*`)
| Tool | What it does |
|---|---|
| `get_invoice_details` | AR invoice header (total, tax, unpaid balance, terms). Not ownership-gated — `ARInvoice` has no customer link field in this schema. |
| `get_vendor_details` | Vendor profile: class, terms, currency, payment method, 1099 flag. |
| `get_vendor_ap_invoices` | AP invoices/bills for a vendor. |
| `get_ap_invoice_details` | AP invoice header (total, tax, due date, paid status). |
| `get_po_order_status` | Purchase order header status. |
| `get_gl_account_transactions` | GL transactions for an account code, with net movement for the page. |
| `get_sales_price` | Sales price records for an inventory item. **Disabled by default** (`ALLOW_PRICE_QUERY=false`) pending a security review — returns a `feature_disabled` result until re-enabled. |

### Office artifacts (`office_*`)
| Tool | What it does |
|---|---|
| `create_excel_report` | Build an XLSX from structured `tables` you already assembled; returns an artifact id/download URL. |
| `create_powerpoint_deck` | Build a PPTX from structured `sections`. |
| `create_word_report` | Build a DOCX from structured `sections`/`tables`. |
| `create_pdf_packet` | Build a PDF review packet from structured `sections`/`tables`. |
| `convert_artifact` | Convert a governed artifact to TXT or PDF. |
| `extract_tables_from_document` | Extract table-like data from a governed XLSX/DOCX artifact. |

For the four `create_*` tools, `tables`/`sections` are plain dicts you build
yourself from prior tool results — there's no fixed schema enforced beyond
"JSON-serializable"; keep each table/section entry to simple keys like
`heading`/`rows`/`bullets` and it will render sensibly.

### Email (`email_*`)
| Tool | What it does |
|---|---|
| `create_email_draft` | Create a durable draft artifact. Does not send anything. |
| `send_email_draft` | Request delivery for a draft. External sends are approval-gated; sends where every recipient is on an internal domain (`EMAIL_INTERNAL_DOMAINS`) can skip approval. |

### Calendar (`calendar_*`)
| Tool | What it does |
|---|---|
| `draft_calendar_invite` | Create a durable `.ics` draft. Does not create an external event. |
| `send_calendar_invite` | Queue an approved draft for connector-backed event creation. |
| `create_meeting_brief` | Build a meeting-prep brief from sections you've already assembled from other governed calls — it does not fetch data itself. |
| `list_upcoming_meetings` | List this consumer's drafted invites starting within N days. |

### Knowledge (`knowledge_*`)
| Tool | What it does |
|---|---|
| `search_knowledge` | Search indexed local knowledge chunks visible to this consumer. |
| `answer_from_knowledge` | Citation-backed extractive answer from indexed documents. |

### Code planning (`code_*`)
| Tool | What it does |
|---|---|
| `opencode_plan_change` | Read-only implementation plan for a requested codebase change. No files are written. |
| `opencode_review_repo` | Read-only repository review plan. |
| `opencode_generate_template` | Generate a governed workflow/template draft without applying code changes. |

Two tools exist on the `mcp-office` backend itself
(`edit_office_document`, whole-document cell/text edits via ONLYOFFICE) but
are **not** currently exposed through the gateway — they're reachable only
from the dashboard's artifact workbench, not from an MCP agent.

## Layout

| Path | What it is |
|---|---|
| `governance_core/` | Shared plane: `edge.py` (Layer-1 auth/rate/IP/size), `audit.py`, `request_context.py`, `scope_store.py`, and `policy/` (PDP). |
| `governance_core/policy/` | `manifest.py` (tool → backend/scope/field-classifications), `entitlements.py` (consumer → allowed levels), `departments.py` (department → backend/tool/level grants), `decision.py` (`decide()`), `redaction.py` (apply plan). |
| `gateway/` | `app.py` (FastMCP PEP + namespaced tools), `govern.py` (the `_govern` pipeline every call passes through), `mcp_clients.py` (outbound MCP client pool), `backend/` (the HTTP API it serves, one module per domain), `static/app.html`, `frontend/` (React dashboard). |
| `mcp-minierp/` | Consolidated ERP backend — orders/accounts/finance/shipments/analytics, one process, one shared GraphQL client (`minierp_core`). |
| `mcp-office/`, `mcp-email/`, `mcp-knowledge/`, `mcp-calendar/`, `mcp-code/` | The other local-only backends, one per domain (see catalogue above). |
| `_smoke/` | `test_policy.py` (offline PDP+redaction), `mock_minierp.py` + `e2e_client.py` (E2E harness — tool names predate the miniERP consolidation and need an update pass; not yet done). |

## Run locally

```powershell
# one-time
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r gateway\requirements.txt

# backend (real miniERP): copy mcp-minierp\.env.local.example -> .env.local, fill MINIERP_USERNAME/PASSWORD
cd mcp-minierp;   ..\.venv\Scripts\python.exe app.py   # :8021

# other local-only backends (each has its own .env.local.example)
cd mcp-office;    ..\.venv\Scripts\python.exe app.py   # :8030
cd mcp-email;     ..\.venv\Scripts\python.exe app.py   # :8040
cd mcp-knowledge; ..\.venv\Scripts\python.exe app.py   # :8050
cd mcp-calendar;  ..\.venv\Scripts\python.exe app.py   # :8060
cd mcp-code;      ..\.venv\Scripts\python.exe app.py   # :8070

# gateway: copy gateway\.env.local.example -> .env.local (dev consumer keys are prefilled)
cd gateway;       ..\.venv\Scripts\python.exe app.py   # :8020, dashboard at /dashboard
```

`startup.sh` is the production equivalent (Azure App Service, one instance):
it launches all six backends bound to `127.0.0.1` plus the gateway on the
platform-assigned `$PORT`, in that order.

## Verify

```powershell
.\.venv\Scripts\python.exe _smoke\test_policy.py          # offline checks, no servers needed -- tool names need updating post-split

# full E2E against a mock backend (no miniERP creds needed):
$env:MINIERP_ORDERS_PORT="8021"; $env:MINIERP_ORDERS_ENV_FILE="NUL"
Start-Process .venv\Scripts\python.exe _smoke\mock_minierp.py
# then start the gateway with the dev keys and:
.\.venv\Scripts\python.exe _smoke\e2e_client.py
```

`_smoke/` predates the miniERP consolidation and still references
un-namespaced-by-domain tool names — needs an update pass before it's
trustworthy again; not yet done.

## Entitlements (Stage 1, in `policy/entitlements.py`)

| Consumer | PUBLIC | INTERNAL | PII (name/email/phone/street) | SENSITIVE (totals) |
|---|:-:|:-:|:-:|:-:|
| `chatbot` | ✓ | ✓ | ✓ | ✓ |
| `email_bot` | ✓ | ✓ | ✓ | — (dropped) |
| `analytics` | ✓ | ✓ | — (masked) | ✓ |
| `finance` | ✓ | ✓ | — | ✓ |
| unknown / `legacy` | ✓ | ✓ | — | — |

Consumer identity = the `GOVERNANCE_KEY_<NAME>` that authenticated the call, or
(once a principal has a `department`) the department's grant in
`policy/departments.py`, which is the more specific and more commonly-used path.

## Known follow-ups (not Stage 1)

- **Chatbot migration.** The chatbot still points at the old `governance_service`
  and uses the un-namespaced tool names / old text result shapes. Re-point it at
  the gateway and update tool names to the new namespaced form (e.g.
  `minierp_orders_get_customer_orders`) + parse structured JSON.
- **Order-number/invoice-number/PO-number tools aren't ownership-gated.**
  `get_order_details`, `get_product_details_in_order`, `get_shipping_by_order`,
  `get_invoice_details`, `get_ap_invoice_details`, `get_po_order_status` are
  non-account-scoped — anyone who knows the number sees it. This matches the
  *current* governance_service behavior for the order-number tools; tightening
  it is a deliberate later decision, not a silent change.
- **`get_invoice_details` has no ownership check by construction, not by
  choice.** `ARInvoice` has no customer/bAccount link field in this schema at
  all (confirmed by probe: `customerId`/`bAccountId`/`customerID`/`custId` all
  rejected as invalid columns) — there is no customer-scoped AR-balance rollup
  tool for this reason.
- **CRM/marketing backend not built.** `Case`/`CROpportunity`/`CRLead`/
  `CRActivity` are confirmed `FORBIDDEN` under every credential probed so far
  (including an `administrator`-level account) — needs an access grant from
  whoever administers `db-api.frontierdental.com` before there's anything real
  to build `mcp-minierp-crm` against.
- **In-memory state.** `scope_store`, `edge` rate limits, and the `audit` ring
  buffer are per-instance (POC). Move to Redis + a durable audit sink before >1 replica.
- **Prod networking.** Backends must be reachable only from the gateway (VNet /
  private endpoint / mTLS) — see `design_plan.md` §10.
- ~~`office_edit_office_document`/`office_extract_tables_from_document` gateway
  exposure.~~ **Resolved 2026-08-17.** `office_edit_office_document` is now
  wired into `gateway/app.py`, same as `extract_tables_from_document`. Found
  and fixed via `_smoke/test_tool_registration_consistency.py` (new — run it
  after adding/renaming any tool; it checks all three registration points:
  the physical backend, `manifest.py`, and the gateway wrapper).
