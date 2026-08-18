# Improve "My Workflow": a Generic Filter Node + Field Catalog + Build-Time Copilot

**Status:** Partially built — verified against the code 2026-08-17. Phase 2 (the
`filter` node: `GraphNode.kind == "filter"`, `_execute_filter_node` in
`workflow_graph_interpreter.py`) and most of Phase 3 (the copilot — see
`workflow_scratchpad.py` + the copilot tool-loop in `orchestrator.py`/`app.py`, demoed
end-to-end in `howtocreatemyworkflowdemo.md`) are **live**. Only Phase 1 (§1's
persisted, sampled field catalog — `governance_core/tool_catalog.py`) is still
unbuilt; the copilot currently runs on `workflow_scratchpad.py`'s lighter,
in-memory, per-session substitute instead. See `finalize_stage_1.md` §3 for the
loop/pagination node gap this doc doesn't cover and the plan to finish Phase 1.
**Builds on:** `governance_core/workflow_graph_models.py`, `gateway/workflow_graph_interpreter.py`,
`gateway/frontend/src/components/workflow/*` (the "My Workflow" canvas), and the
array/object literal-JSON fix already shipped in `StepConfigFields.tsx`.
**Scope:** Let a non-technical user build a *correct*, *deterministic*, *reusable*
workflow like "flag customers that need a win-back" without writing code and
without the platform needing a new node kind for every business rule.

---

> **Read this before implementing anything below.** This is a best-effort design
> from when it was written, not a final spec — and docs in this repo go stale fast
> in both directions (this doc's own status line above is itself an example: it
> claimed nothing was built when most of it already was). Before starting
> implementation on any item here: (1) re-verify the relevant claim against the
> live code, don't trust the doc's description of current state; (2) ask the user
> clarifying questions about anything with a real tradeoff, a security/privacy
> implication, or an external dependency, rather than silently proceeding with
> whatever this doc currently says; (3) only then start writing code.

---

## 0. The problem this solves

Working through a real win-back-radar use case end-to-end (see chat history 2026-08-10/11)
surfaced two separate, compounding gaps in "My Workflow":

1. **No node can express row-level business logic.** The only 4 node kinds are
   `trigger`, `tool_call`, `approval_gate`, `llm_transform`. `llm_transform` takes
   exactly one bound *text* input and returns `{"text": "..."}` — it cannot take a
   table of customers in and return a ranked/filtered table out. So "flag rows
   where X" has nowhere to live in a graph today.
2. **A non-technical builder doesn't know the tool catalog well enough to author
   the rule anyway** — they don't know `get_customer_order_summary` returns
   `status`/`lastOrderDate`/`orderCount`, don't know `status` is coded `A`/`I`/`C`,
   and have no way to discover this short of a developer running ad hoc scripts
   (which is literally how this was discovered — see `_smoke/dump_raw_customer_data.py`).

Two ideas were floated and rejected in favor of a hybrid:

- **"Add a node kind per use case"** — rejected: unbounded proliferation, one node
  type per business rule shape.
- **"Let the LLM re-derive the logic at runtime, every run"** — rejected: LLMs are
  unreliable at exact arithmetic over many rows (date math, ratios), the result is
  non-reproducible run-to-run, and every run pays an extra LLM call per row or per
  batch. This is the same class of failure mode as the Qwen text-tool-call
  JSON-parsing bug already found and fixed in `gateway/orchestrator.py::_coerce` —
  asking a text-generation model to reliably produce/derive structured data at
  every single invocation is fragile by nature.

**The resolution:** separate *discovery + intent-translation* (things an LLM is
actually good at, done once, with a human confirming) from *execution* (things
that must be cheap, deterministic, and reproducible, done every run). Concretely:

- One generic, deterministic **filter/rule node kind** — not a node per use case.
- A **field catalog**, built by live-sampling tools (not the static manifest,
  which is incomplete for this purpose — see §1).
- A **build-time chat copilot** that interviews the user, greps the field catalog,
  confirms ambiguous vocabulary with a human, and proposes a concrete graph for
  review — never auto-publishes.
- Run-time knobs (like "how many days") stay adjustable via the interpreter's
  existing trigger-binding mechanism — no new plumbing needed for that part.

---

## 1. Field catalog

### 1.1 Why the manifest isn't enough

