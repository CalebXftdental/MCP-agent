# Stage 2 — Backend Scaffold, Write-Capable ERP, and Tool-Scale Strategy

**Status:** Proposed (supersedes the "current baseline" and "LLM/MCP" sections of
`expansion.md`, which was written 2026-07-22 before artifacts/approvals/automation/
workflow-graph/knowledge/calendar-send/email-send/code-plans/document-reviews all
shipped — see `STAGE1_README.md` for the real current tool catalogue).
**Builds on:** `design_plan.md` (gateway architecture), `design_plan_v2.md` (control
plane / access model), `STAGE1_README.md` (what's actually built and running).
**Scope:** How we add more systems of record (HubSpot, Acumatica writes, and beyond)
without the tool surface outrunning either our governance discipline or the LLM's
ability to pick the right tool.

---

## 0. Decisions locked (quick reference)

| # | Decision | Choice |
|---|---|---|
| miniERP write access | extend `db-api` with generic mutations vs. call Acumatica's own API | **Acumatica's native contract-based REST API**, via a dedicated scoped integration user. `db-api.frontierdental.com` is a one-directional read mirror (`findWithOffsetPagination`/`findWithCursorPagination` over "any table") — there is no write path through it, and a generic `updateRecord(table, fields)` resolver would bypass Acumatica's own business logic (GL posting, tax, inventory, workflow). Writes must go through the application layer that owns that logic. |
| mcp-acumatica scope | one big write backend vs. narrow pilot | **One narrow write tool first** (e.g. add an order note, or update a tracking number), fully wired through preview → approval → audit, before a second write tool is added. |
| HubSpot integration | adopt HubSpot's official MCP server vs. build our own | **Build `mcp-hubspot`** in the same shape as `mcp-minierp`/`mcp-office`. HubSpot's official MCP has no concept of our consumer identity, `customer_id` scope-binding, or field classification — wrapping it to add governance means building our own backend anyway, so skip the extra layer. Reuse the working token+REST pattern already proven in `governance_service/hubspot/hubspot_query.py`. |
| HubSpot vs miniERP overlap | redundant? | **No — complementary.** miniERP/Acumatica = transactional ERP (orders/invoices/GL/shipments). HubSpot = pipeline/marketing (deal stage, lead source, engagement, activity notes). Only shared key is the customer/company identity (`baccount_acctcd`), already joined today. |
| New-backend pattern | bespoke per backend vs. shared scaffold | **Extract a shared adapter scaffold now**, before backend #4/#5/#6 (hubspot/acumatica/outlook/clickup) each reinvent token-cache/retry/error-shape, the same way `minierp_core` was extracted out of four duplicated `mcp-minierp-*` copies. |
| Read/write separation | same namespace vs. split | **Split at the tool-namespace (and likely backend) level** — `minierp_*` (read) vs `minierp_write_*` — so risk tier and approval requirements stay visually obvious as the surface grows, instead of retrofitting the split after writes exist. |
| Tool-count scaling | keep static exclude-lists vs. dynamic retrieval | **Move to embed+rerank tool retrieval** (top-K relevant tools per turn) once more backends land. `orchestrator.WORKFLOW_ONLY_TOOLS` is a hand-maintained static list — fine at ~50 tools, won't hold at 100+. |
| Embed/rerank model choice | Cohere Embed/Rerank vs open-weight | **Open-weight, self-hosted** — Cohere's Embed/Rerank are API-only, not open-weight (confirmed; see §6). Use **Qwen3-Embedding + Qwen3-Reranker** (Apache-2.0) or **BGE-M3 + bge-reranker-v2** for both tool-retrieval and `mcp-knowledge` search. |
| Local LLM + RL | fine-tune with agentic RL now vs. later | **Not yet.** Ladder: tool retrieval → structured/JSON-schema tool calls → SFT on our own audit-log traces → RL only much later, only for a specific hard skill (e.g. disambiguating among many similar write tools), not as a general chatbot upgrade. See §7. |
| Model routing by risk | one model everywhere vs. split by lane | **Keep the split `orchestrator.py` already supports** (local OpenAI-compatible / Azure OpenAI / Anthropic via env vars) — reserve the hosted/stronger model for the workflow-copilot lane (especially once ERP writes exist), local model for the lower-stakes home-chatbot lane. |

---

## 1. Reality check: what's already built (not what `expansion.md` says)

`expansion.md` (2026-07-22) describes a "Phase 1: Artifact Foundation" that hasn't
started yet. It already had. As of this writing, `governance_core/` already has, live:

- `artifact_models.py` / `artifact_store.py` / `artifact_share_models.py` / `artifact_share_store.py`
- `approval_models.py` / `approval_store.py`
- `automation_models.py` / `automation_store.py`
- `workflow_graph_models.py` / `workflow_graph_store.py` (+ `gateway/workflow_graph_interpreter.py`)
- `email_send_models.py` / `email_send_store.py`, `calendar_send_models.py` / `calendar_send_store.py`
- `knowledge_models.py` / `knowledge_store.py`
- `code_plan_models.py` / `code_plan_store.py`, `document_review_models.py` / `document_review_store.py`
- `template_models.py` / `template_store.py`, `agent_models.py` / `agent_store.py`

And `mcp-minierp` is already the consolidated single process (orders/accounts/finance/
shipments/analytics) that `expansion.md` §8 was still proposing as future split backends.
**Do not plan against `expansion.md`'s "Current Platform Baseline" section — it's wrong.**
Its product-vision material (use cases, UX page layout, categories/risk taxonomy ideas)
is still reasonable reading; its "what exists today" and "what to build" sections are not.

---

## 2. The actual Stage 2 problem

Two things are happening at once and they interact:

1. **More systems of record are coming**: HubSpot (CRM), Acumatica-native writes, and
   likely Outlook and ClickUp after that. Each is a new MCP backend behind the gateway.
2. **Tool count is growing faster than the LLM's ability to reliably pick the right
   tool.** At ~50 tools a model can usually cope with a full spec dump. At 100+ across
   6+ backends, that stops being true — and the only mitigation that exists today
   (`WORKFLOW_ONLY_TOOLS`, a hand-maintained static exclude-list per chat surface) does
   not scale to "many more backends."

These have to be solved together: adding backends without a tool-selection strategy just
makes the second problem worse.

---

## 3. Shared backend scaffold (do this before backend #4)

Right now every backend duplicates its own version of the same plumbing:
`minierp_core/graphql_client.py` has its own per-profile token cache and 401-retry;
`governance_service/hubspot/hubspot_query.py` has its own env-var lookup and blob-fallback
logic. Before adding `mcp-hubspot`, `mcp-acumatica`, `mcp-outlook`, `mcp-clickup`, extract
the common shape into one shared package (same idea as `minierp_core` itself, which was
pulled out of four duplicated `mcp-minierp-*` copies):

- Auth/token caching with proactive refresh + 401-retry-once (generalize what
  `minierp_core` already does).
- A structured, audit-friendly error envelope every backend returns on failure
  (today each backend invents its own `status: "..._error"` shape).
- A manifest-registration helper that **refuses to register a tool without** a risk
  tier (`read_low` / `read_sensitive` / `export` / `send` / `write` / `code_exec`) and
  field classifications — governance-by-construction instead of governance-by-remembering.
- A per-backend health-check convention (already informally present; formalize it).

This is the highest-leverage single piece of work in this plan: it's the difference
between "each new backend is bespoke again" and "a new backend is mostly config."

---

## 4. `mcp-acumatica` (write) — from scratch, most conservative rollout

Confirmed: `db-api.frontierdental.com` is a **read-only mirror/wrapper**
(`minierp_core/graphql_client.py` → `findWithOffsetPagination`/`findWithCursorPagination`,
no mutation field anywhere in `DB_API_documentation_v1.1.md`). A mirror is one-directional
by construction — there is no version of "write to the mirror" that makes sense. Writes
must go through **Acumatica's own native contract-based REST API**
(`/entity/Default/<version>/<Entity>`), which is a different transport, different auth,
and — critically — actually runs Acumatica's business logic (GL, tax, inventory,
workflow) instead of bypassing it.

