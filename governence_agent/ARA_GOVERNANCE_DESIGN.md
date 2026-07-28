# ARA Governance Plane — Delegated Identity + Governed Tool Surface

**Status:** Design — decisions locked
**Builds on:** `design_plan.md` (federation) + `design_plan_v2.md` (control plane / categories) + STAGE1 (gateway + mcp-minierp)
**Scope:** Put **one governed MCP endpoint** in front of the miniERP data and enforce
**per-user, per-caller** policy for every consumer — the ARA assistant in Teams *and*
autonomous agents (e.g. an email bot). Plus the **secondary / cross-table tools** that make
that surface useful to an internal team.
**Out of scope:** ARA's RAG / KB / document-upload logic — untouched.

---

## 0. Decisions locked (quick reference)

| # | Decision | Choice |
|---|----------|--------|
| Staging | Teams first vs later | **Stage 1 = direct dashboard login/register + admin-assigned scope (no Teams, single principal); Stage 2 = delegated actor+subject (Teams + agents)** |
| Identity model | app-only vs delegated | **Actor + Subject** (delegated / on-behalf-of) — **Stage 2**; Stage 1 is single-principal |
| Subject trust | app-asserted vs token | **Mode A now** (ARA asserts the Entra user it already authenticated), audited; **Mode B** (gateway validates the Entra token) drops in later |
| Grant algebra | union vs intersection | **`actor ∩ subject`** — intersection; deny-overrides always win; deny-by-default |
| Backends | 4 vs 1 | **Collapse 4 → 1 server**; `manifest.py` keeps a per-tool `backend` tag so categories/PDP are unchanged |
| Chatbot seam | inner LLM vs direct | **Surface gateway tools directly** to the orchestrator; delete ARA's inner SQL-agent LLM |
| Row scope | model vs trusted | **Trusted `customer_id`** — resolved from identity or an explicit resolver call, never model-scraped |

---

## 1. Where we are, and the one thing that's wrong

The same data-access logic exists **three times**, all hitting the same
`db-api.frontierdental.com/graphql` with the same JWT sign-in:

- **ARA's `src/sqlAgent`** — a nested LLM agent (14 tools) that builds GraphQL and calls the
  DB directly. This is what Teams uses today, and it **bypasses governance entirely.**
- **`mcp-minierp-*`** (×4) — thin, domain-sharded MCP backends fronted by the gateway PEP.
- **`governance_service`** — the monolith the four backends were split from; kept only until
  cutover.

The gateway is already a complete enforcement point — hashed-key auth, a deterministic
category PDP, per-field redaction, audit, admin dashboard. **It just has no live consumer.**
The chatbot's real traffic never reaches it. Closing that gap is the whole job — and while we
do it, we generalize the model so it governs *every* future caller, not only Teams.

---

## 2. Target architecture

One MCP endpoint. Every caller authenticates to it; none touch the database directly. Teams
is just the first *channel* — an autonomous email agent is the same shape with no human
attached.

```
   ara-chatbot   (actor + subject) ─┐
   emailbot      (actor · no subject) ├─ MCP + API key ─▶  GOVERNANCE GATEWAY (PEP)
   analytics-agent (actor · no subject)┘                    │  resolve actor grant
                                                            │  resolve subject grant (if any)
                                                            │  effective = actor ∩ subject
                                                            │  PDP → redact → audit(actor, subject)
                                                            ▼
                                              mcp-minierp  (one server, tagged tools)
                                                            ▼
                                              db-api.frontierdental.com (GraphQL)
```

Nothing below the gateway knows about identity — the whole model lives in the control plane.
The backend collapse (4→1) is pure deployment consolidation: `manifest.py` keeps a `backend`
tag per tool, so categories and the PDP are unchanged.

---

## 3. The identity model: actor + subject

