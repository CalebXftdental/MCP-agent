# Federating Governance Gateway — Design Plan

**Status:** Proposed
**Owner:** Platform / Governance
**Scope:** `governence_agent/` — evolve today's single `governance_service` into a
multi-MCP governance plane fronting miniERP, HubSpot, Acumatica (and future systems).

---

## 1. Goal

We have several systems of record (miniERP, HubSpot, Acumatica, more to come) and a
growing set of AI agents and human users that need data from them. We want **one place**
that:

- authenticates every caller and knows *who* they are,
- decides *whether* a given call is allowed,
- controls *what data comes back* to each requester (per-requester censorship / redaction),
- and produces a single, durable audit trail of every access.

Because every agent and user retrieves data *only* through MCP, and every MCP call is
brokered by this one component, the communication between callers and the databases is
fully mediated and controllable. That component is the **Governance Gateway**.

Non-goal: putting an LLM in the synchronous allow/deny path. The hot path is a
deterministic policy engine. The LLM "governance agent" runs in the control plane,
off the hot path (see §9).

---

## 2. Where we are today

`governance_service/` already implements the right skeleton, but governance and
data-access live in **one process**:

| File | Role today |
|---|---|
| `app.py` | Layer 2: FastMCP server + all miniERP/HubSpot typed tools + per-tool scope checks + `_apply_email_policy` redaction + audit calls. Also holds all upstream credentials. |
| `edge.py` | Layer 1: per-consumer API-key auth (`GOVERNANCE_KEY_*`), fixed-window rate limits, IP allowlist, body-size limit, request-id, auth/rate audit. |
| `session_store.py` | Per-session `customer_id` scoping (in-memory). |
| `audit.py` | Structured JSON audit → stdout + in-memory ring buffer powering `/dashboard`. |
| `request_context.py` | Per-request identity via `contextvars`, shared between layers. |
| `middleware/` | Legacy chatbot-side topic allowlist / scope injection / presentation policy (reference, not the enforcement path). |
| `sqlagent/`, `hubspot/` | The actual data-access clients + credentials. |

**What's already right:** single credentialed choke point; API key *is* the identity;
server-side session scoping (never trust caller-supplied `customer_id` at face value for
account-scoped tools); a unified audit chokepoint; output redaction as a concept.

**What blocks scaling to N backends:** governance is fused to one backend's tools. Adding
Acumatica the current way means either a second monolith (drift, duplicated auth/audit) or
piling every system's tools + credentials into one ever-growing process. Both are wrong.

---

## 3. Target architecture

One endpoint that all callers talk to. It speaks MCP to callers and is itself an MCP
*client* to the backend MCP servers. Backends move to a private network and hold only
their own domain's credentials.

```
   AI agents,                 ┌──────────────────────────────────────────┐
   users, email bot  ──MCP──▶ │          GOVERNANCE GATEWAY (PEP)         │
   (ONE endpoint,             │  authn → PDP decision → route → redact    │
    per-consumer key)         │  → audit                                  │
                              └───┬─────────────┬─────────────┬───────────┘
                     MCP client   │             │             │  private network
                                  ▼             ▼             ▼  (mTLS / VNet PE only)
                           mcp-minierp     mcp-hubspot    mcp-acumatica
                           (SQL/GraphQL)   (CRM API)      (Acumatica API)
                                  │             │             │
                                  ▼             ▼             ▼
                        upstream systems + their own credentials (Key Vault)
```

### Why *federation*, not a path-based HTTP proxy

Our headline requirement is **per-requester censorship of results**. A plain reverse
proxy (`/mcp-minierp`, `/mcp-hubspot`) only sees opaque JSON-RPC bytes and can't redact by
tool/field/requester. The gateway must **terminate MCP**, see `tool_name + args + result`,
and rewrite the result. So the gateway is:

- an **MCP server** to callers — it advertises a single, namespaced tool surface
  (`minierp.get_customer_orders`, `hubspot.get_sales_rep`, `acumatica.get_invoice`), and
- an **MCP client** to each backend — it federates/aggregates their advertised tools.