Rollout, in order:

1. Confirm with the Acumatica instance admin that the contract-based REST API is
   enabled and which entities/version are exposed. This is a separate question from
   anything `db-api` does.
2. Provision a dedicated Acumatica integration user scoped to **one** entity/screen for
   the pilot tool — not a broad integration account.
3. Build a new client module (e.g. `acumatica_write_client.py`) as its own thing,
   using the shared scaffold from §3, not bolted onto `minierp_core`.
4. Pilot with **one** narrow, low-blast-radius write tool (candidates: add a note to a
   sales order; update a shipment tracking number). Wire it through the same
   preview/diff → `approval_store` → audit → commit pattern already built for
   `email_send_external`.
5. Before that pilot ships, it needs:
   - **Idempotency keys** — a retried request must not double-write.
   - **Optimistic concurrency** — check Acumatica's row version/etag before committing,
     so a concurrent edit made in the Acumatica UI isn't silently clobbered.
   - **A staging/sandbox Acumatica tenant test**, if one exists, before production.
   - **Separate rate limiting/circuit-breaking from read tools** — a runaway read loop
     is a cost problem; a runaway write loop is a data-integrity incident.
6. Land it under a visibly separate namespace (`minierp_write_*` or its own
   `mcp-minierp-write`/`mcp-acumatica` backend) per the read/write split decision in §0.
