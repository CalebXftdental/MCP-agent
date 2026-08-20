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
| miniERP write access | extend `db-api` with generic mutations vs. call Acumatica's own API | **Acumatica's native contract-based REST API**, via a dedicated scoped integration user. `db-api.frontierdental.com` is a one-directional read mirror (`findWithOffsetPagination`/`findWithCursorPagination` over "any table") — there is no write path through it, and a generic `updateRecord(table, fields)` resolver would bypass Acumatica's own business logic (GL posting, tax, inventory, workflow). Writes must go through the application layer that owns that logic. |
| mcp-acumatica scope | one big write backend vs. narrow pilot | **One narrow write tool first** (e.g. add an order note, or update a tracking number), fully wired through preview → approval → audit, before a second write tool is added. |
| HubSpot integration | adopt HubSpot's official MCP server vs. build our own | **Build `mcp-hubspot`** in the same shape as `mcp-minierp`/`mcp-office`. HubSpot's official MCP has no concept of our consumer identity, `customer_id` scope-binding, or field classification — wrapping it to add governance means building our own backend anyway, so skip the extra layer. Reuse the working token+REST pattern already proven in `governance_service/hubspot/hubspot_query.py`. |
| HubSpot vs miniERP overlap | redundant? | **No — complementary.** miniERP/Acumatica = transactional ERP (orders/invoices/GL/shipments). HubSpot = pipeline/marketing (deal stage, lead source, engagement, activity notes). Only shared key is the customer/company identity (`baccount_acctcd`), already joined today. |
| New-backend pattern | bespoke per backend vs. shared scaffold | **Extract a shared adapter scaffold now**, before backend #4/#5/#6 (hubspot/acumatica/outlook/clickup) each reinvent token-cache/retry/error-shape, the same way `minierp_core` was extracted out of four duplicated `mcp-minierp-*` copies. |
| Read/write separation | same namespace vs. split | **Split at the tool-namespace (and likely backend) level** — `minierp_*` (read) vs `minierp_write_*` — so risk tier and approval requirements stay visually obvious as the surface grows, instead of retrofitting the split after writes exist. |
| Tool-count scaling | keep static exclude-lists vs. dynamic retrieval | **Move to embed+rerank tool retrieval** (top-K relevant tools per turn) once more backends land. `orchestrator.WORKFLOW_ONLY_TOOLS` is a hand-maintained static list — fine at ~50 tools, won't hold at 100+. |
| Embed/rerank model choice | Cohere Embed/Rerank vs open-weight | **Open-weight, self-hosted** — Cohere's Embed/Rerank are API-only, not open-weight (confirmed; see §6). Use **Qwen3-Embedding + Qwen3-Reranker** (Apache-2.0) or **BGE-M3 + bge-reranker-v2** for both tool-retrieval and `mcp-knowledge` search. |
| Math/aggregation tool treatment | gate behind a manifest/backend tag vs. keep ungated, cross-cutting | **Keep ungated** (no manifest entry) — `compute_stats`/`calculate`/`percent_change`/`group_stats` (`gateway/app.py`, added 2026-08-18) operate only on numbers the caller already has from an earlier governed call, so there's nothing to redact/audit; same treatment as `submit_workflow_request`/`propose_graph`. They're cross-cutting, used across every domain, so any future domain-narrowing cascade (§10.1) must exempt them rather than bucket them under one backend. |
| Tool-count-scaling trigger point | "~50 tools, model copes" (§2's original estimate) vs. re-verified | **Revise down.** External benchmark data (BFCL, reviewed 2026-08-18) shows accuracy degrading meaningfully starting around ~20 tools in a candidate set, not ~50 — see §10.1. Doesn't change the §10 embed+rerank plan itself, just moves up when it's actually worth building. |
| Local LLM + RL | fine-tune with agentic RL now vs. later | **Not yet.** Ladder: tool retrieval → structured/JSON-schema tool calls → SFT on our own audit-log traces → RL only much later, only for a specific hard skill (e.g. disambiguating among many similar write tools), not as a general chatbot upgrade. See §7. |
| Model routing by risk | one model everywhere vs. split by lane | **Keep the split `orchestrator.py` already supports** (local OpenAI-compatible / Azure OpenAI / Anthropic via env vars) — reserve the hosted/stronger model for the workflow-copilot lane (especially once ERP writes exist), local model for the lower-stakes home-chatbot lane. **Refined 2026-08-19 (§11.2): the risk split that actually matters day-to-day is bulk/multi-turn vs. single-record, not just "workflow-copilot vs. home-chat"** — real benchmark showed the local model is competitive on single-record lookups but measurably degrades or hard-fails on bulk/paginated queries, regardless of which chat surface asks. |
| Web search | build it vs. skip | **Build `mcp-websearch`** (§6) — real, currently-missing capability (tool catalog is almost entirely ERP-shaped today). Treat returned page content as untrusted/adversarial by default, same defense class as uploaded documents. |
| ClickUp integration | adopt ClickUp's official MCP server vs. build our own | **Build `mcp-clickup`** (§7) — same reasoning as HubSpot: the official server is per-user-OAuth, doesn't fit our per-consumer service-identity model, so wrap the REST API narrowly ourselves. |
| `mcp-code`/opencode scope | keep read-only planning vs. embed opencode's real run mode | **Keep read-only planning as the default; embedding opencode's server+SDK for real execution is a deliberate, separate scope expansion** (§8), not a side effect of "we found an embeddable SDK." Real run mode means real read/write/bash on a directory — gate it at least as hard as `mcp-acumatica` writes. |
| Real inbox/calendar reads (`mcp-outlook`) | bundle into the ClickUp/websearch batch vs. treat separately | **Treat separately, sequence later** (§9). Today's `calendar`/`email` tools are draft-only; reading someone's actual mailbox/calendar is a materially bigger privacy step than anything else in this batch, and deserves its own explicit decision, not a quiet scope-creep alongside lower-stakes additions. |
| `mcp-outlook` auth model | per-user OAuth (delegated) vs. one IT-provisioned credential (application permissions) | **Application permissions, IT-provisioned** (§9.1) — consistent with every other backend's "one governed service credential" shape; no user ever hand-types a Graph API key under either model, so this is a backend-architecture choice, not a UX one. Blast radius is bounded via an Exchange Application Access Policy (§9.2), not by asking users to individually consent. |
| `mcp-outlook` mailbox identity | assume `consumer_id`/login maps to a mailbox vs. add an explicit field | **Add `mailbox_upn` to `ConsumerRecord`, IT-maintained** (§9.3) — confirmed no such mapping exists today; login is custom username+password, not Microsoft SSO, so there's no implicit link to resolve. Gateway derives the target mailbox from this field only, never a caller-supplied argument — same rule as `digest_persoanl_kb.md`'s owner-derivation decision. |

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

## 6. `mcp-websearch`

The tool catalog today is almost entirely ERP-shaped (30 miniERP tools vs. 6 office,
4 calendar, 2 email). Web search is a real, currently-missing capability, not a
nice-to-have — closing this gap matters as much as the new backends above.

- **Provider**: Tavily or Brave Search API — built for/friendly to LLM agents,
  return cleaned content rather than raw HTML to parse. Self-hosted SearXNG is the
  alternative if minimizing third-party query exposure matters more than result
  quality (it still ultimately queries external engines per request, just
  aggregated rather than logged by one vendor).
- **The governance angle that's unique to this tool, and the one to get right
  before anything else**: search results are adversarial-by-default content —
  unlike ERP data, web pages can be deliberately crafted to inject instructions.
  Apply the same defense already specified for uploaded documents (§13's prompt
  injection notes, `expansion.md`'s): fence/quote returned content, instruct the
  model to never treat page content as instructions, log the source URLs a
  response actually used. This is a bigger risk class than any read tool built so
  far — worth its own manifest note, not just another `read_low` tool.
- **Rate/cost limiting** distinct from internal reads — this is metered, external,
  per-query cost, not a free internal GraphQL call.
- First tools: `web_search(query, limit)`, `fetch_page_summary(url)` — narrow,
  matching the existing per-backend philosophy; not a general-purpose browsing tool.

---

## 7. `mcp-clickup`

Same reasoning as HubSpot (§5): an official ClickUp MCP server exists, but it's
per-user OAuth (built for a human connecting their own editor), not a fit for our
per-consumer service-identity model. Build a thin governed wrapper over ClickUp's
REST API instead, in the same shape as every other backend here.

First tools (read-first, in priority order):
- `list_my_tasks` (tasks assigned to the resolved consumer)
- `get_task_details`
- `create_task` (`write` risk tier — needs a manifest entry and, per §3's rule,
  can't register without one)
- `update_task_status`, `add_comment` — hold until `create_task` has run in
  production long enough to trust the write path, same discipline as
  `mcp-acumatica`'s single-pilot-tool rollout (§4.7).

---

## 8. `mcp-code` scope: embedding opencode for real execution

`opencode` (already the tool `mcp-code` wraps in read-only planning mode —
`opencode_plan_change`, `opencode_review_repo`, `opencode_generate_template`) has a
genuine embeddable server mode: `opencode serve` exposes an OpenAPI 3.1 HTTP API,
with a TS/JS SDK meant for embedding in a custom app's own UI rather than a
terminal. Technically, embedding it into this workspace's "Agent"/"Code" panels is
straightforward — that is **not** the decision that matters here.

**The actual decision: do we want to give it real read/write/bash on a directory,**
not just plan-and-propose. That's a materially bigger risk surface (arbitrary code
execution) than what's governed today, and it's the same class of problem
`mcp-acumatica` writes are (§4) — an agent that can mutate real state, not just
describe a change for a human to apply.

If this is wanted:
- Admin/developer category only (already true for the planning tools — keep it).
- Every write/bash action stays gated behind approval per action, not a blanket
  "developer mode" toggle — matches `expansion.md` §8.5's original rule
  ("never allow arbitrary shell execution for non-admin workflows").
- Run it against an isolated worktree per session, not the live repo directly.
- Land as an explicit new risk tier/tool set alongside the existing planning
  tools, not a silent upgrade of what `opencode_plan_change` already does.

(**Pi**, another open-source coding agent, is comparably embeddable — SDK mode,
RPC mode for process integration. No reason to switch off `opencode` given the
existing integration precedent; noted here only in case a specific gap in
`opencode` ever makes it worth a second look.)

---

## 9. `mcp-outlook`: real inbox/calendar reads (Microsoft Graph)

Today's `calendar`/`email` tools are **draft-only** — `list_upcoming_meetings`
lists this consumer's own drafted invites, not a real calendar; there is no
inbox-read tool at all. Reading someone's actual mailbox/calendar is a materially
bigger privacy step than drafting on their behalf, and deserves its own explicit
decision rather than riding along with the ClickUp/websearch batch. Confirmed
target: Microsoft Graph (Outlook/365), not Google.

### 9.1 Auth model: application permissions (IT-provisioned), not per-user key entry

**A user never has a "Graph API key" to type in, under either auth model** —
that's not how Graph auth works, full stop. The real choice is between two
legitimate patterns:

| | Delegated (per-user OAuth) | Application permissions (app-only) |
|---|---|---|
| Setup | Each user clicks "Connect mailbox," redirected to Microsoft login, consents once; app exchanges the code for tokens | IT registers **one** Entra ID app with application permissions (`Mail.Read`, `Mail.Send`, `Calendars.Read`/`ReadWrite`, application — not delegated — scope), admin-consents once, tenant-wide |
| Per-user friction | One-time consent click per user | None |
| New secret category | Per-user refresh tokens — encrypted storage, rotation, revoke-on-offboarding — **nothing like this exists in the platform today** | None — one client credential, same shape as every other backend's service credential (`minierp_core`, HubSpot token) |
| Blast radius if credential leaks | One person's mailbox | Every mailbox the app is scoped to — **must be bounded separately** (§9.2) |
| Fits existing architecture? | No — new identity model, nothing else here does per-user OAuth | **Yes** — same "one governed service credential, consumer/category-scoped" shape as `mcp-minierp`, `mcp-hubspot`, `mcp-clickup` |

**Decision: application permissions, IT-provisioned, matching the "include it in
the backend" instinct** — consistent with every other backend in this repo and
avoids building a per-user OAuth/token-storage subsystem that nothing else needs.
The cost of this choice is that the app credential itself can technically act on
any mailbox in scope, which shifts more weight onto the mitigations in §9.2 and
the identity-derivation rule in §9.3 — this isn't a free lunch, it's a real
trade against blast radius, made deliberately because the alternative (per-user
OAuth) is a bigger net-new subsystem for a single backend.

### 9.2 Bounding blast radius: Exchange Application Access Policies

Even with tenant-wide application permissions granted, **Exchange Online supports
an Application Access Policy** (`New-ApplicationAccessPolicy`) that restricts a
specific app's Graph mail/calendar reach to a named mail-enabled security group —
independent of code, managed entirely by IT. Concretely: create a security group
(e.g. `mcp-outlook-enrolled`), scope the app's access policy to it, and only
mailboxes IT adds to that group are ever reachable by this app's credential —
even though the underlying OAuth grant is technically tenant-wide. This is the
mitigation that makes application permissions an acceptable choice here rather
than an unbounded one; it should be treated as required, not optional-hardening.

### 9.3 The gap this exposes: no mailbox identity exists in `ConsumerRecord` today

Confirmed: `governance_core/store/models.py`'s `ConsumerRecord` has no email/UPN
field at all (`consumer_id`, `name`, `full_name`, `categories`, ... — nothing that
maps a platform login to a real O365 mailbox address). This isn't an oversight to
route around — login here is custom username+password (`design_plan_v2.md`), not
Microsoft SSO, so there is genuinely no existing link between a platform identity
and an O365 identity. Needed regardless of §9.1's auth choice:

- Add a `mailbox_upn` field to `ConsumerRecord`, settable via the existing admin
  consumer-management UI (IT maintains it — matches the app-only model's
  "no per-user action" property).
- The gateway derives the target mailbox **strictly from `mailbox_upn` on the
  authenticated session's own consumer record** — never a caller-supplied
  argument. Identical rule to `digest_persoanl_kb.md`'s "owner is server-derived
  only, never MCP-caller-supplied" decision — same shape of risk (one person
  reading another person's private data via a spoofed identifier), same fix.
- A consumer with no `mailbox_upn` set simply gets no Outlook tools available
  (or a clear "mailbox not linked, ask IT" response) — fails closed, not open.

### 9.4 Tools

First tools, read-only: `get_upcoming_meetings` (real calendar, not drafts),
`get_free_busy`, `search_inbox` (scoped, e.g. by sender/subject/date — not a
full-mailbox dump tool). Hold write/send Graph tools until these are proven and
until `mcp-clickup`/`mcp-acumatica` have already exercised the write-approval
pattern (§4, §7) at least once.

Sequence after `mcp-websearch`/`mcp-clickup` (§12) — §9.1-§9.3 above needed their
own answer before build starts, and now do.

---

## 10. Tool-selection at scale (why not just keep adding tools to every prompt)

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

### 10.1 Refinement (2026-08-18): where the industry actually landed, and where the real failures are

External research (out of scope when §10 was first written) confirms the embed+rerank
direction above, but sharpens two things worth acting on.

**This is now a converged industry pattern, not a bespoke idea.** OpenAI shipped "Tool
Search" (GPT-5.4+): a lightweight index the model queries, loading a tool's full schema
into context only once it decides to use it (47% token reduction at equal accuracy, per
OpenAI's own reporting). Anthropic shipped the same idea independently as "Tool Search
Tool" (public beta, Nov 2025): a BM25 index over tool names/docstrings, 85% token
reduction with the full tool library still reachable. The MCP spec's own best-practices
docs name this "progressive discovery," plus a gateway/catalog pattern for multi-server
setups (one server per domain, a metadata registry, centralized policy at the gateway) —
which is already this repo's shape (`mcp-minierp`/`mcp-knowledge`/`mcp-office`/... behind
one gateway, `policy/manifest.py` as the registry). Cohere's own public tool-use docs, by
contrast, don't address catalog-size scaling at all — they focus on schema-quality
guidance (`strict_tools` mode, descriptive schemas), which turns out to matter more than
§10 credited (next point).

**BFCL (Berkeley Function-Calling Leaderboard) shows something more specific than "pick
fewer tools":** accuracy drops from ~95-96% at 1 tool to ~65-78% at 20+ tools, but
**60-75% of failures at scale are parameter-mismatch on the CORRECTLY selected tool, not
wrong-tool-selection.** Narrowing the candidate set (this section's whole plan) addresses
the smaller share of the problem. Schema/description precision on whichever tool actually
gets picked — the discipline already applied to `compute_stats`/`group_stats`/
`percent_change`'s docstrings (explicit input shape, explicit "use this instead of X"
framing, explicit edge-case behavior) — carries at least as much weight as the retrieval
layer itself. Don't treat shipping tool retrieval as "solved" for reliability; keep
tightening tool descriptions as new tools land, especially ones with multi-field inputs.

**A two-level cascade, built to reuse what already exists, staged in when actually
needed:**
- **Level 1 — domain narrowing**, reusing `ToolPolicy.backend` (already exists for
  governance) as the grouping key — no new taxonomy needed. Narrow to the 1-2 backends a
  question plausibly touches before Level 2 ever sees full schemas.
- **Level 2 — the embed+rerank plan above**, operating within the Level-1-narrowed set
  rather than the full per-grant catalog.
- **Exemption: cross-cutting utility tools never get Level-1-narrowed.**
  `compute_stats`/`calculate`/`percent_change`/`group_stats` (and anything else in the
  "Data Aggregation" family, §10.2) have no `backend` tag and are relevant regardless of
  domain — a Level-1 pass keyed on `backend` would incorrectly drop them for any question
  not already classified into their (nonexistent) domain. They must always survive to
  Level 2 regardless of which domain(s) Level 1 selects.
- **Don't build Level 1 before the numbers justify it.** Per the revised trigger point
  above (~20, not ~50), check the actual current per-grant tool count before adding a
  domain-classification pass — Level 2 alone may already be sufficient if a typical grant
  is still comfortably under that range.

### 10.2 The "Data Aggregation" tool category

A fourth tool family, alongside the backend-specific ones (miniERP/knowledge/office/...):
narrow, ungated, cross-cutting math/utility tools that operate only on data the caller
already has from an earlier governed call — never a new data-access path, never gated,
because there's nothing backend-specific or sensitive about arithmetic itself. Exists
because a small self-hosted model (Qwen, quantized) is not reliable at exact arithmetic
across many values, at grouped aggregation, or at period-over-period math — the same
"discover, don't guess" principle `WORKFLOW_COPILOT_SYSTEM_PROMPT` already applies to
data, applied here to computation.

**Built (2026-08-18, `gateway/app.py`, tested in `_smoke/test_math_tools.py` and
`_smoke/test_tool_call_resilience.py`):**
- `compute_stats(values)` — flat sum/mean/min/max/median/stdev/variance.
- `calculate(expression)` — safe scalar arithmetic via an `ast`-restricted evaluator
  (never `eval()`); exponent- and length-capped against pathological input.
- `percent_change(from_value, to_value)` — period-over-period change, fixed formula so
  base/sign can't get flipped.
- `group_stats(rows)` — per-group count/sum/mean/min/max over `{"group","value"}` rows
  already in hand; explicitly NOT for company-wide/unbounded data (see its own
  docstring) — that's a job for a purpose-built bulk aggregation tool (e.g. a planned
  `get_financial_summary` over `Account`/`GLHistory`, designed but not yet built — see §13).
- Same pass also hardened `orchestrator.execute_tool` to catch any tool-call exception
  (bad args past FastMCP's own pydantic validation, a backend failure) and return a clean
  `{"source":"governance","status":"error",...}` result instead of crashing the whole
  chat turn — a gap these new tools' own tests surfaced, but one that applied to every
  existing tool too.

**Candidates for this category, not yet built — each should clear the same bar (a
demonstrated small-model arithmetic-reliability gap, not just "involves a number")
before being added, since the cascade above solves discovery cost, not whether a tool is
worth building in the first place:**
- **`date_diff`/`days_between(date_a, date_b)`** — probably the next one worth building.
  This domain is full of due-date/aging math (`get_ap_invoices_due_soon`,
  `get_ar_invoices_past_due`, PO expected dates) and date arithmetic (leap years,
  variable month lengths, off-by-one) is a distinct, well-known small-model weak spot —
  same justification class as the tools already built.
- **Multi-period compound growth rate** (geometric mean across >2 periods) — a plausible
  extension of `percent_change` for "average quarterly growth this year"-style asks. The
  formula is easy to get wrong by hand (same failure class), but defer until a real
  question needs it.
- **`top_n`/rank over an already-in-hand list** — defer; most ranking needs today are
  already served by tools that return pre-sorted results server-side
  (`get_top_customers_by_spend`). Revisit only if a real question needs to re-rank across
  multiple prior calls' combined results.
- **Weighted average** — defer until a real question needs paired values+weights; no
  demonstrated need yet.
- **Explicitly NOT this category: currency conversion.** Needs an exchange-rate data
  source, not pure math — that would be a finance-domain *data* tool (its own manifest
  entry/backend tag), not a math/utility tool, if it's ever needed at all.

---

## 11. Local LLM strategy for home-chatbot + workflow-copilot

Do **not** reach for agentic RL post-training first. In order of actual leverage:

1. **Tool retrieval (§10)** — shrinking the choice set from ~100 tools to ~15 relevant
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

### 11.1 Refinement (2026-08-18): does SFT go stale as the toolkit keeps growing?

Not "useless," but not free of a real risk either — worth being precise about which.

**A newly added tool isn't broken by not being in the training set.** The model always
reads the full tool schema fresh at inference time regardless of training — that's how
tool-calling works. SFT sharpens a *transferable* skill (disambiguating similar tools,
filling arguments correctly, following our conventions like "call `find_customer`
first"), not a fixed lookup table. A tool added after the last training run works at the
same baseline every tool works at today, pre-any-fine-tuning — not "useless," just not
specially reinforced yet.

**The real risk is narrower: a new tool that SUPERSEDES an old workaround the model was
already reinforced on.** Concrete case: if we'd fine-tuned before `get_financial_summary`
existed, any real traces of "answer a company-wide financial question" would show
whatever workaround the model used instead (looping single-account tools, or failing) —
training on that concentrates the model's distribution toward the old pattern, and it
could come out *worse* at picking the new, correct tool than an untrained base model
would be. This is the case that actually justifies re-training sooner, not tool additions
in general.

**Policy: retrain asymmetrically, not on every addition.**
- Genuinely new, non-overlapping capability — no urgency; works at baseline until the
  next scheduled retrain naturally picks up traces of it.
- A tool that replaces an existing workaround the model has already been reinforced on —
  retrain sooner; leaving the old reinforcement in place actively works against adopting
  the better tool.
- Otherwise, batch it (a cadence, or "N new tools shipped"), not continuously — LoRA/QLoRA
  keeps this viable since adapters are cheap to retrain/version/roll back compared to
  full fine-tuning.
- Maintain a standing eval set (fixed questions with expected tool calls, including ones
  exercising newer tools), run before/after every retrain — same regression-testing
  discipline `_smoke/` already applies to code, applied to model behavior.

**Architectural takeaway: let retrieval (§10.1), not fine-tuning, be the layer that keeps
pace with catalog growth.** Re-embedding a new tool's description is cheap and immediate;
re-training is expensive and periodic. Don't rely on SFT to track every addition in real
time — that's what the always-current retrieval layer is for. Fine-tuning is a slower
sharpening pass on top, not the mechanism responsible for freshness.

### 11.2 Refinement (2026-08-19): real head-to-head benchmark, local Qwen3.6-27B vs. Claude Sonnet

Ran the actual production copilot loop (`orchestrator.run_chat`, real system prompt, real
tool specs, real governed pipeline) against both backends for 7 finance-domain use cases,
through the real ERP mirror — not a synthetic eval. Ground truth (expected tool + args)
computed independently via direct governed calls before either model ran. Full method,
transcripts, and per-turn timing: `governence_agent/_smoke/.tmp/copilot-compare/` (this
run's artifacts; regenerate via the harness used this session if the directory has since
been cleaned).

**Tool selection: both models are good, 13/14 exact matches.** Both correctly mapped
free-text ("next 60 days") to the right non-default argument (`days_ahead=60`), both
correctly called zero tools for a conceptual question, both correctly reported "not found"
for a fake vendor code rather than fabricating a profile. Tool selection is not the local
model's weak point.

**The real gap is bulk/multi-turn flows, not tool selection — and it's not just "slower,"
it's a genuine reliability cliff at the local model's small context window (40,960
tokens):**
- **Hard failure on a bulk list.** Asking for AP invoices due in the next 60 days (a
  real, unremarkable ask — 1,210 real rows) made the *local* model's own tool call
  succeed, then its next completion call **hard-error**: `input (160,588 tokens) is
  longer than the model's context length (40,960)`. The user gets no answer at all.
  Claude Sonnet took the identical 1,210-row payload and answered fine (in ~22s).
- **Silent garbage output, not a crash.** On a payment-history question, the local model
  paged through results 3 times, then tried to self-initiate a `group_stats` call the
  user never asked for, ran out of its 6-turn/1024-token budget mid-generation, and
  returned a **truncated, unparsed `<tool_call>` tag as its literal final answer** — a
  broken response that looks like a leaked internal artifact, not an error a caller can
  detect programmatically (`status` still comes back as a normal completed turn).
- **Latency cliff on multi-turn chains.** A two-tool "profile + full invoice list" ask
  took the local model 5 turns / 91 seconds (one single turn alone took 48s) vs. Sonnet's
  4 turns / 33 seconds for the identical task — and the extra turn was, again, an
  unrequested `compute_stats` call the local model reached for on its own.
- **Single-record lookups: no meaningful gap.** For simple one-tool questions (vendor
  profile, one invoice's detail, not-found handling), wall-clock time was within ~1-2
  seconds either direction and both models produced clean, correct answers. This is
  where the local-model / hosted-model home-chat split from §11's original guidance
  already holds up fine.

**Root cause, not just symptom:** as accumulated tool-result context grows across turns,
the local 27B model's per-turn latency degrades non-linearly (not linearly with token
count) and it becomes *more* likely to self-initiate an unrequested aggregation call
(`compute_stats`/`group_stats`) — the always-available cross-cutting math tools from
§10.2 — rather than just answering from the data already in hand. Claude Sonnet showed
no equivalent tendency in this run.

**Implication for the routing decision above:** the risk split that actually predicts
local-model trouble is **bulk/paginated/multi-turn vs. single-record**, not simply "which
chat surface." A home-chat question that happens to hit a bulk digest-style tool
(`get_ap_invoices_due_soon`, `get_ar_invoices_past_due`, and future
`get_shipment_exceptions`/`get_financial_summary`) carries the same risk profile as a
workflow-copilot turn, regardless of which lane's system prompt is in effect. Before
trusting the local model with any bulk-shaped flow in production:
- Cap default `page_size` more conservatively for the local lane specifically (the
  gateway wrapper's own default of 250 is what produced the 160K-token blowout — a
  smaller default, or a hard row-count-to-token-estimate check before returning a bulk
  result, would catch this before it ever reaches the model).
- Consider excluding the math/utility tools (§10.2) from the local model's tool set for
  bulk-shaped questions specifically, or strengthen the system prompt's "use the data you
  already have, don't re-derive it" instruction for that lane — the unrequested
  `compute_stats`/`group_stats` calls were the direct cause of both the worst-latency
  case (UC4) and the garbled-answer case (UC5).
- This is exactly the class of gap the still-open per-run tool-call budget (§5 of
  `finalize_stage_1.md`) and stricter pagination discipline would catch structurally,
  rather than relying on the model to self-limit.

---

## 12. Rollout sequencing

1. Shared backend scaffold (§3) — nothing else should be built on the old ad hoc pattern.
2. `mcp-hubspot`, read-only (§5) — proves the scaffold on a lower-risk backend.
3. `mcp-websearch` (§6) — independent of the ERP-shaped backends, closes the
   biggest non-ERP tool-catalog gap, low governance novelty beyond the injection
   defense.
4. `mcp-clickup`, read-first (§7) — same scaffold, same risk-tier discipline.
5. Tool retrieval / embed+rerank layer (§10) — needed once backend #4+ lands, not before.
6. `mcp-acumatica` pilot write tool (§4) — highest risk, most conservative, goes last,
   gated on the scaffold and the approval pattern both being proven elsewhere first.
7. `mcp-code`/opencode real-execution decision (§8) — independent of the rest;
   make the scope decision explicitly rather than letting it happen as a side
   effect of finding an embeddable SDK.
8. `mcp-outlook` real inbox/calendar reads (§9) — sequence last: needs its own
   identity-model answer (acting *as* a specific person vs. a shared service
   account) before build starts.

## 13. Open questions blocking work

- Acumatica contract-based REST API availability + version, and provisioning a scoped
  pilot integration user — **owned by whoever admins the Acumatica instance**, not us.
- Which entity to pick for the Acumatica write pilot (order note vs. tracking number
  vs. something else) — needs a product decision on what's actually useful *and* low-risk.
- Whether a staging/sandbox Acumatica tenant exists to test against before production.
- Web search provider choice (Tavily vs. Brave vs. self-hosted SearXNG) — needs a
  cost/quality/data-exposure tradeoff decision, not just a technical pick.
- Do we actually want opencode's real-execution mode in this workspace at all, or
  is read-only planning sufficient? (§8) — a product decision, not just "we found
  an SDK for it."
- ~~Which mailbox/calendar system does Frontier Dental actually use~~ — **resolved:
  Microsoft Graph/365** (§9). Auth model and mailbox-identity gap resolved in §9.1-9.3;
  remaining open item is purely operational: who in IT registers the Entra ID app
  and owns the Application Access Policy / enrolled-mailbox security group (§9.2)?
- `get_financial_summary` (§10.2) — designed (query `Account` where `type` is a
  revenue/expense type + `active`, then `GLHistory` for those account ids + the target
  period, sum by type in Python — two bounded queries, no per-account looping) but not
  yet built. One real unknown before writing it: the actual string values this
  deployment's `Account.type` uses ("Income"/"Expense" is the standard Acumatica
  convention, not yet confirmed against live data) — probe live before hardcoding a
  filter, same discipline as this doc's other "confirmed by probing" claims.
