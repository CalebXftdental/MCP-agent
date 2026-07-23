# Governance Plane v2 — Control Plane, Access Model, and Security

**Status:** Proposed (design locked; ready to build)
**Builds on:** `design_plan.md` (architecture) + `STAGE1_README.md` (gateway + mcp-minierp, built & verified)
**Scope:** The control plane on top of Stage 1 — how policy is stored, authored via
the dashboard, and enforced; the category-based access model; the identity /
credential model; and the security hardening roadmap.

> **2026-07 terminology change:** the access grouping was renamed from **department**
> → **category**, and a category now corresponds **1:1 to a data domain / backend**
> (`accounts`, `finance`, `orders`, `shipments`, …). A principal is assigned **one or
> more categories** (a set), and its effective grant is the **union** of those
> categories' grants minus its deny-overrides. This replaces the earlier
> single-department-per-principal model. **The code rename is DONE**
> (`policy/categories.py`, `ConsumerRecord.categories: list`, union resolution in
> `policy/resolve.py`, `/admin/categories*`, store `get_category`/`categories()`,
> Cosmos container `categories`, env `GOVERNANCE_CATEGORY_*`, dashboard UI). ERP
> `department` fields in `mcp-minierp-*/sqlagent` are unrelated and were left untouched.

Deployment (Part A) is intentionally sequenced **last** — after the control plane and
access model are in place.

---

## 0. Decisions locked (quick reference)

| # | Decision | Choice |
|---|---|---|
| Control-plane placement | separate service vs. gateway routes | **Same deployment**, path-split: `/dashboard` (all users), `/dashboard/admin` (admins). Mutations gated server-side. |
| Policy store | Cosmos vs Postgres | **Cosmos** (+ Redis for hot rate-limit counters + PDP cache) |
| Partition key | `/username` vs immutable id | **Immutable `/consumerId`**; `username`/`categories`/`type` are queryable fields |
| Manifest vs policy | — | **Manifest = capability (code-owned); Policy = authorization (store, admin-editable)** |
| Identity model | app+user vs app-only | **App-only.** Governance authenticates the *calling principal*; the *end customer* behind it is the app's responsibility |
| Credential | JWT + API key + IP | **Hashed API key = principal identity** (JWT/mTLS are later hardening; IP allowlist = defense-in-depth) |
| Access model | per-principal vs role | **Category templates (1 category = 1 data domain) + per-principal overrides**; a principal holds **a set of categories**, effective grant = **union** of them; resolved **live (dynamic), not snapshotted** |
| Default posture | — | **Deny-by-default** |
| Dashboard auth | Entra vs custom | **Custom login** (username+password), reusing the policy store; roles `user` / `admin` |
| Credentials per principal | — | **Two hashes:** dashboard login = **scrypt** (memory-hard, stdlib — chosen over argon2 to avoid a C build dep); MCP API key = **SHA-256 + pepper** (high-entropy key; a slow hash per-request would be needless latency) |
| Secret delivery | email link vs login | **Self-service reveal/rotate** after login (no email infra needed) |

---

## Guiding principle: data plane vs control plane

Stage 1 is the **data plane** (hot path: caller → gateway → backend → redacted result).
Everything here is the **control plane**: how policy is authored, stored, and observed.
They stay decoupled:

- The hot path stays deterministic and fast (no LLM, no blocking admin calls).
- **Policy is data the control plane writes and the data plane reads** — never a
  redeploy. This is the PAP → PDP split from `design_plan.md` §4 made real, and the
  single biggest structural change v2 introduces: entitlements, consumer keys, rate
  limits, and allowlists move out of code/env into Cosmos.

---

## Part B — Control plane

### B.1 Manifest vs Policy (the core distinction)

Two different things; only one is admin-editable.

- **Manifest = capability (code-owned, NOT in the dashboard).** Per backend: the tool
  catalog, whether each tool is account-scoped, and the **classification of every
  output field** (`PUBLIC/INTERNAL/PII/SENSITIVE`). This is `policy/manifest.py` today.
  It stays engineer-authored and code-reviewed, because mislabeling a field's
  sensitivity is a security bug, not a config toggle. The dashboard *reads* the
  manifest to populate the grant UI; admins do not retag field sensitivity from a form.