> **Staged rollout.** **Stage 1 does not require Teams.** Users come to the governance
> **dashboard/frontend**, register, log in, and an admin assigns their scope — the human
> authenticates to governance *directly*, so actor and subject **collapse into one principal**
> and the whole delegated model below (`X-On-Behalf-Of`, `actor ∩ subject`, Mode A/B, the
> Entra `/me` dependency) is **Stage 2**. Stage 1 reuses the login / register / approve /
> categories / key-mint machinery that already exists (design_plan_v2 §B.5–B.6); the PDP keys
> off the logged-in principal, exactly as it does today. Everything from §3.1 onward is Stage 2
> — read it as "how the same store + PDP extend once a channel starts calling on behalf of
> people it authenticated elsewhere."

Beyond Stage 1, "governance over everything the MCP provides" means the gateway must reason
about **two identities per call**, not one:

- **actor** — the thing holding the API key (`ara-chatbot`, `emailbot`, `analytics-agent`).
  The *channel*. Always present.
- **subject** — the human the actor acts for (`alice@frontier` in Teams). *Absent* when an
  agent runs on its own behalf.

Both resolve to an `EffectiveGrant` through the existing `resolve.py`, because the store
already models both: `ConsumerRecord.type` is `user | agent`. Users exist today only as
dashboard logins — we **promote them to policy subjects.** Almost no new data model; new
*resolution*.

### 3.1 Grant algebra — intersection

Effective grant is `actor_grant ∩ subject_grant` (and `= actor_grant` when there's no
subject). Right security property in both directions:

- the **channel** caps what can ever flow through it — ARA-in-Teams may reach orders +
  accounts but *never* GL, no matter who's asking;
- the **person** caps it further to their own entitlement — a junior sees order status but
  not spend, on any channel.

Neither a compromised app nor a privileged user can exceed its own grant. Explicit
deny-overrides always win; anything ungranted after the intersection is denied
(deny-by-default).

### 3.2 Trusting the subject — Mode A now, Mode B later

ARA already authenticates the Teams user (validates the Entra bearer via `WEB_AUTH_ME_URL`).
Under **Mode A** it asserts `on-behalf-of: alice@frontier` and the gateway trusts ARA — an
authenticated first-party principal — to assert truthfully.

> **Residual risk & compensating controls.** A compromised actor could spoof a subject.
> Mitigated by: the actor grant still caps everything (the ∩); every `(actor, subject)` pair
> is audited; the enumeration counter (§7) flags abuse. **Mode B** — ARA forwards the user's
> Entra token and the gateway validates it independently (JWKS / `aud` / `exp`) — removes the
> trust-the-app assumption and drops in without touching the PDP, because the wire field is
> defined to carry an id *or* a token.

### 3.3 Where the subject rides

Not as a tool argument — that pollutes every schema and risks the model setting it. It sits
at the edge, beside the API key: `EdgeMiddleware` authenticates the actor and reads /
validates an `X-On-Behalf-Of` header, stashing *both* into `request_context`. It binds to the
MCP `session_id` once per session, exactly as `customer_id` already does. The orchestrator LLM
never sees it.

---

## 4. Request lifecycle (hot path)

Deterministic, no LLM in the allow/deny path. The only additions to today's flow are steps
2 and 3.