`governance_core/policy/manifest.py`'s `ToolPolicy.fields` dict is a **redaction
classification list**, not a return-schema. It only lists fields that carry
PII/SENSITIVE risk. Confirmed directly: `get_customer_order_summary`'s manifest
entry lists `grandTotal`/`averageOrderValue`/`total` — but not `status`,
`lastOrderDate`, `firstOrderDate`, or `orderCount`, which the tool actually
returns (`mcp-minierp/sqlagent/analytics.py::get_customer_order_summary`) and
which are exactly the fields a filter rule needs. They're absent because they
don't need redacting, not because they don't exist. Any design that only reads
the manifest will systematically miss the fields business rules care about most.

### 1.2 Design: sample, don't just introspect

A new backend job, `governance_core/tool_catalog.py` (new file), that for each
governed **read** tool:

1. Resolves 1–3 representative sample calls:
   - Non-account-scoped list tools (`get_customers_by_region`,
     `get_top_customers_by_spend`, …): call directly with a broad filter.
   - Account-scoped tools (`get_customer_order_summary`, `get_customer_profile`,
     …): first resolve a few real ids via a discovery tool (`find_customer`,
     `get_customers_by_region`), then call the scoped tool for each.
2. Unions the field names/types/example-values seen **across all samples** (not
   just one) — a single sample can miss a field that happens to be null/absent
   for that record.
3. Stores per tool: `{fields: [{name, type, exampleValues: [...], seenInSamples,
   totalSamples}], lastRefreshedAt, sampledBy}`.

Storage: alongside `governance_core/artifact_store.py`'s pattern — a JSON file
under `GOVERNANCE_STATE_DIR`, one record per canonical tool name.