- **Policy = authorization (store, admin-editable).** *Who may use what.* Written by the
  dashboard. The PDP combines manifest (what's sensitive) + policy (who's allowed) to
  produce allow/deny + a redaction plan.

"Policy" therefore means: category templates + per-principal grants/overrides + rate
limits + whitelists. The manifest is capability, not policy.

### B.2 Data model (Cosmos)

```
categories             partition /id                    domain templates (1 category = 1 data domain)
  { id:"finance", displayName:"Finance",
    backend:"minierp_finance",
    tools:"*" | ["get_vendor_ap_invoices", …],   // that domain's tools (or all)
    levels:["PUBLIC","INTERNAL","SENSITIVE"] }    // level SET, not a ceiling
  { id:"accounts", displayName:"Accounts",
    backend:"minierp_accounts",
    tools:"*", levels:["PUBLIC","INTERNAL","PII"] }
  # …orders, shipments, …

consumers              partition /consumerId             principals (users + agents)
  { id:"c_8f2a", username:"users_Michele", type:"user",
    categories:["finance","accounts"],  // A SET — effective grant = union of these
    role:"user",                        // user | admin — drives dashboard access
    loginPasswordHash:"scrypt$…",       // dashboard login (humans only) — scrypt
    keyHash:"sha256:…",                 // MCP API key — SHA-256 (+ pepper), not argon2
    keyMeta:{ prefix:"gmk_8f2a…", createdAt:…, lastUsedAt:…, status:"active" },
    status:"active",                    // active | pending | disabled
    ipAllowlist:[…],                    // optional per-principal
    rateLimit:{ perHour:1000 },
    overrides:{ "<backend>":{ grantTools:[], denyTools:[], grantLevels:[], denyLevels:[] } } }

accessRequests         partition /consumerId             signup + access asks
  { id:"req-123", consumerId:"c_8f2a", kind:"account|access",
    categories:["finance"], backend:"minierp_finance", tools:["get_vendor_ap_invoices"],
    levels:["SENSITIVE"], justification:"…", status:"pending",
    createdAt:…, decidedBy:null, decidedAt:null }

config                 partition /id                    global IP/CIDR allowlist + hosts/origins (id="global")

audit                  partition /consumer               tool calls + policy changes; enable TTL
                                                         (durable replacement for audit.py's ring buffer)
```

Notes:
- **Immutable partition key.** Partition `consumers` by a generated `consumerId`, never
  by `username`, `categories`, or `keyHash` (a Cosmos partition key can't change; all of
  those can). The auth hot path resolves by API-key hash from an **in-memory cache** (the
  Cosmos store loads all consumers, short TTL, refresh on write) — so the partition key is
  for writes / admin point-reads, not per-request auth.
- **One registry, no dual-write.** The `consumers` container *is* the API-key list —
  issuing a key = writing `keyHash` on the consumer doc. No separate key list.
- **Admins are just `consumers` with `role:"admin"`** — no separate admins container.
- **category id == data domain == a backend.** Each category names one backend and grants
  its tools + a level set; a principal composes several categories.

### B.3 Category-based access + resolution

A **category** is a first-class template = **one data domain** (`accounts`, `finance`,
`orders`, `shipments`, …), granting that domain's tools + a data-classification **level
set**. A principal holds **a set of categories**; its effective policy is resolved **live**
(not snapshotted) from the union of those categories plus its own overrides — so editing a
category template instantly updates every principal that holds it.

Levels are an **allowed-SET, not a linear ceiling** — PII (personal data) and SENSITIVE
(financials) are orthogonal (finance sees financials but not contact PII; accounts the
reverse), so a scalar "max" can't express real categories.

**Effective-policy resolution (deterministic):**
1. Start from the **union** across the principal's categories: for each category, add its
   backend's granted tools and its level set.
