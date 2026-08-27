# Make MCP Robust — Post-2026-07-28-Spec Hardening Plan

**Status:** Proposed (planning only — nothing here is built yet unless marked
"confirmed built" below).
**Date:** 2026-08-26
**Owner:** Platform / Governance
**Scope:** `governence_agent/gateway/mcp_clients.py`, `governence_agent/gateway/govern.py`,
`governence_agent/governance_core/{edge,scope_store,audit}.py`, and the backend
FastMCP servers (`mcp-minierp`, `mcp-office`, `mcp-email`, `mcp-knowledge`,
`mcp-calendar`, `mcp-code`).
**Trigger:** The MCP spec jumped to **2026-07-28** (stateless core, MRTR,
`CacheableResult`, auth hardening, Roots/Sampling/Logging deprecated). See §1.
**Builds on:** `design_plan.md` §10 (Production-readiness checklist) and
`finalize_stage_1.md` — this doc re-audits both against the *live* code (not
the doc's claims) and turns the still-open items into an actionable, sequenced
plan, folded together with what the new spec unlocks.

---

> **Read this before implementing anything below.** Every "decisions locked"
> row is this session's best-effort design, not a final spec. Docs in this
> repo go stale fast (`design_plan.md` §10 already claims some things as
> undone that turned out done — see §2). Before starting on any item:
> 1. Re-verify the relevant claim against the live code.
> 2. Ask the user about anything with a real security/data tradeoff (the IDOR
>    fix in §5 especially) rather than silently proceeding.
> 3. Only then write code.

---

## 1. Why now — the 2026-07-28 MCP spec

Full detail already discussed in-session; the parts that matter for this repo:

- **Stateless core** (SEP-2575): `initialize`/`notifications/initialized` handshake
  removed, `Mcp-Session-Id` gone, `server/discover` added. Every backend here
  already runs `stateless_http=True` and the gateway already opens a fresh
  session per call (`mcp_clients.py`) — this repo anticipated the spec, not
  the other way around.
- **`CacheableResult`** (`ttlMs`, `cacheScope`) on `tools/list`/`resources/list`
  — standardizes what `gateway/schema_catalog.py` already hand-rolls as a
  process-lifetime `_CACHE` dict.
- **Deterministic `tools/list` ordering** (new SHOULD) — matters for LLM
  prompt-cache hit rate on the tool list the orchestrator sends.
- **Roots / Sampling / Logging deprecated** — confirmed unused in this repo
  (grepped; only false-positive "sampling" hits in finance/analytics code).
  Nothing to migrate.
- **OpenTelemetry `_meta` convention** (`traceparent`, `tracestate`, `baggage`)
  — usable today, doesn't require SDK version bump, and plugs a real hole
  (see §4).
- **SDK dependency:** `mcp==1.28.1` is exact-pinned in both
  `requirements.txt` and `gateway/requirements.txt` (deliberately, per the
  Oryx/Azure comment there). Nothing that requires the new handshake removal
  is actionable until that SDK ships 2026-07-28 support — tracked as an open
  item, not blocking the rest of this plan.

---

## 2. Current-state audit (live code, not doc claims)

`design_plan.md` §10's checklist, re-verified 2026-08-26:

| Checklist item | Doc says | Actually is | Evidence |
|---|---|---|---|
| Durable audit sink | "move to Cosmos" (open) | **✅ Done** | `governance_core/audit.py` — Cosmos container with TTL, JSONL file fallback, ring-buffer hydration on boot |
| Rate-limit state off-process | "move to Redis" (open) | **❌ Still in-memory** | `edge.py:77` `_rate_state: dict` — single instance, resets on restart |
| Session scope store off-process | "move to Redis" (open) | **❌ Still in-memory** | `scope_store.py:27` `_sessions: dict` — same issue |
| IDOR: trust-on-first-use `customer_id` | "gate behind existence check" (open) | **❌ Still open** — flagged in the module's own docstring as "the biggest current IDOR exposure" | `scope_store.py:9-16` |
| Per-backend timeout/circuit breaker | "in the gateway" (open) | **❌ Not built** — one global `GATEWAY_BACKEND_TIMEOUT_SEC`, no retry, no breaker | `mcp_clients.py:89-91` |
| Versioned/tested policy | "golden tests" (open) | **❌ Not found** — no `(consumer, tool, args) → verdict` regression suite located |
| Rate limits per classification tier | "not just per consumer" (open) | **❌ Still per-consumer only** | `edge.py:109-119` |
| Network isolation (VNet/mTLS) | "decided: Container Apps + VNet" | Infra-level, not verifiable from code — carry forward as-is |

Net: audit durability is done; everything else in that checklist is still open
and maps directly onto the tracks below.

---

## 3. Track A — Spec-aligned wins (low risk, do first)

### A.1 Distributed tracing via `_meta`
Today `request_context.py`'s `request_id` dies at the `mcp_clients.call()`
boundary — nothing correlates a gateway-side audit record with the backend's
own GraphQL call in `minierp_core/graphql_client.py` when something goes
wrong three hops deep.

**Proposal:** thread a trace id through `_meta.traceparent` (W3C Trace
Context format, sourced from the existing `request_id` when no real tracer is
wired up yet) on every outbound `mcp_clients.call()`/`ping()`/
`get_tool_schema()`, and have backend tools log it on error. No SDK version
bump required — `_meta` passthrough is already spec-legal.

### A.2 Deterministic tool ordering check
One-time audit: confirm `policy/manifest.py`'s backend-tag filtering
(used when building the tool list the orchestrator hands the LLM) preserves a
stable order across requests. If it doesn't, fix it — cheap, and now a
spec-encouraged behavior rather than incidental.

### A.3 SDK watch
Add a tracked TODO (not code) to bump `mcp==1.28.1` once 2026-07-28 support
ships, at which point `session.initialize()` can be dropped from
`mcp_clients.py`'s three call sites (§4 below is written to not depend on this).

---

## 4. Track B — Backend call resilience (`gateway/mcp_clients.py`)

This is the single highest-value change: every governed tool call passes
through `_govern()` → `mcp_clients.call()`, so hardening this one module
hardens the whole system at once.

### 4.1 Current shape (for reference)
`call()`, `ping()`, `get_tool_schema()` each: open a fresh
`streamablehttp_client` + `ClientSession`, `initialize()`, do the RPC, wrapped
in one `asyncio.wait_for(..., timeout=_timeout_sec())` (one global
`GATEWAY_BACKEND_TIMEOUT_SEC=30`). Any failure → `BackendError` → surfaces as
`_error_result()` in `govern.py` with `errorCode: type(exc).__name__`. No
retry. No breaker. No per-backend budget.

### 4.2 Proposed additions

**Bounded retry with jitter — reads only.** Every tool in this repo is a
read (confirmed: no write tools exist yet in `mcp-minierp`/etc.), so retry is
always safe from an idempotency standpoint. Retry only on *transport*
failures (`ConnectError`, `TimeoutError`, connection reset) — never on a
tool-level `isError` result, which is a real answer (e.g. "missing_identifier"),
not a transient fault. Proposed: 2 retries, exponential backoff with jitter
(e.g. 150ms, 450ms base), total retry budget bounded so worst case stays
under ~2x the single-call timeout.

**Per-backend circuit breaker.** Track consecutive failures per backend name
(the same keys as `_DEFAULT_URLS`: `minierp_orders`, `office`, `email`, ...).
On N consecutive failures (proposed default 5), open the breaker for a cooldown
window (proposed default 30s) — further calls to that backend fail fast with
a distinct `"backend_unavailable"` status instead of paying a full timeout
each time. Half-open probe after cooldown (one trial call) before fully
closing. Feed the same state `ping()` already produces for the admin health
panel, rather than duplicating tracking.

**Per-backend / per-risk-tier timeout config.** Replace the single
`GATEWAY_BACKEND_TIMEOUT_SEC` with a lookup: backend-level override via
`{BACKEND}_MCP_TIMEOUT_SEC` (mirrors the existing `{BACKEND}_MCP_URL`
pattern in `backend_url()`), falling back to the current global default.
Rationale: a `fetch_all=true` GL pull and a single-customer profile lookup
shouldn't share a budget, and this needs no new concept — just extending the
existing per-backend env convention.

**Richer error taxonomy surfaced to the caller, not just the audit log.**
`govern.py::_error_result()` today collapses everything to
`errorCode: type(exc).__name__` (e.g. `BackendError` — not useful to an LLM
deciding whether to retry vs. give up vs. tell the user "try again shortly").
Proposed statuses: `timeout` | `backend_unavailable` (breaker open) |
`backend_error` (backend returned `isError`) | `transport_error` (connection-level
failure). `mcp_clients.BackendError` gains a `.kind` attribute set at each
raise site; `_error_result()` maps it straight through instead of using the
exception's class name.

### 4.3 What this does NOT change
- No change to `_govern()`'s PDP/redaction/audit pipeline — retry and breaker
  logic live entirely inside `mcp_clients.py`, called the same way.
- No change to backend servers — this is purely gateway-side.
- No behavior change for the common case (backend healthy) beyond the `_meta`
  trace id.

---

## 5. Track C — Governance gaps (needs a decision before building)

### 5.1 IDOR: trust-on-first-use `customer_id` — needs a decision
`scope_store.py` trusts whichever `customer_id` a caller supplies first for a
session, for the rest of that session, with no ownership check. This is the
one item in this plan with real security weight and a real design question:
what does "verify" mean here?

Options to weigh with the user before building (not decided in this doc):
- **(a) Live existence check** — first `customer_id` seen in a session must
  resolve via `find_customer`/an equivalent lookup before being trusted, so a
  guessed id that doesn't exist is rejected. Doesn't prove *ownership*, only
  *existence*.
- **(b) Bind to an authenticated identity** — if callers ever carry a
  verified end-user identity (not just the consumer API key), require the
  first-use `customer_id` to match a lookup keyed on that identity (e.g. "this
  chat session belongs to contact X, whose account is Y"). Stronger, but only
  works where such an identity exists upstream — needs to be confirmed per
  consumer (chatbot vs. email_bot vs. future agents).
- **(c) Leave as-is, narrow the blast radius instead** — tighter rate limits
  and anomaly detection (many distinct `customer_id`s from one session/consumer
  in a short window) as a detective control rather than a preventive one.

**This section is intentionally a menu, not a plan** — flagging for
discussion rather than picking (a)/(b)/(c) unilaterally, per this doc's own
opening caveat.

### 5.2 Redis migration for rate-limit + scope state — decided: not needed now
**Decided 2026-08-26:** no Redis provisioned, and at current scale (<10
concurrent users, single always-on App Service instance per
`design_plan.md` §11 — no horizontal scale-out) in-memory state in
`edge.py::_rate_state` and `scope_store.py::_sessions` is the right call, not
premature infra. The problem this would solve (state not surviving a restart,
not shared across replicas) only bites at >1 replica or if losing session
scope on a deploy becomes a real disruption — neither is true today. A
redeploy resets rate-limit counters and forces a caller to re-supply
`customer_id` once; harmless at this volume.

**Revisit when:** the gateway is scaled to >1 App Service instance, or
restart-survival of session scope becomes a hard requirement. Until then, no
action — dropped from the sequencing below.

### 5.3 Per-classification rate limits
Extend `check_rate_limit()` (`edge.py`) to key on `(consumer, risk_tier)`
instead of `consumer` alone, using the risk tier already present in
`policy/manifest.py`'s per-tool declarations (`verdict.risk`, referenced in
`govern.py:274`). A cheap PUBLIC lookup and a bulk PII export currently share
one hourly budget; this lets them be budgeted independently without a new
concept — the classification data already exists.

### 5.4 Golden policy tests
A `(consumer, tool, args, scope) → expected verdict` regression suite against
`policy/decision.py::decide()`, mirroring the "mirror the chatbot's
`evaluation/` harness for policy" line already in `design_plan.md` §10. Pure
unit-test work, no design risk — good candidate to build alongside Track B
rather than blocked on anything.

---

## 6. Sequencing

1. **Track B** (`mcp_clients.py` resilience) — self-contained, no design
   tradeoffs, highest leverage (every governed call passes through it).
2. **Track A** alongside it — same file, same PR-sized unit of work.
3. **§5.4 golden policy tests** — independent, can run in parallel with 1-2.
4. **§5.1 IDOR decision** — needs a conversation with the user first; not
   started until (a)/(b)/(c) (or another option) is chosen.
5. **§5.3 per-classification rate limits** — small, do after Track B lands
   since it touches the same risk-tier data Track B's error taxonomy also
   references.

(§5.2 Redis migration dropped from sequencing — decided not needed at
current scale; see §5.2.)

---

## 7. Open questions for the user

- ~~Is Redis already provisioned?~~ **Resolved 2026-08-26: no, and not
  needed at current scale (<10 concurrent users, single instance) — see §5.2.**
- For §5.1 (IDOR), which of (a)/(b)/(c) — or something else — fits how
  `customer_id` actually gets supplied today (chatbot free-text vs. an
  upstream authenticated identity)?
- Retry/breaker defaults in §4.2 (2 retries, 5-failure breaker threshold,
  30s cooldown) are starting guesses — confirm or adjust before implementing.