Refresh: periodic (weekly default, `TOOL_CATALOG_REFRESH_SEC`) + an admin-only
`POST /admin/tool-catalog/refresh` for on-demand rebuilds. Sampling calls run
**through `_govern`** under a real, auditable service identity (a dedicated
`type="agent"` consumer, e.g. `catalog_builder`), never bypassing redaction — the
catalog must never expose a field's real values to a builder who wouldn't
otherwise be entitled to see them. If a field is redacted in the sample response
(masked per the caller's grant), the catalog records `exampleValues: ["<redacted>"]`
and a `redacted: true` flag rather than the mask token.

Exposed to the frontend via `GET /dashboard/tool-field-catalog?tool=...` (new
route in `gateway/backend/workflow_graphs.py` or a sibling file), consumed by
both the manual condition-builder UI (§2.3) and the copilot (§3).

### 1.3 The caveat this doesn't solve: syntax vs. semantics

Sampling tells you `status` takes values `A`/`I`/`C`. It does **not** tell you
`A` means "active." That's a semantic fact, not a structural one, and inferring
it wrong is worse than not inferring it — a workflow that silently treats the
wrong code as "active" produces confidently incorrect reports. The catalog
records raw values only; anything that looks enum-like (small cardinality,
non-numeric) is flagged `needsConfirmation: true`, and the copilot (§3) must
surface a direct question ("we see status codes A, I, C on real accounts — which
of these means active?") rather than assume.

---

## 2. Generic filter/rule node kind

### 2.1 Model

New `GraphNode.kind == "filter"` (extends the union in
`governance_core/workflow_graph_models.py:21`, currently
`"trigger" | "tool_call" | "approval_gate" | "llm_transform"`).

Config shape:

```jsonc
{
  "input": {"source": "node", "node_id": "n1", "path": "records"},  // array in
  "conditions": {
    "all": [                                   // "all" = AND, "any" = OR, nestable
      {"field": "daysSinceLastOrder", "op": "gt",
       "value": {"source": "trigger", "path": "win_back_days_threshold"}},
      {"field": "status", "op": "eq", "value": "A"}
    ]
  }
}
```

`op` ∈ `eq | ne | gt | gte | lt | lte | contains | in | not_in`. Each condition's
`value` may be a **literal** or a **binding** (trigger/node), reusing the exact
`WorkflowBinding` shape `_resolve_binding` already understands
(`gateway/workflow_graph_interpreter.py:77`) — this is how "how many days" stays
a run-time-adjustable knob (a trigger input) instead of a rebuilt literal every
time someone wants a different threshold (see §4).

### 2.2 Interpreter executor

New `_execute_filter_node` in `workflow_graph_interpreter.py`, sibling to
`_execute_llm_transform_node`. Pure Python: resolve `input` to a list of dicts,
evaluate the condition tree per row (a ~30-line recursive boolean evaluator —
no library needed, no LLM call, no network call). Output:

```json
{"matched": [...], "unmatched": [...], "matchedCount": N, "totalCount": M}
```

Both `matched` and `unmatched` are kept (not just the filtered set) so a
downstream node can report on either — e.g. bind `tables` on the win-back PDF to
`matched`, or build a QA/debug view over `unmatched` during authoring.

Type coercion (string dates → comparable timestamps, numeric strings → numbers)
happens once, deterministically, in the evaluator — never re-derived per run by
a model. Same input always produces the same output; this is the property that
matters most for something scheduled to run unattended every week.

### 2.3 UI (manual authoring, independent of the copilot)

`StepConfigFields.tsx` gets a `filter` branch alongside the existing
`tool_call`/`approval_gate`/`llm_transform` ones: an input-source picker (reuse
`BindingRow`'s node-source dropdown), then a condition-row list — each row a
`field` dropdown (populated from the field catalog for whatever node feeds
`input`, falling back to free text if the catalog has no entry yet), an `op`
dropdown, and a value control that reuses the *already-shipped*
`JsonValueControl`/`LiteralValueControl` split (a plain value gets the typed
literal widget; anything bound gets the existing trigger/node source picker).
This is usable **without the copilot** — the copilot (§3) is what makes it
usable by someone who doesn't already know the field names.

---

## 3. Build-time chat copilot

### 3.1 Why build-time, not run-time

This is the crux of the whole design: the copilot's output is a **graph**, reviewed
once by a human, then executed deterministically forever after. It is never
re-invoked per run. This sidesteps the reliability risk that killed the
"LLM decides at runtime" idea entirely — a bad LLM turn during *authoring* gets
caught by the human reviewing the proposed graph before publishing; a bad LLM
turn during *every scheduled execution* would fail silently, differently, every
time.

### 3.2 Surface

A chat panel in `MyWorkflowsPage.tsx` (new panel, adapting the existing chat
scaffolding — `useChat.ts`/`chatStream.ts` already built for the main assistant
chat, `HomePage.tsx`). Opens from "New workflow" or from an existing draft graph
("ask the copilot to help finish this").

### 3.3 Backend: a second orchestrator mode

**Not** `orchestrator.run_chat` (that loop calls *governed business tools* on
behalf of the end user for Q&A). This needs its own loop —
`orchestrator.run_workflow_copilot` (new) — with a distinct, narrower tool set:

- `list_tool_catalog(category?)` — the existing `/dashboard/workflow-graph-catalog`
  data, tool-name + description + schema.
- `get_field_catalog(tool)` — reads §1's catalog; if stale/missing for that tool,
  triggers an on-demand sample (bounded — one or two live calls, not a crawl).
- `propose_graph(nodes, edges, notes)` — the one mutating tool. Writes a **draft**
  graph version (`workflow_graph_store`'s existing draft/publish split already
  gives us "exists but not live" for free) and returns its id to the frontend,
  which opens/refreshes the canvas showing exactly what was proposed, editable,
  **not published**. Calling this again on the same conversation updates the same
  draft rather than creating a new one each turn.

System prompt must encode every structural rule this session surfaced by hand,
or the copilot will confidently emit an invalid graph:
- A `send`-risk tool_call (e.g. `send_email_draft`) is rejected at publish time
  unless reachable only through an `approval_gate` — `workflow_graph_store.py`'s
  `validate_graph`, confirmed in `_smoke/test_workflow_graphs.py`.
- `llm_transform.input_text` may only bind to a `tool_call`/`llm_transform`
  node's output, never `approval_gate`.
- Array/object tool args (`sections`, `tables`, `to`, …) must be real JSON, not a
  string — already true for the human-facing canvas after the `StepConfigFields.tsx`
  fix; the copilot must emit the same shape.
- A `filter` node's `conditions` values should default to a **trigger binding**
  for anything that reads as a tunable threshold ("how many days"), and a
  **literal** for anything that's a settled structural fact ("status == active"),
  matching §4.

### 3.4 Conversation shape (worked example)

> **User:** "I want a win-back workflow."
> **Copilot:** "What should count as needing a win-back?"
> **User:** "If their last order was very long ago and they're not closed."
> **Copilot** calls `get_field_catalog("minierp_orders_get_customer_order_summary")`
> and `get_field_catalog("minierp_accounts_get_customers_by_region")`, sees
> `status` takes values `A`/`I`/`C` (flagged `needsConfirmation`), and asks:
> "I see accounts have a status code — A, I, or C. Which one means active?"
> **User:** "A."
> **Copilot:** "And how long since their last order should count as overdue — a
> fixed number of days, or relative to how often *that* customer normally orders?"
> **User:** "Relative to their normal cadence." *(this is the fix from earlier in
> this session — the copilot should offer this as an explicit option, not just
> default to a flat threshold, precisely because flat-threshold ranking was
> proven wrong against real data.)*
> **Copilot** proposes a graph: fetch customers by region → per customer, fetch
> order summary → `filter` node computing `daysSinceLastOrder / avgGapBetweenOrders
> > threshold AND status == "A"` (threshold bound to a trigger input, default
> 1.5) → `create_pdf_packet` on the matched rows → optional `create_email_draft`
> per flagged customer, gated. Opens in the canvas as an editable draft.
> **User** reviews, tweaks the default threshold, hits Publish.

### 3.5 Human-in-the-loop, always

`propose_graph` never calls `workflow-graphs/{id}/publish`. Publishing remains an
explicit human action in the existing canvas UI, same as a hand-built graph.
This preserves the property every other part of this platform already has:
nothing customer-facing or scheduled goes live without a human looking at it once.

---

## 4. Run-time-adjustable parameters (no new plumbing)

The interpreter already resolves `input_bindings`/condition values from
`{"source": "trigger", "path": ...}` against `run.inputs` — that's how the 5
hardcoded templates take a `customer_id` at run time today
(`workflow_graph_interpreter.py:77`, `gateway/backend/workflow_api.py`'s
`_workflow_input_requirements`). A filter node's condition value is resolved
through the exact same code path (§2.1). So "let the business user pick 45 days
this week and 30 days next week" requires **no interpreter change** — only a
convention the copilot follows: default tunable-looking values to a trigger
binding with a labelled input (`"Win-back threshold (days)"`), not a baked
literal, unless the user explicitly wants it fixed.

---

## 5. Governance & safety notes

- The `filter` node itself carries no risk classification — it reads/writes
  nothing external. Risk stays exactly where it already lives: on the
  `tool_call` nodes that feed it and consume its output. No manifest change
  needed for the node kind itself.
- Field-catalog sampling (§1.2) is real, audited, governed traffic under its own
  service identity — never a way to peek at data the catalog-builder isn't
  entitled to. Redacted fields stay redacted in the catalog.
- Ambiguous/coded values are surfaced for human confirmation (§1.3) rather than
  silently inferred — a wrong silent inference is strictly worse than asking.

---

## 6. Phased build plan

| Phase | What | Depends on |
|---|---|---|
| 1 | Field catalog builder + admin refresh endpoint + storage | Nothing new — read-only, uses existing `_govern` |
| 2 | `filter` node kind: model, interpreter executor, manual JSON-based canvas UI | Phase 1 (for the UI's field dropdown; interpreter/model work can start in parallel) |
| 3 | Copilot chat surface: frontend panel + `run_workflow_copilot` orchestrator mode + `propose_graph` tool | Phases 1 & 2 (copilot needs both the catalog and the node kind to target) |
| 4 (stretch) | Richer condition-builder UI (autocomplete from catalog, inline sample-value hints) so a human can hand-edit without touching raw JSON | Phase 2 |

## 7. Open questions

- **Which LLM backend for the copilot?** Its output (a full nested graph — several
  nodes, edges, configs) is a bigger, higher-stakes version of the same
  array/object JSON-reliability problem already found in the Qwen text-tool-call
  path (`orchestrator.py::_coerce`). Recommend gating this feature to a
  native-tool-calling backend (Azure OpenAI / Anthropic) rather than the local
  Qwen/sglang text-parsing path, at least initially.
- **Catalog staleness policy** — weekly refresh is a starting guess; revisit once
  real usage shows how often ERP schemas actually change vs. how often builders
  hit a missing/stale field.
- **`propose_graph` merge semantics** — replace vs. patch an existing in-progress
  draft when the conversation continues across multiple turns; needs a concrete
  answer before Phase 3, not left implicit.
- **Sampling cost/rate** — `db-api.frontierdental.com` has already shown
  aggressive edge protection (Cloudflare) for unrecognized callers; the catalog
  job must run from an already-allowlisted caller (the deployed App Service, per
  `DEPLOY.md`'s IP-allowlist warning) and should stay light (1–3 calls per tool,
  on a slow refresh cadence), not a crawl.
