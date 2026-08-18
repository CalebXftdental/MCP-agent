# Finalize Stage 1 — Close the Known Gaps Before Stage 2 Backends Land

**Status:** Proposed (this doc; design/planning only — nothing here is built yet
unless explicitly marked "confirmed built" below).
**Builds on / supersedes stale status claims in:** `STAGE1_README.md` ("Known
follow-ups" section), `expansion.md` (superseded, see its banner), `STAGE2_PLAN.md`,
`improve_myworkflow_with_nodes.md` (status header is now wrong — see §3),
`digest_persoanl_kb.md` (verified current, see §4).
**Scope:** Before spending effort on Stage 2's new backends (`mcp-hubspot`,
`mcp-acumatica`, the shared scaffold), finish what Stage 1 already started but left
half-built: the miniERP tool surface, the workflow-graph node set, and the personal
knowledge tier. `STAGE2_PLAN.md`'s shared-scaffold argument only pays off if the
platform underneath it isn't already carrying known holes — cheaper to close these
now, with 6 backends and one node engine, than after 8-10 backends exist.

---

> **Read this before implementing anything below.** Every row in every "decisions
> locked" table in this doc is this session's best-effort design given what we knew
> talking it through — not a final spec, and not a green light to implement
> unquestioned. Docs in this repo go stale fast in both directions: `expansion.md`
> and `improve_myworkflow_with_nodes.md` both turned out to be wrong about "what's
> already built" within weeks. Before starting implementation on **any** item here:
> 1. **Re-verify the relevant claim against the live code** — grep for the thing,
>    read the actual file, don't trust the doc's description of current state.
> 2. **Ask the user clarifying questions** about anything with a real tradeoff, a
>    security/privacy implication, or a dependency on something outside this repo
>    (IT-owned infra, credentials, a coworker's system, a business decision like
>    which entity to pilot a write tool on) — rather than silently proceeding with
>    whatever this doc currently says. Treat "the doc says X" as a hypothesis to
>    confirm with the person who'll actually own the consequences, not an
>    instruction to execute.
> 3. Only after 1-2, start writing code.

## 0. Decisions locked (quick reference)

| # | Decision | Choice |
|---|---|---|
| Sequencing vs. Stage 2 | build Stage 2 backends now vs. close Stage 1 gaps first | **Close Stage 1 gaps first** — new miniERP read tools, the loop node, and the personal-KB PDF-extraction fix, before `mcp-hubspot`/`mcp-acumatica`. |
| "Exhausted miniERP" framing | technical ceiling vs. investment ceiling | **Investment ceiling, not technical — with one confirmed exception.** `db-api`'s `findWithOffsetPagination(table, ...)` can reach any table the credential is granted on, and direct probing 2026-08-17 confirmed `POLine`/`ARAdjust`/`APAdjust`/`SOShipment`/`SOShipLine`/`SOAdjust`/`INItemXRef`/`EPEmployee`/`SalesPerson`/`CurrencyRate`/`Ledger` are all granted (mostly under the `admin` profile, not `default` — a real wiring detail, not just a manifest entry) — so for those, the gap really is engineering time, as this row originally claimed. But `INSiteStatus`/`INItemXWarehouse` (warehouse stock-on-hand) and `INLotSerStatus`/`INLotSerClass` (lot/serial tracking) came back `FORBIDDEN` under every credential probed, same as CRM — a real technical ceiling for those specific entities until a broader-scoped credential is provisioned, not an unbuilt tool. See §2's inventory row. |
| CRM entities in miniERP | keep trying vs. accept the block | **Accept the block.** `Case`/`CROpportunity`/`CRLead`/`CRActivity` are confirmed `FORBIDDEN` under every credential probed (`STAGE1_README.md`). This is exactly why `mcp-hubspot` (Stage 2) carries that weight instead — not a reason to keep probing `db-api`. |
| Loop/pagination gap | one node kind vs. two | **Two distinct node behaviors**, not one generic "loop" — "see every row" (paginate-until-exhausted) and "do something per row" (for-each/fan-out) solve different problems and have different failure modes; conflating them into one node invites a design that does neither well. |
| Field catalog (Phase 1 of `improve_myworkflow_with_nodes.md`) | leave as the scratchpad vs. finish the persisted version | **Finish it.** The copilot works today off `workflow_scratchpad.py` (in-memory, per-session, expires on idle) — real, but not what unblocks the *manual* canvas UI's field dropdowns, and it re-samples the same tools every new conversation. |
| Personal KB | new design needed vs. already designed | **Already designed** (`digest_persoanl_kb.md`, decisions locked). Verified current against the live code (§4) — just needs building, and its one hard blocking prerequisite (real PDF extraction) is worth fixing regardless of whether personal-KB ships next. |
| Workflow versioning/rollback + test-run mode | leave as an open question vs. commit to building | **Commit to building** (§6.2), reusing `artifact_store.py`'s existing `versions.json`/`ArtifactVersionRecord` pattern rather than inventing a new one. Cohere North productizes this as a core capability of the same kind of builder we're building — treat it as scope, not a maybe. |
| Per-workflow/automation **time** rollup | new instrumentation vs. aggregation over existing data | **Aggregation only — already fully recorded.** Confirmed: `audit.log_call()` already captures `latency_ms` per tool call, tagged `session_id="workflow:"+run.run_id` (`workflow_graph_interpreter.py:361`). Zero new instrumentation; just join audit→run→template and sum. |
| Per-workflow/automation **token** rollup | new instrumentation vs. aggregation over existing data | **New instrumentation required — genuinely not captured today.** Confirmed: `orchestrator.py` discards every provider response's `usage` object at all ~4 LLM-call sites. Needs real capture, unlike the time rollup above. |