7. Do **not** generalize to a second write tool until the first has run in production
   long enough to trust the approval/audit loop end-to-end.

**Open question for the coworker who owns `db-api`/Acumatica admin:** confirm contract-API
availability and get the pilot integration user provisioned — this blocks step 1-2 above.

---

## 5. `mcp-hubspot`

Not redundant with miniERP — different data entirely (§0). Build it the same way
`mcp-minierp` was built: narrow, purpose-specific tools over the existing HubSpot REST
API, not a raw "search any object" tool. Reuse the access-token pattern already proven
in `governance_service/hubspot/hubspot_query.py` (`HUBSPOT_ACCESS_TOKEN`, `requests`
against `api.hubapi.com`).

First tools (read-only, in priority order):
- `find_company` (by name/domain/`baccount_acctcd` — extends the existing
  `customer_exists_in_hubspot` / company-search logic into a governed tool)
- `get_deal_summary` (stage, amount, close date, owner — for a resolved company)
- `get_recent_activity` (notes/calls/emails logged against a contact or company)

Hold HubSpot writes (`create_note`, `update_deal_stage`) until after the read tools are
live and the write governance pattern has already been proven once on `mcp-acumatica`'s
pilot tool (§4) or `email_send_external`.

Use `mcp-hubspot` as the backend that **proves the shared scaffold from §3** — it's lower
risk than Acumatica writes, so it's the right place to find scaffold problems first.

---

## 6. Tool-selection at scale (why not just keep adding tools to every prompt)

Today's only scoping mechanism, `orchestrator.WORKFLOW_ONLY_TOOLS`, is a static,
hand-maintained exclude-list checked in one place (`gateway/backend/chat.py`). It works
at ~50 tools across 6 backends. It will not work once HubSpot/Acumatica-write/Outlook/
ClickUp are all live and each surface (home-chat vs. workflow-copilot vs. a specific
workflow template) needs a different relevant subset.