2. Apply `overrides.grantTools` / `grantLevels` (admin-approved additions).
3. Apply `overrides.denyTools` / `denyLevels` — **explicit deny always wins**.
4. Effective levels (per backend) = `(⋃ category.levels ∪ overrides.grantLevels) −
   overrides.denyLevels` (set operations, not a min-ceiling).
5. Anything not granted after the above → **denied** (deny-by-default), enforced by the
   PDP as tool-level authz (`allows_tool`) plus level-set redaction.

The PDP resolves the principal + its categories into an `EffectiveGrant` and applies these
rules. A category-less principal (env-seeded Stage 1 consumers) falls back to
allow-all-tools + its explicit `allowed_levels` (pre-category behavior preserved); an
unknown category contributes nothing (fail closed).

Example: a principal with `["finance","accounts"]` can call finance-domain tools (with
`SENSITIVE` financials) **and** accounts-domain tools (with `PII` contacts/addresses),
because the effective grant is the union; a principal with only `["orders"]` sees order
status but no AR/AP financials and no contact PII.

**Implementation status: DONE.** `policy/categories.py` (4 seeded categories =
orders/accounts/shipments/finance, each mapping to one `minierp_*` backend with a tool
set + level set) + `policy/resolve.py` (union across `record.categories`, per-backend
overrides, legacy fallback, unknown-category fail-closed). `ConsumerRecord.categories:
list`; store `get_category`/`categories()`/`upsert_category`; `GOVERNANCE_CATEGORY_<NAME>`
seeding; `/admin/categories*` + catalog/signup/approve/my-access wired to categories;
dashboard UI updated. Verified: `_smoke/test_categories.py` (21 — union, deny-by-default,
per-category redaction, overrides, fail-closed, legacy) + all prior offline suites +
a live boot smoke (a `["orders","accounts"]` consumer reaches both backends; `finance`
is denied `not_granted`).

### B.4 Identity & credentials

- **App-only identity.** Governance authenticates the *calling principal* (agent or user)
  via its API key. The *end customer* behind an app call (e.g. which dental practice) is
  the **app's** responsibility to assert (`customer_id`) — governance does not verify each
  customer. This keeps governance from re-doing per-customer verification the app already
  does. **Residual risk:** a compromised app can read any customer its grants allow, and
  governance can't catch "wrong customer" because it never knew the right one. The
  compensating control is the anomaly signal **distinct `customer_id`s per principal over
  time** (see C.3) — build that counter early.
- **JWT vs API key.** Under app-only identity they're redundant. The **hashed API key is
  the principal identity**, sent over TLS. Optional hardening: exchange the key for a
  short-lived JWT (client-credentials) so a long-lived secret isn't sent per request. IP
  allowlist stays as defense-in-depth; mTLS later (see Part C).
- **Two credentials per principal, both hashed, never the same secret:**
  - `loginPasswordHash` — the human dashboard login (users only). **`argon2id`** — a
    slow password hash is right for a low-entropy human password.
  - `keyHash` — the MCP API key the tools send to the gateway. **SHA-256 + optional
    pepper** — the key is high-entropy random, so a fast hash is correct; argon2 on
    every MCP request would be pure latency for no security gain.
  Independent lifecycles: rotate the API key without touching the login.