---

## 1. Why finish Stage 1 before Stage 2 backends

Every new backend `STAGE2_PLAN.md` proposes (`mcp-hubspot`, `mcp-acumatica`) will be
built on the same three load-bearing pieces this doc audits: the miniERP tool
pattern (composed governed tools over a generic query primitive), the workflow-graph
node engine (which every future workflow — win-back, AP/AR digests, and whatever a
HubSpot or Acumatica-backed workflow needs next — runs on), and the knowledge/RAG
pipeline (which `STAGE2_PLAN.md` §10's embedding work is shared infrastructure for).
Gaps in any of the three get inherited by every backend built after them. Closing
them now, while there are 6 backends and one node engine to touch, is strictly
cheaper than after 8-10 backends depend on the current shape.

---

## 2. Exhaust miniERP: the actual gap list

Current live tool count: **33** (`mcp-minierp/app.py`, counted directly against
`@mcp.tool()` — `STAGE1_README.md`'s "44 tools" figure covers all backends and is
already stale for miniERP alone). `get_po_line_items`, `get_ar_payment_history`,
and `get_ap_payment_history` shipped 2026-08-17 (see below) — the other rows are
still open. Gaps, in rough priority order (most operationally useful first):

| Gap | Why it matters | Candidate tool(s) |
|---|---|---|
| Inventory/stock levels | **Confirmed a real technical ceiling, not just unbuilt** — direct probe of `INSiteStatus`/`INItemXWarehouse` against db-api.frontierdental.com returned `FORBIDDEN` under every credential profile available (default and admin). `InventoryItem` (item master) itself IS reachable and unlocks a narrower `get_inventory_item_details` (description, class, UOM, preferred vendor, UPC — not stock-on-hand), but real stock/warehouse-balance visibility is blocked until a credential with `INSiteStatus` access is provisioned. This is the one row in this table where §0's "investment ceiling, not technical" framing does NOT hold. | `get_inventory_item_details` (buildable now, item-master only); `get_stock_on_hand` (blocked) |
| ~~PO line items~~ | ~~`get_po_order_status` is header-only, same asymmetry `get_order_details` had before `get_product_details_in_order` was added for sales orders.~~ **Shipped 2026-08-17.** `POLine` was re-probed and found granted under the `admin` profile (the finance module's original note that it was `FORBIDDEN` was stale). `get_po_line_items` is live in `sqlagent/finance/index.py` + registered in `app.py` + classified in `policy/manifest.py`, verified end-to-end against production (`PO0323342` → 5 real line items). | `get_po_line_items` ✅ |
| ~~Payment records (AR/AP)~~ | ~~Invoice tools expose `unpaidBalance`/`paid`, not *when/how* something was paid.~~ **Shipped 2026-08-17.** `ARPayment`/`APPayment` were probed and found reachable but useless for this purpose (no amount or customer/vendor link field in this schema — only cash-account/deposit/card metadata). The real invoice-to-payment join lives on `ARAdjust`/`APAdjust` instead (Acumatica's application DACs), which DO carry `customerId`/`vendorId` directly — notably, `ARAdjust.customerId` exists even though `ARInvoice` itself has no customer link (see `get_invoice_details`'s own docstring). `get_ar_payment_history`/`get_ap_payment_history` are live, verified end-to-end against production. | `get_ar_payment_history`, `get_ap_payment_history` ✅ |
| Credit hold / order hold status | `get_customer_profile` has credit limit/terms but not whether an account or order is on hold right now. | `get_customer_credit_status`, extend `get_order_details` |
| Cross-customer shipment exceptions | `get_customer_shipment_status` is per-customer only. `expansion.md`'s "Shipment Exception Report" use case still has no bulk tool behind it. | `get_shipment_exceptions` (mirrors `get_ap_invoices_due_soon`'s cross-entity shape) |
| Vendor-side aggregates | Customer-side analytics exist (`get_top_customers_by_spend`, `get_customer_order_recency`); nothing vendor-side beyond one-vendor-at-a-time. | `get_top_vendors_by_spend` |
| GL statement-level rollups | `get_gl_account_transactions` is per-account; no trial-balance/period-summary aggregate. | `get_gl_period_summary` |
| CRM entities | **Hard ceiling, not a gap to close here.** Confirmed forbidden under every credential probed. | — build in `mcp-hubspot` instead |

Each new tool follows the existing pattern exactly: a narrow, purpose-built function
in the relevant `sqlagent/<domain>/` module, registered with `@mcp.tool()` in
`mcp-minierp/app.py`, with `policy/manifest.py` field classifications added at
registration time (per `STAGE2_PLAN.md` §3's "no tool without a risk tier" rule,
which should apply retroactively to Stage 1's own remaining growth, not just Stage 2
backends).

---

## 3. Workflow node gaps

### 3.1 Status correction

`improve_myworkflow_with_nodes.md` still says "Status: Proposed — nothing in this
doc is built yet." Confirmed by reading the code: **wrong.** Phase 2 (the `filter`
node — `GraphNode.kind == "filter"`, `_execute_filter_node`) and most of Phase 3
(the copilot — `workflow_scratchpad.py` + the copilot's tool-loop in
`orchestrator.py`/`app.py`) are live, demoed end-to-end in
`howtocreatemyworkflowdemo.md`. Only Phase 1 (a persisted, sampled field catalog)
is genuinely unbuilt. That doc's status header needs fixing to match — done as part
of this pass (see the banner added to it alongside this doc).