1. **Caller opens an MCP session** and presents its consumer API key. *(reject → audit(auth_denied), fail closed)*
2. **Resolve the actor** from the key hash; **read & validate the `X-On-Behalf-Of` subject**
   (Mode A: trust ARA's assertion). Bind subject to the session.
3. **Resolve both grants** and compute `effective = actor ∩ subject` (or just actor if no subject).
4. **Resolve row scope** — the server-side, trusted `customer_id` bound to the session
   (never a value the model produced).
5. **PDP decides** against the effective grant + tool manifest. *(deny → audit(denied), typed refusal)*
6. **Forward to `mcp-minierp`** with sanitized args + resolved scope. *(timeout/error → circuit-break, fail closed)*
7. **Apply the redaction plan** to the backend's structured JSON per the effective grant's level set.
8. **Audit** the outcome with the **(actor, subject, tool, customer_id, redactions, latency)** tuple.

---

## 5. The governed tool surface

Today's registry is **all primary lookups that require you to already hold the key** —
`get_customer_orders(customer_id)`, `get_order_details(order_number)`. There's no way to get
*to* a `customer_id` from something a person actually types. For an internal team the missing
layer is the **resolvers** that turn a human handle into the canonical key, plus
**composites** that pre-join the tables an inquiry always needs together.

The join graph everything builds on:

```
email / phone / name ──▶ Contact.bAccountId ─┐
acctCd (human "Customer ID") ─▶ BAccount ─────┤
                                              ├─▶ bAccountId ─┬─▶ Customer (profile, terms)
                                              │               ├─▶ Address (bill / ship)
                                              │               └─▶ SOOrder ─▶ SOLine (orderNbr+orderType)
order_number ─▶ SOOrder ─▶ shipAddressId / billAddressId ─▶ Address
tracking# ─▶ shipment ─▶ order ─▶ customer        inventoryId ─▶ SOLine ─▶ order ─▶ customer (reverse)
```

### 5.1 Reusable primitives (what we pick up from the SQL agents)

The DB API offers **two mechanisms**, and the existing agents already use both — so most new
tools are composition, not new query engineering.

**A. Generic table access** — `findWithOffsetPagination` / `findWithCursorPagination` over any
table (`customer`, `contact`, `soorder`, `soline`, `address`, `baccount`, `Vendor`, `ARInvoice`,
`APInvoice`, `POOrder`, `GLTran`, `Account`) with the operator set
(`not/in/notIn/lt/lte/gt/gte/contains/startsWith/endsWith`). Cursor pagination is the right tool
for the paginate-and-sum rollups (§ quaternary).

**B. Purpose-built server-side join resolvers** — the miniERP API exposes named resolvers that
join across tables *on the server*, which is where the richest secondary/tertiary tools come from
(currently only ARA's `sqlAgent` uses these; the governance backends do not):

| Resolver | Server-side join | Feeds |
|----------|------------------|-------|
| `getSalesOrderDataByOrderNumber(orderNumber \| shipmentNumber:)` | order/shipment ⋈ invoice ⋈ shipment ⋈ tracking | shipment/tracking tools; `order_by_tracking` |
| `getOrderAddressDataByOrderNumber(orderNumber:)` | order ⋈ bill address ⋈ ship address | `get_order_addresses`, `get_order_overview` |
| `getCustomerDataBy{AccountName,Email,Phone}WithOffsetPagination` | Contact ⋈ BAccount ⋈ SOOrder (returns `Contact_* / BAccount_* / SOOrder_*`) | `find_customer` fast path |

**C. Governance backend helpers already written** (in `mcp-minierp-*/sqlagent/index.py`) —
`_gql_baccounts_for_acct_cd` / `_gql_baccount_ids_for_acct_cd` (acctCd → bAccountId, the core
resolution every account-scoped tool needs), `_gql_orders_for_customer`, `_gql_customer_order_total`,
`_gql_product_details_in_order`, `_gql_inventory_items_by_ids`, `_gql_contacts`, `_gql_addresses`,
`_gql_customer_profile`. The new tools call these, not the DB directly.

### 5.2 The four levels

A four-level taxonomy the LLM can reason about (and we register explicitly):

- **Primary** — direct `key → record`. Caller already holds the canonical id. *(the current registry)*
- **Secondary** — **resolvers / single joins**: turn a human handle into a key, or join two tables.
- **Tertiary** — **composite "360" views**: one call fans across ≥3 tables/domains for one entity.
- **Quaternary** — **aggregation & reverse lookups**: rollups, derived metrics, cross-entity/child→parent.

#### Primary (exists today — keep, unchanged behavior)

| Tool | Backend | Scoped | Sensitive fields |
|------|---------|:------:|------------------|
| `get_customer_orders(customer_id, …)` · `get_customer_order_total(customer_id, …)` | orders | ✓ | `total` = SENSITIVE |
| `get_order_details(order_number)` · `get_product_details_in_order(order_number)` | orders | — | `total`/`unitPrice`/`extPrice` = SENSITIVE |
| `get_customer_profile(customer_id)` · `get_contacts(customer_id)` · `get_addresses(customer_id)` | accounts | ✓ | contact/address = PII |
| `get_shipping_by_order(order_number)` · `get_shipping_by_shipment(shipment_number)` | shipments | ✓* | — |
| `get_invoice_details` · `get_vendor_details` · `get_vendor_ap_invoices` · `get_ap_invoice_details` · `get_po_order_status` · `get_gl_account_transactions` | finance | — | amounts/balances = SENSITIVE |

#### Secondary — resolvers & single joins  *(build first)*

| Tool (public signature) | When the LLM calls it | Builds on | Gov |
|-------------------------|-----------------------|-----------|-----|
| `find_customer(query, by="auto"\|name\|email\|phone\|acctCd)` | User names a customer by anything *except* the internal id — "orders for Dr. Smith / acct MAIN / jane@x.com". Returns candidates `{customer_id(acctCd), bAccountId, name, company, status, primaryContact}` to pick from. | `getCustomerDataBy*` (fast) or contact+baccount chain; `_gql_baccounts_for_acct_cd` | scoped:— · PII (name/email); **enumeration-metered per subject** |
| `resolve_contact(email\|phone\|name)` | Need the person/role behind a handle, not their orders. | `generate_customer_details_query` (contact) | PII |
| `get_order_addresses(order_number)` | "Where is order X shipping / billed to?" | `getOrderAddressDataByOrderNumber` | ship/bill = PII |

#### Tertiary — composite 360 views

| Tool | When the LLM calls it | Builds on (fans into) | Gov |
|------|-----------------------|-----------------------|-----|
| `get_customer_overview(customer_id)` | "Tell me everything about this customer." One call = BAccount + profile + primary contact + default bill/ship addresses + recent-orders summary + open AR balance. | accounts + orders + finance helpers | scoped ✓ · **mixed PII + SENSITIVE — classify every field** |
| `get_order_overview(order_number)` | "Full picture of order X." Header + line items + ship/bill address + shipment/tracking + invoice + customer/contact. Replaces ~4 primary calls. | `get_order_details` + `_gql_product_details_in_order` + `getSalesOrderDataByOrderNumber` + `getOrderAddressDataByOrderNumber` | mixed PII + SENSITIVE |
| `get_customer_shipment_status(customer_id)` | "Where are this customer's recent shipments?" | `_gql_orders_for_customer` → `getSalesOrderDataByOrderNumber` per order | scoped ✓ |

#### Quaternary — aggregation & reverse lookups

| Tool | When the LLM calls it | Builds on | Gov |
|------|-----------------------|-----------|-----|
| `get_customer_order_summary(customer_id, start, end, group_by="month")` | "How much / how many, over a period, by month." Counts by status, spend, AOV, first/last order; month-wise bucketing. | cursor-paginate `soorder` + client rollup (extends `fetch_customer_order_total_value`) | scoped ✓ · spend = SENSITIVE |
| `get_customer_open_items(customer_id)` | "What does this customer owe / what's outstanding?" Open orders + unpaid AR. | orders(open) + finance(AR) | scoped ✓ · SENSITIVE |
| `get_product_sales(inventory_id, start, end)` | "How is product X selling?" Qty + revenue + buyers. | soline rollup → soorder → customer | SENSITIVE |
| `get_orders_by_product(inventory_id)` | "Who ordered product X?" (reverse) | soline → soorder → baccount | PII |
| `get_order_by_tracking(tracking_number)` | "Whose package / order is this tracking number?" (reverse) | scan via `getSalesOrderDataByOrderNumber` — *may need a tracking→shipment primitive; flag* | PII |
| `get_customers_by_region(country\|state\|city)` | Territory questions — "customers in Alberta." (reverse) | address scan → baccount → customer | PII |
| `get_top_customers_by_spend(range)` *(gated)* | Cross-customer ranking. **Collides with ARA's cross-customer guardrail** — gate behind an explicit analytics entitlement, off by default. | soorder rollup across customers | SENSITIVE · **cross-customer — entitlement-gated** |

### 5.3 Registering so the LLM calls the right one

The registry only helps if the model can disambiguate. The rules we enforce in each tool's MCP
description (porting ARA's `FIELD_TO_FUNCTION_ROUTING` discipline into per-tool docstrings):

- **Public, backend-agnostic names** in `verb_object` form (`find_customer`, not
  `minierp_accounts_...`); the `backend` tag is metadata for the PDP, not the model.
- **One-line "when to call"** in every description keyed to the identifier the user has
  (order number → `get_order_*`; name/email → `find_customer` first). Prefer a **single
  tertiary composite over N primary calls** — say so in the composite's description.
- **Required vs optional inputs typed**, with `"do NOT guess identifiers; only use if
  explicitly provided"` (lifted verbatim from `CustomerDataInput`) so the model doesn't
  hallucinate an `acctCd`/`customer_id` — reinforcing the trusted-scope rule.
- **`session_id` / subject / trusted `customer_id` are never in the schema** — injected by the
  edge/adapter (§3.3, §4). For account-scoped tools the model supplies a *resolved* customer_id
  it got from `find_customer`, never one it invented.
- **Enums reused** from `input_classes.py` (`OrderStatus`, `CountryName`, `SortOrder`) so filters
  are constrained, not free text.

**Build order:** secondary resolvers first (mostly lift-and-shift from ARA's `tools.py`, and they
make *every* primary tool reachable from a name/email) → the two tertiary overviews (collapse the
most multi-call workflows) → quaternary as demand shows. `get_top_customers_by_spend` ships
behind an analytics entitlement, off by default (§10.1).

---

## 6. Governing the tools — the part that's easy to get wrong

Adding tools isn't just writing functions; each must be **declared to the PDP** or governance
silently fails open.

- Every tool needs a `manifest.py` entry: a `backend` tag, an `account_scoped` flag, and **a
  classification for every field it returns**. Unlisted fields default to `INTERNAL` and get
  exposed.

> **The composite leak risk.** Composites are the danger. `get_order_overview` emits order
> totals (`SENSITIVE`) *and* contact PII *and* addresses (`PII`) in one payload — it **widens
> the redaction surface**. If any emitted field is unclassified, redaction leaks it. Treat
> field-classification for composites as a reviewed security change, not a config toggle
> (design_plan_v2 §B.1).

- **Which category owns a cross-domain composite?** With the 4→1 collapse: tag each field by
  its originating domain so the union grant still redacts correctly, and place the tool itself
  in the category the *inquiry* belongs to (likely a new `support_desk` category granting the
  composites).
- **Resolvers are enumeration surface.** `find_customer` by email is exactly the scraping
  signal. Rate-limit per subject, audit, and consider gating broad search behind a
  "can-search-widely" entitlement.
- **Structured output only.** Redaction works on field keys, so tools return flat, typed JSON
  (the `structured.py` pattern) — never LLM-narrated prose. The top orchestrator LLM narrates.

---

## 7. Two axes, kept distinct

The most common way to muddle this design is to conflate *who is asking* with *whose records
they want*. They're independent, and both are enforced.

- **subject — who is asking** *(authorization)*: the employee. Governs **which tools & levels**
  they may use at all. Set per-user from the admin panel.
- **customer_id — whose records** *(row scope)*: the record selector. One employee legitimately
  looks up many customers; this just picks which.

The safeguard that ties them together is the **`subject → distinct customer_ids over time`**
counter (design_plan_v2 §C.2) — the enumeration/scraping guard, and the reason the resolvers
must be metered per subject.

---

## 8. Provisioning & admin visibility

- **JIT provision.** First time the gateway sees `alice@frontier`, auto-create a `type:user`
  principal with a deny-heavy default. She's visible but can do almost nothing until elevated.
- **Admins elevate from the panel.** The existing Consumers CRUD already edits
  categories/overrides on any principal — users are just principals. Unknown-subject denials
  surface in the **access-suggestions queue** you already built:
  *"alice tried `get_customer_order_total` 5× — not granted [Grant]."*
- **Audit carries the pair.** `audit.log_call` records `(actor, subject, tool, customer_id, …)`,
  so an admin sees *"ara-chatbot on behalf of alice → get_customer_orders(4905)."* The
  dashboard gains a per-subject filter — the "know who asked in Teams" requirement.

---

## 9. What actually changes in code

The identity model is contained to the plane you already own. The store, manifest, backend,
and tool registry are untouched *by the identity change* (they change only to *add tools*).

| Area | Location | Change |
|------|----------|--------|
| Edge | `governance_core/edge.py` | Read + validate `X-On-Behalf-Of`; resolve subject; stash actor + subject in context |
| Context | `request_context.py` | Add `subject_record_ctx` |
| Resolve | `policy/resolve.py` · `decision.py` | Intersect actor ∩ subject grants; deny-overrides win |
| Audit | `governance_core/audit.py` | Log the `(actor, subject)` pair; dashboard subject filter |
| Backend | `mcp-minierp-*` → `mcp-minierp` | Collapse 4 servers into 1; extract shared `minierp_core` lib; add secondary tools |
| Manifest | `policy/manifest.py` | Entry + field classifications per new tool; `support_desk` category |
| Chatbot | `AraTestEnvBE/src/orchestrator` | Replace `database_agent_tool` with an MCP client; surface gateway tools; inject session/subject/customer_id; **delete `src/sqlAgent`** |
| Cleanup | `governance_service` · `auth_token.py` | Delete after cutover (removes hardcoded creds) |

---

## 10. Open questions

### 10.1 Resolved

- **Row-scope model → unrestricted, tool-gated.** Employees may look up any customer; the
  **subject grant** controls which tools/levels, `customer_id` is just the selector returned by
  `find_customer`, and the `subject → distinct customer_ids` counter + audit is the guard.
  **Consequence: no user→account mapping is needed** — the trusted `customer_id` comes from the
  employee's explicit resolver call, not from their identity.
- **Cross-customer aggregates → allowed behind an entitlement.** `get_top_customers_by_spend`
  and rankings ship gated to an explicit analytics entitlement, off by default; that entitlement
  overrides ARA's cross-customer guardrail only for granted subjects.
- **Persistence (first cut) → FilePolicyStore + durable audit sink.** Policy/users in
  `FilePolicyStore` (single instance); audit to a durable sink (Cosmos/Log Analytics) from day
  one so "who asked in Teams" persists across restarts. Redis counters deferred.
- **Hosting / secrets → App Service, credentials in env vars.** The governance gateway runs on
  Azure App Service; `MINIERP_*` (and session/pepper) live in App Service application settings
  (env vars) for the first cut. Key Vault + managed identity is a hardening step, not a blocker.
- **Accepted design defaults (B-items):** company scope = explicit tool input defaulting to both
  companies, validated against `{2,11}`; `session_id` = the governance chat/conversation id,
  principal bound once per session; MCP→LangChain adapter = hand-rolled (to hide/inject
  session + resolved customer_id); JIT default category = read-only, no-PII, no-SENSITIVE baseline
  (order status, tracking).

### 10.1a Stage 1 scope (locked)

- **Data-only.** Governed miniERP data chat. **No RAG/KB in Stage 1** — RAG stays in ARA and is
  composed in later.
- **Thin orchestrator.** A minimal LLM tool-loop co-located with the governance service, calling
  the governed MCP under the logged-in session's grant. ARA's guardrails / antispam / competitor /
  RingCX escalation are **not** relocated in Stage 1.
- **All four tool levels ship in Stage 1** (primary + secondary + tertiary + quaternary), not a
  subset. Consequence: the composite field-classification review (C1) and the
  `get_order_by_tracking` primitive check (C2) are **Stage-1 work**, not deferred.
- **Additive, not destructive.** Stage 1 stands up the new governed surface for dashboard users;
  **ARA keeps running unchanged for existing users.** Deleting ARA's `src/sqlAgent` /
  `governance_service` / hardcoded creds is the **Stage-2 cutover**, not Stage 1.

### 10.2 Remaining before implementation

**Stage 1 has no external blockers** — it rides on the existing dashboard login/register/approve/
scope machinery, so the Entra `/me` question does not apply. Two items to settle, both
resolvable in-house:

- **Grant delivery for the chat path → RESOLVED: session-internal, co-located.** The Stage-1
  chat surface + LLM orchestrator live in / next to the governance service; the orchestrator runs
  server-side and the PDP keys off the dashboard **session's** `consumer_id` — no per-user key
  round-trip, one deployable. (Alternative, deferred: ARA stays the frontend and calls `/mcp`
  with each user's minted key.)
- **Composite classification owner (C1).** Every field each tertiary/quaternary tool emits must
  be classified in `manifest.py`; per design_plan_v2 §B.1 this is a reviewed security change —
  assign a sign-off owner.
- **`get_order_by_tracking` primitive (C2).** Confirm the DB supports a tracking#→shipment
  lookup, or drop the tool (the existing resolver keys on order/shipment number, not tracking#).

### 10.3 Stage 2 (deferred — not blocking Stage 1)

- **Subject identity source.** What canonical id `WEB_AUTH_ME_URL` returns that a channel (ARA
  in Teams) can assert in `X-On-Behalf-Of` — Entra **object id** (preferred) or UPN/email.
- Autonomous-agent principals (emailbot, analytics-agent) and their onboarding.

### 10.4 Deferred to hardening (not blocking)

Mode B (verifiable Entra-token validation), mTLS / private networking, Redis-backed distributed
counters, Key Vault + managed identity, replay protection.

---

## 11. Stage 1 build sequence

Each step ships something runnable and leaves the system working. Steps 1–2 are safe refactors;
**steps 4–5 are the demoable vertical slice** (a logged-in user gets a governed, redacted answer);
6–7 fill out the surface; 8 is polish. Verification reuses the existing `_smoke/` suites
(`test_policy`, `test_categories`, `test_store`, `test_file_store`, and the `*_http` live smokes)
plus new golden PDP cases for the composites.

**0 · Baseline & harness.** Get the current gateway + backends + dashboard running locally
(mock or real DB) and refresh the post-domain-split `_smoke` E2E, which STAGE1_README flags as
stale. *Deliverable:* runnable baseline, green smoke. *Verify:* existing primary tools answer
end-to-end through the gateway.

**1 · Shared `minierp_core` lib (no behavior change).** Extract the duplicated
`graphql_client.py` + boilerplate (`_company_ids`, `_clamp_page*`, `_json_tool_result`, token
lifecycle) into one package the backends import. Pure refactor. *Verify:* smoke/E2E still green;
tool outputs byte-identical.

**2 · Collapse 4 → 1 server.** One `mcp-minierp` FastMCP app importing all domain tool modules;
per-tool company/region flag (default both `{2,11}`); `manifest.py` backend tags unchanged;
`gateway/mcp_clients.py` URLs collapse to one. *Verify:* every existing primary tool reachable
exactly as before; categories/PDP unchanged.

**3 · Persistence + durable audit.** Select `FilePolicyStore` (`GOVERNANCE_STORE_FILE`) for
policy/users; add a durable audit sink behind `audit.py` (App Insights / Log Analytics is the
low-friction default on App Service; Cosmos an option) while keeping the in-memory ring buffer
for the live dashboard. *Verify:* register→approve→key survives a restart; a tool call lands in
the durable sink and persists. This is what makes "who asked" real.

**4 · Secondary resolvers + `support_desk` category.** Add `find_customer`, `resolve_contact`,
`get_order_addresses` (reuse `getCustomerDataBy*`, `getOrderAddressDataByOrderNumber`,
`_gql_baccounts_for_acct_cd`); gateway wrappers; manifest entries (PII); the `support_desk`
category granting the tool set; per-principal enumeration rate-limit on `find_customer`.
*Verify:* resolve by email/name/acctCd → candidates; PDP redacts PII per grant; rate-limit trips.

**5 · Thin session-keyed orchestrator + chat surface** *(pivotal integration).* A minimal LLM
tool-loop in/next to the governance service that lists the gateway tools, **hides + injects
`session_id` and the resolved `customer_id`**, resolves the caller's grant from the dashboard
**session** (session-internal), and narrates the structured JSON. Chat endpoint + minimal UI on
the dashboard behind login. *Verify (end-to-end):* a logged-in user asks "orders for jane@x.com"
→ `find_customer` → `get_customer_orders` under their grant → redacted answer; an ungranted user
is denied; audit shows the principal. Later tool steps are picked up automatically via
`tools/list`.

**6 · Tertiary composites.** `get_customer_overview`, `get_order_overview`,
`get_customer_shipment_status` — backend composition of existing primitives + join resolvers;
gateway wrappers; **manifest with every emitted field classified (C1 — reviewed security change;
mixed PII + SENSITIVE).** *Verify:* pre-joined view returns; redaction correctly
drops/masks across the whole payload per grant; golden PDP tests.

**7 · Quaternary aggregations + reverse.** `get_customer_order_summary`, `get_customer_open_items`,
`get_product_sales`, `get_orders_by_product`, `get_customers_by_region`, `get_order_by_tracking`
(C2: confirm the tracking→shipment primitive or drop), and `get_top_customers_by_spend` behind
the analytics entitlement (off by default). *Verify:* rollups correct (cursor-paginate + sum);
reverse lookups; entitlement gate blocks top-customers unless granted.

**8 · Admin visibility & Stage-2 seam.** Per-principal call history in the dashboard from the
durable sink ("who asked"); access-suggestions queue works for the new tools; document the
Stage-2 cutover (add `X-On-Behalf-Of` + intersection; re-point ARA/Teams; delete `src/sqlAgent`
/ `governance_service`). *Verify:* admin sees who ran what and can grant/deny from suggestions.

**9 · Durable Cosmos policy store** *(Stage-1 requirement — per-user policy + default
category scope in Cosmos).* `store/cosmos.py::CosmosPolicyStore` implements the same
`PolicyStore` write surface as the file store, backed by Cosmos Core/SQL containers
(auto-created: `consumers` `/consumerId`, `categories` `/id`, `accessRequests`
`/consumerId`, `config` `/id`). Reads are cache-served with a short TTL + refresh-on-write
so the auth hot path stays fast while reflecting other instances' writes. First run seeds
the same defaults (code category templates + env admin/consumers). Selected by
`get_store()` when `GOVERNANCE_COSMOS_URL`/`_CONNECTION_STRING` is set (precedence:
Cosmos > file > local). *Note:* this makes **policy** durable + shared; rate-limit
counters, session scope, and the audit ring buffer are still per-instance — full
scale-out also needs Redis (counters/session) + a shared audit sink.

**Milestones:** steps 0–3 = governed foundation on durable state; **steps 4–5 = first
demoable governed answer** (the Stage-1 thesis proven); steps 6–8 = full surface + admin
story; step 9 = durable Cosmos-backed policy.
