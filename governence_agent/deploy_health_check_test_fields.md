# Deployment Health-Check Script — Test Fields Plan

**Status:** Proposed (planning only — nothing here is built yet).
**Date:** 2026-08-26
**Owner:** Platform / Governance
**Scope:** a single, runnable-from-a-local-machine script that walks the entire
governed pipeline against the **deployed** gateway (not local dev processes)
and reports PASS/FAIL, so a human can confirm "the deploy is healthy" in one
run instead of poking the dashboard by hand.
**Builds on:** existing `_smoke/` precedent — `test_winback_radar_deployed.py`
already proves the pattern works (real Azure URL, real API key, real ERP data,
PDF artifact round-trip, email draft) but is scenario-shaped (a win-back
report), not a systematic sweep. `auth_http.py`/`admin_http.py`/`lifecycle_http.py`
cover dashboard HTTP auth/RBAC locally but aren't pointed at the deployed URL.
This plan turns "one good example" into "one script that covers every layer."

---

## 1. Goal / non-goals

**Goal:** one script, run locally, exit code 0/1, that exercises every layer
of the pipeline against the live deployment and prints a clear pass/fail
summary — a post-deploy confidence check, runnable in a couple minutes.

**Non-goals:**
- Not replacing the existing ~85-file `_smoke/` suite — that stays as
  dev-time/pre-deploy regression coverage against local mocks/processes.
  This is a thin, fast, *live* check, not exhaustive testing.
- Not CI-wired for now — the ask is a local script; can be promoted to a
  scheduled job later if it proves useful.
- Not a load/perf test — correctness and reachability, not throughput.

---

## 2. Pipeline layers to cover

Mapped to the real modules so the eventual script has one section per layer,
not ad-hoc coverage:

| # | Layer | Module(s) | What "healthy" means |
|---|---|---|---|
| 1 | Edge / auth | `edge.py` | `/mcp` rejects no/bad API key; dashboard rejects no session; rate-limit headers present |
| 2 | MCP transport & federation | `app.py`, `mcp_clients.py` | session initializes; `tools/list` returns the full expected namespaced tool set across all 6 backends |
| 3 | Policy / PDP | `govern.py`, `policy/decision.py`, `policy/manifest.py` | a granted call succeeds; an ungranted call denies with the right `errorCode`; redaction by classification tier is visible in the payload |
| 4 | Scope resolution | `scope_store.py` | `customer_id` supplied once is remembered for later calls in the same `session_id` |
| 5 | Backend federation health | `mcp_clients.py` → each backend | minierp (orders/accounts/finance/shipments/analytics), office, email, calendar, knowledge, code each reachable and returning real data, not a transport error |
| 6 | Artifact pipeline | `backend/artifacts.py`, `artifact_store.py` | create (PDF/XLSX/DOCX/PPTX) → `downloadUrl` → actual download → content sanity (reuse the `pdf_text()` extraction trick from `test_winback_radar_deployed.py`) |
| 7 | Audit trail | `audit.py`, `/admin/calls` | a call made by this script shows up in the audit log shortly after |
| 8 | Workflow graph plane | `backend/workflow_graphs.py`, `workflow_graph_store.py` | create draft → list → read back → validation rejects a deliberately broken graph |
| 9 | Knowledge base | `backend/knowledge.py`, `knowledge_store.py`, `personal_knowledge_store.py` | company search/answer returns citations; personal-tier upload → search → **delete** (isolation + self-cleanup) |
| 10 | Chat orchestrator / LLM broker | `orchestrator.py`, `llm_broker.py` | a live chat turn reaches the LLM (sglang/Qwen or Azure) and produces at least one real tool call — **not covered by any existing deployed test found**, and the one path most likely to silently break (model/server config drift) without tripping any of the MCP-only checks |
| 11 | Dashboard HTTP surface | `backend/pages.py`, `admin_security.py`, `session.py` | login, role-based route access (admin vs viewer), logout invalidates session |
| 12 | Static/frontend serving | — | `/` and `/dashboard` load without a 500, basic asset present |

Layer 10 is the gap I'd flag hardest: everything else fails loudly (HTTP
status, `isError`, exception). A broken LLM endpoint fails *quietly* — the
gateway itself stays "up," only chat degrades — so it's the check most worth
having that doesn't exist yet.

---

## 3. Read-only vs. write-path checks — explicit split, because this hits PRODUCTION

This script talks to the real deployment: real ERP data, real Cosmos/audit
records, real generated files. Not everything should run by default.

**Tier 1 — always run, side-effect-free:**
`tools/list`, backend `ping`, read-only data calls (`find_customer`,
`get_customer_orders`, `get_customer_order_summary`, etc.), dashboard
login/RBAC checks, `GET /admin/calls`, `GET /admin/rate-limits`.

**Tier 2 — opt-in flag, creates real records:**
`office_create_*` + download, `email_create_email_draft` (never sent, so
low-risk), `propose_graph` draft creation, `knowledge_ingest_my_document`.
This is what `test_winback_radar_deployed.py` already does today — but it
leaves the PDF/email-draft/workflow-draft behind with no cleanup. The new
script should clean up everything it creates where a delete path exists
(`knowledge_delete_my_document` exists; confirm whether an artifact-delete or
workflow-graph-delete tool exists before deciding whether PDFs/graphs are
"accept the residue" or "must clean up" — open question, §6).

---

## 4. Script shape recommendation

- One file, e.g. `_smoke/deploy_health_check.py`, same `check()`/PASS/FAIL
  harness convention as the rest of `_smoke/` (see `e2e_client.py`,
  `auth_http.py`) so it reads like the existing suite, not a new pattern.