### 3.2 The loop gap (confirmed missing, two distinct designs needed)

`GraphNode.kind` today: `trigger | tool_call | approval_gate | llm_transform |
filter`. No iteration construct exists. This is two separate missing capabilities:

**(a) Paginate-until-exhausted.** Every bulk `tool_call` node only ever returns one
page (`get_ap_invoices_due_soon`'s default `page_size=250`,
`get_customers_by_region`'s `25`). A published, *scheduled* workflow ("flag
customers needing a win-back nationally") silently only ever sees the first page,
forever — a correctness bug that won't announce itself. `minierp_core/graphql_client.py`
already has the right logic (`paginate_all` — loop pages, cap at N, set
`truncated: true`) but only inside a tool implementation, never at the graph level.

Proposed: a `paginate` flag (or a distinct `kind == "paginate_tool_call"`) on a
`tool_call` node that, when set, repeats the underlying call bumping `page` until
`hasMore` is false or a hard cap (`max_pages`, default matching `paginate_all`'s)
is hit, accumulating `items` and setting `truncated` on the node's output exactly
like the existing Python helper does. No LLM involved; pure deterministic looping,
same as the `filter` node's own execution model.

**(b) For-each / fan-out per row.** The win-back-radar demo produces one digest
PDF/email listing every matched customer together — there's no way to say "for each
matched customer, draft a *personalized* email." Needed for any workflow where the
row-level personalization matters more than a batch summary.

Proposed: new `GraphNode.kind == "loop"`. Config shape (mirrors the `filter` node's
binding conventions):

```jsonc
{
  "input": {"source": "node", "node_id": "n_filter", "path": "matched"},  // array in
  "body": ["n_draft_email"],           // node ids to run once per item, in this loop's scope
  "item_binding_name": "loop_item",    // how body nodes reference the current item
  "max_iterations": 500                // hard cap, matches the "never silently truncate" convention
}
```

Body nodes reference the current item via a new binding source
(`{"source": "loop_item", "path": "..."}`), resolved the same way
`_resolve_binding` already resolves `trigger`/`node` sources
(`workflow_graph_interpreter.py:77`). Output:
`{"iterations": N, "results": [...], "truncated": bool}`. Each iteration's body
nodes get their own step-status entries in the run (so a partial failure mid-loop
is visible per-item, not just as one opaque "loop failed").

**Safety implications, both designs:**
- A `loop` node whose body contains a `send`-risk `tool_call` (e.g. per-customer
  `create_email_draft` → `send_email_draft`) must still be reachable only through
  an `approval_gate`, per the existing `validate_graph` rule — fan-out doesn't get
  an exemption from that check; if anything it raises the stakes (N sends instead
  of one).
- `max_iterations`/`max_pages` caps are non-negotiable and always surfaced as
  `truncated: true` rather than silently dropped, matching `paginate_all` and the
  `filter` node's own precedent.
- Runaway-loop protection: a `loop` node's total body-node executions count toward
  whatever per-run tool-call budget the interpreter already enforces (if none
  exists today, that's a gap this design surfaces, not one it can silently assume
  away — worth confirming before shipping).

### 3.3 Other node candidates

- **Retry/error-handling node (or per-node retry policy).** Now that
  `automation_store.py` supports real unattended scheduled runs, a single
  transient GraphQL failure killing an entire scheduled run — with no retry —
  is a real risk. Either a `retry_policy: {max_attempts, backoff_sec}` field on
  `tool_call`/`filter`/`loop` nodes, or a wrapping `kind == "retry"` node; the
  former is less invasive and matches how config already lives on nodes today.
- **Finish the persisted field catalog** (Phase 1 of `improve_myworkflow_with_nodes.md`,
  §1 of that doc) — a `governance_core/tool_catalog.py` store, sampled through
  `_govern` under a dedicated service identity, refreshed periodically +
  admin-triggered. Unblocks the manual (non-copilot) canvas UI's field dropdowns
  and stops the copilot re-sampling from scratch every new conversation.

---

## 4. Personal knowledge base (`digest_persoanl_kb.md`) — verified current, not stale

Checked directly against the code (2026-08-17): its claims hold up as written.
`governance_core/knowledge_store.py:88` still does `payload.decode("latin-1", "ignore")`
for PDF "extraction" (confirmed — real PDF parsing has not been added), and
`mcp-knowledge/app.py` still registers only `search_knowledge`/`answer_from_knowledge`
— none of the personal-tier tools (`ingest_my_document`, `search_my_documents`,
`answer_from_my_documents`, `list_my_documents`, `delete_my_document`) exist yet.
That doc's design (Cosmos containers partitioned by owner, no admin bypass,
Qwen3-Embedding-backed search shared with `STAGE2_PLAN.md` §10's tool-retrieval work)
is sound and decisions-locked — no changes proposed here.

**One reason to fold it into this "finish Stage 1" pass specifically:** the PDF
extraction fix (`pypdf`/`pdfplumber`) is a blocking prerequisite for personal-doc
upload regardless of sequencing, and it's a small, self-contained fix — worth doing
now rather than rediscovering it as a blocker mid-Stage-2.

---

## 5. Open questions to check, not assumed gaps

Carried over from the previous review pass — flagged because they're plausible
risks, not because they're confirmed:

- **Does a failed scheduled automation notify its owner?** `AutomationSchedule`
  tracks `last_status`/`run_count`; unclear whether failure triggers any alert.
  Silent failure of an unattended report is worse than a slow one.
- **Per-run tool-call budget enforcement** — referenced in §3.2's safety note;
  confirm whether one exists today before the `loop` node ships, since fan-out
  is exactly the kind of node that would expose a missing one.

(The rollback/versioning question that used to sit here is resolved into a concrete
plan item in §6.2 below, not just flagged — a productized competitor treats it as
a must-have, not a nice-to-have.)

---

## 6. Lessons from Cohere North (a productized version of the same thing)

North (Cohere's enterprise agent platform, GA through 2026 — see `STAGE2_PLAN.md`
§10 for the connector-scope and embed/rerank angle already covered there, and §6-§9
for the new non-ERP backends this session's conversation also added) is the
closest publicly-documented analog to what this repo is building: a governed,
low-code workflow builder with human-in-the-loop approval, sitting on top of an
enterprise's own tools and data. Worth checking what they productized against what
we've built or are planning, specifically for the workflow-builder piece this doc
is about (not re-litigating the connector/embedding angle — that's `STAGE2_PLAN.md`'s).

### 6.1 What this validates (no new work — confirms decisions already made)

- **Loops and branching as native builder primitives, not an LLM re-deriving logic
  per run.** North's own marketing explicitly calls out "a clear and auditable
  execution path with loops and branching" as core to its workflow builder. That's
  the same conclusion §3.2 already reached independently (and the same reasoning
  `improve_myworkflow_with_nodes.md` used to reject "let the LLM re-derive the
  logic at runtime" in favor of the deterministic `filter` node) — a second data
  point, from a team solving the same problem at product scale, that this is the
  right shape rather than over-engineering.
- **Human-in-the-loop at "key moments," not everywhere.** Matches the existing
  `approval_gate` node design — selective, not blanket, approval gating.
- **Auditable execution path.** Matches `audit.py`'s existing chokepoint design.

### 6.2 What North productized that we haven't built yet (new gaps to add to scope)

Checked directly against the code (2026-08-17) — this splits into one thing that's
already fully recorded and just needs a rollup, one that needs real new
instrumentation, and one that should reuse an existing pattern rather than invent
a new one. Don't treat these as equally-sized work.

- **Time — already recorded, purely an aggregation gap.** `audit.log_call()`
  (`governance_core/audit.py:291`) already captures `latency_ms` per tool call.
  The interpreter already tags every one of those calls with
  `session_id = "workflow:" + run.run_id` (`workflow_graph_interpreter.py:361`).
  So "how long did this run take, broken down by tool" is already sitting in the
  audit stream today — nobody's ever written the query. Close this by joining
  audit `call` events (filtered on `session_id` starting `"workflow:"`) to the
  workflow-run store's `run_id → template_id`/`automation_id` mapping, summing
  `latency_ms` and counting calls, surfaced on an admin page. **Zero new
  instrumentation.**
- **Tokens — a genuine, unbuilt gap.** Confirmed: `orchestrator.py` calls
  OpenAI-compatible/Azure OpenAI/Anthropic and discards the `usage` object every
  one of those provider responses already includes (`prompt_tokens`/
  `completion_tokens` or `input_tokens`/`output_tokens`) at all ~4 LLM-call sites.
  Only `llm_transform` nodes and the copilot's own conversation call an LLM at all
  (`tool_call`/`filter`/`loop`/`approval_gate` never do), so this is sparser than
  call-latency but can still dominate cost for an `llm_transform`-heavy workflow
  or a long copilot session. Needs real work: capture `usage` at each call site,
  extend `audit.log_call` with optional `tokens_in`/`tokens_out` (or add a sibling
  `log_llm_usage`), tagged with the same `session_id="workflow:<run_id>"`
  convention so it joins the same way the time rollup does.
- **Versioning with rollback, and a real test/staging run mode before production
  publish.** North explicitly supports "versioning to keep track of changes" and
  lets a builder "test and iterate before publishing to production" — a distinct
  capability from our existing draft/publish split and `Check` (`validate_graph`)
  step, which only validates structure and doesn't let someone run a draft against
  real data before it goes live on a schedule, nor see/restore a prior published
  version. **Don't design this from scratch** — `governance_core/artifact_store.py`
  already implements exactly this shape for artifacts: a `versions.json` per
  record, `ArtifactVersionRecord{version_id, version_number, ...}`,
  `list_versions()`/`get_version()`. Mirror it for `workflow_graph_store`:
  - Retain every published version (not just latest), with a way to view a past
    version's config and re-publish it as rollback.
  - Add a "test run" mode: executes a draft through the real interpreter against
    real governed tools (catches real bugs, not just structural ones), but tags
    the run as non-production — excluded from `AutomationSchedule` history/SLA
    tracking, and any artifact it produces gets a `testRun: true` flag so a test
    PDF/email-draft is never mistaken for a real deliverable.

### 6.3 What we're deliberately not chasing

- **North Mini Code** (their open-weight coding agent) — a separate product
  surface (agentic software engineering) with no overlap with this platform's
  `mcp-code` (which is deliberately read-only planning per `expansion.md` §8.5,
  not an autonomous coding agent). Not a gap; a different product.
- **Compass** (Cohere's proprietary retrieval engine, part of North's stack) —
  same caveat as Embed/Rerank in `STAGE2_PLAN.md` §10: proprietary, not
  self-hostable. Doesn't change that plan's open-weight recommendation.

---

## 7. Sequencing

1. Fix PDF extraction in `knowledge_store.py` (small, self-contained, unblocks §4
   regardless of what's prioritized next).
2. Ship the `paginate` capability (§3.2a) — smaller and lower-risk than the
   `loop` node, and fixes a live correctness bug (silent page-1-only truncation)
   in workflows that already exist today (win-back radar, AP/AR digests).
3. Ship the `loop`/for-each node (§3.2b) — depends on nothing else here, but
   benefits from the per-run budget question (§5) being answered first.
4. Finish the persisted field catalog (§3.3) — independent, can run in parallel
   with 2-3.
5. Per-workflow/automation **time** rollup (§6.2) — cheapest item on this whole
   list, pure aggregation over data already captured; do this first among the
   North-lessons items.
6. Workflow versioning/rollback + test-run mode (§6.2) — independent of 2-4;
   worth doing before more workflow templates accumulate without either. Reuse
   `artifact_store.py`'s version pattern rather than designing one.
7. Token capture + rollup (§6.2) — the one genuinely new instrumentation item
   here; lower priority than 5-6 since it's real new work, not aggregation.
8. Add the new miniERP tools (§2) — independent, prioritize inventory/stock and
   cross-customer shipment exceptions first (highest plausible usage).
9. Personal KB build-out (`digest_persoanl_kb.md`, full implementation) — once
   its one prerequisite (step 1) is done; can run in parallel with the rest.
10. Only after the above: start `STAGE2_PLAN.md`'s shared backend scaffold and
    the new backends it now covers (`mcp-hubspot`, `mcp-websearch`, `mcp-clickup`,
    then `mcp-acumatica`, `mcp-code`'s scope decision, and `mcp-outlook` last).