**Plan: tool retrieval.** Embed every tool's name + description once (cheap, done at
manifest-load time, re-embed on manifest change). At request time, embed the user's
message/intent, retrieve the top-K candidate tools by cosine similarity, then rerank
those candidates for precision before handing the final tool spec list to the LLM. This
is the same "retrieve for recall, rerank for precision" shape already used for
`mcp-knowledge` document search — one pipeline, two use sites.

**Correction on the Cohere reference:** Cohere's Embed and Rerank models (`embed-v4`,
`rerank-v3.5`) are **API-only, not open-weight** — they cannot be self-hosted. For a
self-hosted pipeline, the actual open alternatives are strong and current:
- **Qwen3-Embedding + Qwen3-Reranker** (Apache-2.0, 0.6B–8B sizes, strong on retrieval
  and rerank benchmarks) — preferred if multilingual/flexible sizing matters.
- **BGE-M3 + bge-reranker-v2** — the other solid open pair, widely deployed.

Either pair backs both tool-retrieval and `mcp-knowledge` search; no reason to run two
different embedding stacks.

**External validation:** Cohere's own enterprise agent product, North, ships native
connectors only for Gmail/Outlook/Slack/Salesforce/Linear — no ERP. For "industry-specific
or in-house applications" (exactly our ERP case), North's own answer is "bring your own
MCP server." Even a company selling a general enterprise-agent platform assumes ERP
integration is BYO-MCP — which is exactly this repo's architecture.

---

## 7. Local LLM strategy for home-chatbot + workflow-copilot

Do **not** reach for agentic RL post-training first. In order of actual leverage:

1. **Tool retrieval (§6)** — shrinking the choice set from ~100 tools to ~15 relevant
   ones is the single biggest lever on tool-selection accuracy, and it's not a model
   change at all.
2. **Structured/JSON-schema-enforced tool-call output** — `orchestrator.py` already
   partially does this (the Qwen text-format tool-call parser); tightening this further
   is still cheaper than any training run.
3. **SFT on our own audit-log traces** — once there are a few thousand real successful
   tool-call traces in the audit log, supervised fine-tuning on them is far cheaper than
   RL and captures most of the same benefit (grounding the model in *our* tools, *our*
   naming, *our* argument shapes — not generic function-calling).
4. **Agentic RL — only much later, only for a specific hard skill.** Reserve this for a
   narrow, well-defined failure mode that SFT can't fix (e.g. reliably disambiguating
   among several similar write tools before a write happens), with a real reward signal
   (valid tool + valid args + task success) and a real eval set. Not a general chatbot
   quality upgrade, and not before ERP writes exist — the cost of a bad RL-induced
   regression is much higher once the model can call `mcp-acumatica` write tools.

Keep the existing multi-provider routing in `orchestrator.py` (local OpenAI-compatible
endpoint / Azure OpenAI / Anthropic, selected by env var) rather than collapsing to one
model — route the workflow-copilot lane (especially anything touching write tools) to
the strongest available model, and reserve the local model for the home-chatbot lane
where mistakes are cheap.

---

## 8. Rollout sequencing

1. Shared backend scaffold (§3) — nothing else should be built on the old ad hoc pattern.
2. `mcp-hubspot`, read-only (§5) — proves the scaffold on a lower-risk backend.
3. Tool retrieval / embed+rerank layer (§6) — needed once backend #4+ lands, not before.
4. `mcp-acumatica` pilot write tool (§4) — highest risk, most conservative, goes last,
   gated on the scaffold and the approval pattern both being proven elsewhere first.
5. Outlook / ClickUp / further backends — should be near-zero-novelty once 1-3 exist;
   each is "config on the scaffold," not a new architecture decision.

## 9. Open questions blocking work

- Acumatica contract-based REST API availability + version, and provisioning a scoped
  pilot integration user — **owned by whoever admins the Acumatica instance**, not us.
- Which entity to pick for the Acumatica write pilot (order note vs. tracking number
  vs. something else) — needs a product decision on what's actually useful *and* low-risk.
- Whether a staging/sandbox Acumatica tenant exists to test against before production.