- MCP layer via `streamablehttp_client` + `ClientSession`, same as
  `test_winback_radar_deployed.py`; HTTP/dashboard layer via `httpx`, same as
  `auth_http.py`.
- Target URL + API key(s) taken from env vars / CLI args (`--url`,
  `--api-key`, or `GATEWAY_URL`/`GATEWAY_API_KEY`), **not hardcoded** the way
  `test_winback_radar_deployed.py` currently hardcodes the prod URL and reads
  a key straight from `mcp-knowledge/.env.local` — worth fixing so the same
  script can point at prod or a future staging slot without editing the file.
- `--tier2` (or similar) flag gates the write-path checks in §3.
- Exit 0 only if every Tier-1 check passes (Tier 2 failures can be reported
  but configurable on whether they fail the run — open question, §6).

---

## 5. Concrete check list (the actual "test fields")

One row per assertion the script should make — this is what gets turned into
`check(...)` calls in the eventual script.

| Layer | Check | Tool / endpoint | Pass condition |
|---|---|---|---|
| Edge | reject missing key | `POST /mcp` no auth | 401/400 |
| Edge | reject bad key | `POST /mcp` bad Bearer | 401 |
| MCP | session initializes | `session.initialize()` | no exception |
| MCP | full tool federation | `tools/list` | every expected `<backend>_<canonical>` name present, count matches manifest |
| PDP | granted call succeeds | e.g. `minierp_accounts_find_customer` | `status` not `denied` |
| PDP | ungranted call denies | a tool outside the test consumer's grant | `status == "denied"`, correct `errorCode` |
| PDP | classification redaction | a SENSITIVE field (e.g. order total) with a lower-tier key | field absent/masked |
| Scope | customer_id memory | 2 calls, same `session_id`, 2nd omits `customer_id` | 2nd call still resolves the account |
| Backend: minierp orders | real data returned | `get_customer_orders` / `get_customer_order_summary` | `status: success`/`ok`, non-empty for a known test account |
| Backend: minierp accounts | real data returned | `find_customer`, `get_customer_profile` | resolves a known test customer |
| Backend: minierp finance | real data returned | `get_vendor_details` or `get_ap_invoices_due_soon` | no transport error |
| Backend: minierp shipments | real data returned | `get_customer_shipment_status` | no transport error |
| Backend: minierp analytics | real data returned | `get_top_customers_by_spend` | no transport error, entitlement respected |
| Backend: office | artifact round-trip | `office_create_pdf_packet` → download | 200, content contains expected string |
| Backend: email | draft only, never sent | `email_create_email_draft` | `draftId` present, no `sendId` |
| Backend: calendar | draft only | `calendar_draft_calendar_invite` | `draftId` present |
| Backend: knowledge (company) | search + answer | `search_knowledge`, `answer_from_knowledge` | non-empty result with citation |
| Backend: knowledge (personal) | isolation + cleanup | `ingest_my_document` → `search_my_documents` → `delete_my_document` | uploaded doc found, then gone after delete |
| Backend: code | plan-only, no changes | `code_opencode_plan_change` | returns a plan, `status` success |
| Audit | call appears in trail | `GET /admin/calls` after a known call | this run's call visible (by session_id or timestamp) |
| Workflow graph | draft lifecycle | `propose_graph` → `list_my_workflows` → `get_my_workflow` | round-trips correctly; a deliberately invalid graph (e.g. cycle) is rejected with `status: error` |
| Chat/LLM | live tool-calling turn | orchestrator chat endpoint, a question that requires a tool | response references real tool output, not a canned/error string |
| Dashboard | login + RBAC | `auth_http.py` pattern against deployed URL | admin sees `/admin/*`, viewer gets 403 on admin-only routes |
| Dashboard | logout | `POST /dashboard/logout` | subsequent protected call 401 |
| Frontend | static serving | `GET /` and `GET /dashboard` | 200, no 500 |

---

## 6. Decisions (confirmed 2026-08-26)

1. **Tier 2 (write-path) checks** — **opt-in flag** (`--tier2`). Default run
   is read-only/side-effect-free; write-path checks (PDF/draft/workflow-graph
   creation) only run when explicitly requested.
2. **Target URL** — **required `--url` argument**, no hardcoded default. Makes
   the script reusable against prod or a future staging slot, and prevents an
   accidental prod run from a bare invocation.
3. **Test identity** — the **local admin key already in `mcp-knowledge/.env.local`**
   (the same one `test_winback_radar_deployed.py` reads today) — confirmed
   as the dedicated key for this testing purpose. Script reads it from that
   file by default, overridable via `--api-key`/env var for a future
   non-admin healthcheck consumer if one gets added later.

Remaining lower-stakes items, resolved with defaults rather than blocking
further on them — revisit if the eventual script's behavior surprises you:

4. **Cleanup** — Tier-2 checks clean up what they create wherever a delete
   path exists today (`knowledge_delete_my_document` for the personal-KB
   check). Where no delete tool exists (office artifacts, workflow graph
   drafts), the script leaves them and labels them clearly (e.g. filename/title
   prefixed `[healthcheck]`) rather than blocking on building new delete
   tooling just for this script.
5. **Test data discovery** — script discovers a live customer at runtime the
   same way `test_winback_radar_deployed.py` does (`get_customers_by_region`
   probing), rather than depending on a hardcoded id that could stop existing.
6. **Failure policy** — Tier-1 failures fail the run (exit 1). Tier-2 failures
   are reported in the summary but don't flip exit code by default, since
   Tier-2 isn't part of a default run anyway — only relevant when `--tier2`
   is passed, and a broken PDF export shouldn't be conflated with "the core
   deployment is unhealthy."

---

## 7. Next step

Plan confirmed — generate `_smoke/deploy_health_check.py` implementing §5's
check list, following the shape in §4 and the decisions in §6.