---

## 4. Component roles (zero-trust / XACML vocabulary)

Splitting the monolith into named roles is what makes this "industry level" and keeps each
piece independently testable.

| Role | Responsibility | Maps to |
|---|---|---|
| **PEP** — Policy Enforcement Point | The gateway. Intercepts every call, asks the PDP, enforces the verdict, applies the redaction plan, audits. | `edge.py` grows into this |
| **PDP** — Policy Decision Point | Pure function `(identity, tool, args, scope, field-tags) → allow / deny / redaction-plan`. **Deterministic. No LLM. No I/O beyond the PIP.** | new `policy/` package |
| **PIP** — Policy Information Point | Supplies facts the PDP needs: consumer entitlements, session scope, per-tool field classification. | `session_store.py` + new `entitlements`, `classification` |
| **PAP** — Policy Administration Point | Where policy is authored, reviewed, versioned. | config repo + dashboard |
| **Audit** | Immutable record of every decision (allow, deny, redaction applied). | `audit.py` → durable sink |

**Fail closed:** PDP error, unknown tool, backend timeout, or missing classification → deny.

---

## 5. The censorship / redaction model

Generalize today's single `_apply_email_policy` into a data-driven model that scales across
all backends and tools.

1. **Classify every field a tool can return.** Each backend ships a **tool manifest**
   declaring, per tool: its scope requirement (e.g. `account_scoped: true`) and the
   classification of each output field:
   `PUBLIC | INTERNAL | PII | PCI | SENSITIVE`.

2. **Give every requester an entitlement profile.** Extend the `GOVERNANCE_KEY_*` consumer
   record with what classifications it may see, and under what condition. Example:
   - `chatbot` → `PUBLIC + INTERNAL`; `PII` only for the customer currently verified in scope.
   - `email_bot` → `PUBLIC + INTERNAL + PII(rep contact)`.
   - `analytics_agent` → aggregates only; no row-level `PII`.

3. **Redaction is a transform chosen by the PDP,** applied by the PEP to the backend result
   before it is returned: `pass | drop | mask` (`j***@x.com`) `| hash/tokenize`.

So "the governance agent controls what each requester gets" becomes concrete and
deterministic: *the PDP resolves requester entitlements against result field
classifications and emits a redaction plan; the PEP applies it and records what it redacted.*

This is exactly `_apply_email_policy` — generalized, data-driven, and uniform across
miniERP, HubSpot, and Acumatica.

---

## 6. Request lifecycle (hot path)

```
1. Caller opens MCP session to the gateway, presents its consumer key.
2. PEP (edge): authn (key → consumer identity), IP allowlist, body size, rate/quota.
   └─ reject → audit(auth_denied | rate_limited), fail closed.
3. PEP receives tools/call: resolves tool → backend + canonical tool name (de-namespace).
4. PEP resolves session scope (customer_id) from the scope store, server-side.
5. PEP → PDP.decide(identity, tool, args, scope, tool_manifest):
   └─ deny  → audit(denied), return typed refusal (missing scope, not entitled, …).
   └─ allow → returns a redaction plan.
6. PEP → backend MCP server (as MCP client), forwarding sanitized args (+ resolved scope).
   └─ timeout / error → circuit-breaker, audit(error), fail closed.
7. PEP applies the redaction plan to the backend result.
8. PEP audits (call: consumer, tool, args summary, session, status, latency, redactions).
9. PEP returns the redacted result to the caller.
```

Everything in steps 2–8 already has a home in the current code — this plan relocates and
generalizes it, it does not invent it from scratch.

---

## 7. Repository layout (target)