- **`type:"user"` vs `type:"agent"`.** Users have a login + role + their own key. Agents
  have a key and **no login**, plus `owner` = the `consumerId` of the human who manages
  them (owner or admin rotates the agent's key).

### B.5 Account lifecycle

```
self-signup (dashboard):  username + password + requested category(ies) + use case
   → consumer doc { type:user, role:user, loginPasswordHash, status:"pending" }
   → can log in (sees "pending"); NO MCP key, NO grants yet   (deny-by-default)

admin review (/dashboard/admin):
   → approve with requested categories → status:active, categories set (union grant)
   → approve with custom overrides     → same + overrides
   → deny

on approve: generate MCP key → store keyHash + keyMeta (prefix only)
user logs in → "My Access" → reveal-once / rotate the key
```

The **login** credential is set by the user at signup (self-service); the **MCP key** is
issued by the system on approval. This is what removes the earlier email-delivery problem.

### B.6 Dashboard: placement, auth, RBAC

- **Same deployment, path-split.** `/dashboard` (any logged-in user) and `/dashboard/admin`
  (admins). Serving the admin HTML behind auth is cosmetic — **every mutation route
  verifies the admin session server-side** and returns 403 otherwise. Accepted trade of
  one process: the gateway now holds write credentials to the policy store (larger blast
  radius than a separate service).
- **Login for everyone.** Bringing login back lets us **scope monitoring by role**: a
  `user` sees only their own call history + their "My Access" page; an `admin` sees global
  monitoring + all control panels. This also removes the earlier concern about an
  anonymous dashboard leaking `customer_id`s/IPs.
- **Roles:** `user` (self-service: view own access, reveal/rotate key, request more) and
  `admin` (full control). Enforced server-side on the API, per route.
- **Session:** on login, verify password against `loginPasswordHash`, issue a short-lived
  (30–60 min) **signed, httpOnly, Secure cookie** carrying `consumerId` + `role`. The one
  secret in **Key Vault** is the **session-signing key**; passwords live only as hashes in
  Cosmos.
- **Brute-force protection:** rate-limit + lockout per account/IP on login; audit every
  attempt (success + failure) to `policy_audit`.
- **Least privilege on the store:** only the gateway/control-plane identity may read the
  `consumers` collection (it holds login + key hashes).

### B.7 Control panels

Each is a view over the store; admin edits write a new version + a `policy_audit` entry.

1. **Consumers & keys** — register/approve principals; rotate/revoke keys; set expiry.
2. **Categories** — edit category templates (per data domain: tools + level set).
3. **Access matrix** — per principal: assigned categories (a set) + overrides
   (grant/deny tools/levels). The "which MCP, which part" control.
4. **Rate limits** — per principal / tool / tier; burst + sustained.
5. **Whitelists** — IP/CIDR, allowed hosts/origins.
6. **Live monitor** — calls, denials, rate usage, active sessions (role-scoped).
7. **Access requests** — the approval queue (B.8).
8. **Audit & incidents** — durable call history + anomaly flags.

### B.8 Access requests

Two paths, one queue (`accountRequests`):
- **Denial-derived (start here):** every PDP deny for an ungranted tool/backend surfaces in
  the admin dashboard as a suggestion — "consumer X attempted `minierp.get_contacts` 5× —
  not granted. [Grant] [Dismiss]." One-click grant writes the override + `policy_audit`.
- **Explicit (later):** an app owner (or the app via its key) files a request with a
  justification. Approval writes grants and stamps `policy_audit`.

### B.9 How this maps to existing code

- `policy/decision.py::decide()` keeps its signature; its inputs (manifest + entitlements)
  become **store-backed** — a cached loader resolves `categories + overrides` (union) into
  the same allowed-levels/grants the PDP already consumes.
- `edge.py::_load_consumers()` / `_load_ip_allowlist()` read from the store (via cache)
  instead of env; key comparison becomes **hash verification**, not plaintext equality.
- New `control_plane` routes on the gateway app: login/session, `/dashboard/admin`, CRUD,
  approvals — isolated from the MCP hot path.
- New cached loader module (Cosmos + Redis) with short TTL + version-stamp invalidation;
  **fail closed** if the store/cache is unavailable.

---

## Part C — Security hardening

**Verdict:** the original "JWT + API key + IP allowlist" is a reasonable baseline, but with
app-only identity you don't need all three (JWT and API key would identify the same app).
The baseline is **necessary-not-sufficient** for customer PII/financials; IP allowlisting
is the weakest factor and must not be load-bearing.

### C.1 Prioritized gaps (baseline → production-robust)

1. **mTLS + private networking gateway↔backend** (ideally client↔gateway too). Biggest
   single upgrade; demotes IP allowlisting to nice-to-have.
2. **Secret hygiene.** Hash API keys (SHA-256 + pepper) instead of comparing **plaintext**
   env values. Add rotation, expiry, revocation (the Consumers panel).
   *(DONE — landed with the store, Part D step 1.)*
3. **Full JWT validation** (if adopted) — signature, `exp`/`nbf`, `aud`=gateway,
   `iss`=IdP, `jti`; rotate signing keys via JWKS.
4. **Per-tool/per-backend authorization as authz, not only redaction** — deny the *call*
   when a principal isn't granted a tool/backend at all (the access matrix, B.3).
5. **Replay protection** (if not on mTLS): HMAC-sign request body + timestamp + nonce;
   reject stale/replayed.
6. **Tamper-evident, off-box audit** — append-only + hash chain, shipped to Cosmos/Log
   Analytics. Replaces the in-memory ring buffer.
7. **Anomaly detection** feeding the off-hot-path governance agent (`design_plan.md` §9).

### C.2 Middleware signals to monitor

Cheap to emit from `edge.py` / `_govern`; the signals that catch abuse:
- Auth-failure rate per key and per IP (brute force / key-stuffing).
- **Distinct `customer_id`s per principal/session** — the strongest enumeration/scraping
  signal, and the compensating control for app-only identity (C / B.4).
- Denial-reason distribution and denial-rate spikes.
- Rate-limit-hit frequency; oversized/malformed request counts.
- New/unusual source IP or geo per principal; concurrent session count.
- Per-principal call volume vs a rolling baseline.
- Redaction frequency dropping suddenly (possible policy misconfig over-exposing data).
- Backend latency + error rate (availability + tamper probe).

### C.3 Target auth stack (end state)

1. **Network:** private VNet; backends internal-only; mTLS from gateway; gateway public
   behind Front Door/APIM (WAF + DDoS).
2. **Transport:** TLS everywhere; mTLS gateway↔backend.
3. **Principal identity:** hashed, rotatable API key (optionally exchanged for a
   short-lived JWT, `aud`=gateway).
4. **Request integrity:** HMAC signature + timestamp + nonce (replay protection).
5. **Authz:** PDP resolves categories + overrides (consumer × backend × tool × field),
   enforced server-side, fail-closed.
6. **Rate/quota:** distributed (Redis), multi-dimensional (per principal / IP / global),
   cost-aware.
7. **Audit:** tamper-evident, off-box, + anomaly detection → governance agent.

---

## Part A — Deployment (sequenced last)

Substrate decided (`design_plan.md` §11): gateway → **App Service**, backends → **Container
Apps** (internal ingress).

```
Internet ─▶ Front Door / APIM (TLS, WAF, DDoS) ─▶ Gateway (App Service, public)
                                                    │  mTLS, private VNet
                                                    ▼
                                    mcp-minierp (Container Apps, INTERNAL ingress)
                                                    ▼
                                    db-api.frontierdental.com
                                    + Key Vault (session key, secrets)
                                    + Cosmos (policy + audit) + Redis (counters + cache)
```

Work items: containerize both services (gateway image includes `governance_core/`);
secrets → Key Vault + managed identity; backend internal-only; Front Door/APIM at the edge;
provision Cosmos + Redis. Deliverables: `Dockerfile` ×2, Bicep/`az` script, deploy runbook.

---

## Part D — Build sequence

Each step ships independently and leaves the system working.

1. **Policy store + cached read path** — ✅ **DONE.** `governance_core/store/`
   (`PolicyStore` interface + `LocalPolicyStore` seeded from env; Cosmos backend selected
   by `get_store()` later). `edge.py` authenticates via the store with SHA-256 hashed keys
   + per-principal rate limit/IP; the resolved `ConsumerRecord` is stashed in
   `request_context.consumer_record_ctx`; `decision.decide()` takes the principal's levels
   from it (falling back to `entitlements`). Verified: `_smoke/test_store.py` (12) +
   unchanged `_smoke/test_policy.py` (22) + full E2E (12).
2. **Department model + dynamic resolution** — ✅ **DONE.** `policy/departments.py`
   (drafted templates) + `policy/resolve.py` (live resolution, 5 precedence rules,
   deptless legacy fallback). PDP `decide()` now enforces **tool-level authz**
   (deny-by-default, reason `not_granted`) and takes redaction levels from the resolved
   grant; gateway computes the grant from the request's `ConsumerRecord` + department.
   `LocalPolicyStore` assigns departments via `GOVERNANCE_DEPT_<NAME>`. Verified:
   `_smoke/test_departments.py` (20) + unchanged `test_store`/`test_policy` + E2E now 16
   (with a `customer_service`-scoped consumer: denied the spend tool, total redacted,
   contact PII visible). *(C.1 #4 also DONE.)*
3. **Dashboard auth + RBAC** — ✅ **DONE.** `governance_core/auth/`: `passwords.py`
   (**scrypt**, stdlib — swapped from argon2 to avoid a C build dep), `session.py`
   (HMAC-signed cookie token, expiry, fail-closed without `GOVERNANCE_SESSION_SECRET`),
   `lockout.py` (brute-force limiter). Gateway routes: `/dashboard/login` `/logout` `/me`,
   session-guarded `/dashboard` (+ admin-only `/dashboard/admin`), role-scoped `/admin/*`
   (viewer sees only its own calls; `/admin/sessions` + `/admin/rate-limits` admin-only).
   `EdgeMiddleware` now gates **only `/mcp`** (API keys); humans use the session cookie.
   Store: `ConsumerRecord.login_password_hash` + `get_by_username`; dashboard admin/viewer
   seeded from `GOVERNANCE_ADMIN_*` / `GOVERNANCE_VIEWER_*`. Verified: `test_auth.py` (16)
   + `auth_http.py` (18, live: login/RBAC/lockout/logout, `/mcp` still API-key-gated) +
   all prior suites + E2E (16) unchanged.
4. **Control panels** — ✅ **DONE.** Writable persistent store `store/file_store.py`
   (`FilePolicyStore`, JSON-persisted, seeded from env+code on first run; selected by
   `GOVERNANCE_STORE_FILE`; Cosmos backend will mirror the same write surface). Admin CRUD
   API on the gateway (admin-only, audited via `audit.log_policy_change`): consumers
   (list/create+key-once/rotate/patch/delete), departments (list/upsert/delete), whitelist
   (get/set, wired into the edge IP check), catalog, policy-changes. Admin UI at
   `/dashboard/admin` (`gateway/static/admin.html`): Consumers, Departments, Whitelist,
   Monitor tabs. Verified: `test_file_store.py` (14, CRUD+persistence+roundtrip) +
   `admin_http.py` (14 live — incl. a dashboard-created consumer immediately usable through
   `/mcp` with its department grant; viewer forbidden from writes) + all prior suites green.
5. **Account lifecycle + access requests** — ✅ **DONE.** Public `/dashboard/signup`
   (+ page) creates a `pending` account + an account request; pending users can log in
   (see status) but have no grants/key. Admin `/admin/requests` + approve/deny (account →
   active + department; access → merge grant into overrides). Self-service `My Access`
   page (`/dashboard` → `account.html`): status, resolved access, **self-mint/rotate key**
   (shown once — this replaces email delivery), and request-more-access. Denial-derived
   `/admin/access-suggestions` (aggregates `not_granted` denials). Requests persisted in
   the store (`access_requests`). Verified: `lifecycle_http.py` (17 — signup→pending→
   approve→mint key→use via `/mcp`→denied tool becomes a suggestion→request→approve→grant
   widens→deny path) + regressions (`admin_http` 14, `e2e` 16) green.
6. **Hardening** — mTLS + private net; tamper-evident audit; anomaly signals; optional JWT +
   replay protection (Part C).
7. **Deploy** (Part A).

---

## Open decisions

- **API-key → JWT exchange:** adopt short-lived JWTs now, or ship with hashed API keys over
  TLS and add JWT in step 6? (Recommend: hashed keys now, JWT in hardening.)
- **callAudit sink:** Cosmos vs Log Analytics for the durable audit trail (Redis/Cosmos for
  policy is settled).