```
governence_agent/
├── design_plan.md                  # this document
├── governance_core/                # backend-agnostic shared plane (extracted from today)
│   ├── edge.py                     #   authn, rate/quota, IP allowlist, body size, request-id
│   ├── audit.py                    #   structured audit → durable sink
│   ├── request_context.py          #   per-request identity (contextvars)
│   ├── scope_store.py              #   per-session account scope (was session_store.py)
│   ├── policy/                     #   PDP
│   │   ├── decision.py             #     decide(identity, tool, args, scope, manifest) -> verdict
│   │   ├── entitlements.py         #     consumer -> allowed classifications / conditions (PIP)
│   │   └── redaction.py            #     apply redaction plan to a result
│   └── classification.py           #   field classification helpers (PIP)
├── gateway/                        # the Governance Gateway (PEP) — the ONLY public endpoint
│   ├── app.py                      #   FastMCP server + EdgeMiddleware; federates backends
│   ├── router.py                   #   namespaced tool -> backend MCP client + canonical name
│   ├── mcp_clients.py              #   outbound MCP client pool (per-backend URL, timeouts, breaker)
│   └── static/dashboard.html       #   monitoring UI
├── mcp-minierp/                    # backend MCP server: miniERP tools only + its creds
│   ├── app.py                      #   thin FastMCP; NO auth/rate/redaction (gateway's job)
│   ├── manifest.py                 #   tool -> scope + field classifications
│   └── sqlagent/                   #   moved from governance_service/sqlagent
├── mcp-hubspot/                    # backend MCP server: HubSpot tools only + its creds
│   ├── app.py
│   ├── manifest.py
│   └── hubspot/
├── mcp-acumatica/                  # backend MCP server: Acumatica tools + its creds (new)
│   ├── app.py
│   ├── manifest.py
│   └── acumatica/
└── governance_service/             # DEPRECATED once migration lands; kept until cutover
```

Backends are **thin**: typed tools + their own credentials + a manifest. They do **no**
auth, rate-limiting, or redaction — those belong to the gateway alone, so there is one
enforcement path, not four.

---

## 8. Migration path (incremental, no big-bang rewrite)

Each step ships independently and leaves the system working.

1. **Extract `governance_core/`.** Move `edge.py`, `audit.py`, `request_context.py`, and
   `session_store.py` (→ `scope_store.py`) out of `governance_service` unchanged. They are
   already backend-agnostic — nothing in them is miniERP-specific.
2. **Carve backends out.** Create `mcp-minierp` (today's `sqlagent` tools) and `mcp-hubspot`
   (today's `hubspot_query` tools) as standalone thin FastMCP servers with **no** governance
   middleware. Verify each in isolation.
3. **Add tool manifests.** For each backend tool, declare scope requirement + output-field
   classifications. This single source replaces the hardcoded allowlists in
   `middleware/query_guard.py`.
4. **Build the gateway.** FastMCP server running `EdgeMiddleware`; on each `tools/call` it
   consults the PDP, forwards to the right backend via an MCP client, applies the redaction
   plan, audits. Tool registration = federate + namespace backend tools.
5. **Lift policy out of handlers.** The `_resolve_scope` / `_denied` / `_CUSTOMER_ID_REQUIRED`
   logic in today's `app.py` becomes PDP rules keyed on manifest metadata
   (`account_scoped: true`), and `_apply_email_policy` becomes an entitlement + classification
   rule — no longer copy-pasted per tool.
6. **Cut consumers over** to the gateway endpoint (chatbot orchestrator first), watch the
   dashboard, then **add `mcp-acumatica`** as the first backend built the new way from day one.
7. **Delete `governance_service/`** once all consumers are on the gateway.

---

## 9. Where the LLM governance agent belongs

**Not on the hot path.** An LLM in front of every tool call adds latency, non-determinism,
and a prompt-injection surface on the security boundary. The hot path stays deterministic.

The LLM governance agent runs in the **control plane**, consuming the audit stream the
gateway already produces:

- **Anomaly detection / investigation** — e.g. "`email_bot` suddenly pulled 40 distinct
  customers' contacts in 5 minutes" → flag, draft an incident, optionally revoke a session.
- **Policy-authoring assistant (PAP)** — propose entitlement/redaction rules from observed
  traffic; a human approves; the deterministic PDP enforces. Human-in-the-loop.
- **Async / sampled deep inspection** — a second-pass review of high-sensitivity calls, out
  of band, that can retroactively tighten a rule — never blocking a live response.

This keeps enforcement fast and auditable while the agent does the judgment work humans
can't scale to.

---

## 10. Production-readiness checklist

State and durability (today's code flags most of these as POC in its own docstrings):

- [ ] **Off-process state.** Rate-limit counters (`edge._rate_state`), session scope
      (`session_store._sessions`), and the audit ring buffer are per-instance in-memory.
      Move rate limits + scope to **Redis**, audit to an append-only sink
      (**Cosmos / Log Analytics**) before running >1 replica.
- [ ] **Secrets in Key Vault + managed identity**, not `.env.local`. Only backends hold
      upstream credentials; the gateway holds only consumer keys.
- [ ] **Tighten the scope trust model.** `session_store` trusts the last-supplied
      `customer_id` for the rest of the session (a known, pre-existing relaxation). Gate
      first-use of a `customer_id` behind a real existence/ownership check — this is the
      biggest current IDOR exposure (one caller reading another customer's data by guessing
      an id).
- [ ] **Network isolation.** Backends reachable only from the gateway (VNet private
      endpoints / mTLS). Keep the DNS-rebinding host allowlist already configured in `app.py`.
- [ ] **Fail closed everywhere.** PDP error, unknown tool, missing classification, backend
      timeout → deny.
- [ ] **Per-backend timeouts + circuit breakers** in the gateway, so a hung Acumatica call
      can't exhaust it.
- [ ] **Versioned, tested policy.** Golden tests: `(consumer, tool, args) → expected
      allow/deny + redaction plan`. Mirror the chatbot's `evaluation/` harness for policy.
- [ ] **Rate limits per (consumer, tool) or classification tier**, not just per consumer, so
      a cheap PUBLIC lookup and a sensitive PII pull aren't budgeted identically.

---

## 11. Deployment substrate (decided) + open questions

**Decided:**

- **Gateway (governance MCP):** Azure **App Service** — always-on. It holds the
  client-facing MCP sessions and is the single public endpoint, so it wants a warm,
  stable process rather than scale-to-zero.
- **Backend MCPs (`mcp-minierp`, `mcp-hubspot`, `mcp-acumatica`):** Azure **Container
  Apps** — one app per backend. Chosen over consumption Function Apps because Container
  Apps give the same scale-to-zero economics *without* the drawbacks that matter here:
  - **scale-to-zero** (min replicas = 0) for genuinely idle backends → pay ~nothing when unused;
  - **min replicas = 1** on hot backends → no cold-start latency on the customer-facing path;
  - **first-class VNet + private endpoints** → satisfies the §10 isolation requirement
    (consumption Functions can't be locked into a VNet with inbound private endpoints;
    that would force Elastic Premium, which erases the per-call cost win anyway);
  - **stable, bounded instance count** → avoids the DB connection-pool blow-up that
    consumption Functions cause by fanning out to many independent instances;
  - **uniform ops model** across all backends (one deploy/scale/monitor story).

  Right-size per backend by traffic shape: steady daytime traffic → min replicas = 1;
  genuinely bursty with long idle gaps → min replicas = 0.

**Open questions:**

- **Shared state:** is Redis and/or Cosmos already provisioned, or do we stand them up as
  part of step 10?
- **Backend transport:** MCP Streamable HTTP over the private network (consistent with today)
  vs a lighter internal RPC. Recommendation: keep MCP end-to-end so backends stay independently
  usable and the gateway stays a pure MCP federator.
- **Acumatica auth model** (OAuth vs API keys) — affects only `mcp-acumatica`'s own secret
  handling, not the gateway.

---

## 12. Summary

Evolve the current fused service into: **a federating Governance Gateway (PEP)** that is the
single MCP endpoint for all callers, **thin per-system backend MCP servers** that hold only
their own credentials on a private network, **a deterministic PDP** driven by **tool
manifests + consumer entitlements + field classification** for per-requester censorship, a
**durable audit trail**, and an **off-hot-path LLM governance agent** for anomaly detection
and policy authoring. Migrate incrementally — extract the shared core, split the backends,
build the gateway, then add Acumatica the new way and retire the monolith.
