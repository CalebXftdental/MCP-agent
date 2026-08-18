# Personal Knowledge Base — Design

**Status:** Implemented in code (2026-08-17) — see §3 for the file list and the
"Revision history" section at the bottom for what changed along the way and why.
Not yet fully live: the Document Intelligence resource still needs provisioning,
env vars need setting on the `Governence-agent` App Service, and two category/
department edits need a manual admin-UI step to reach the already-populated
production Cosmos store (see §2 and the last revision-history entry).
**Scope:** Add a per-user personal knowledge tier (PDF/doc upload) to the Knowledge
panel, isolated from the existing shared company knowledge base, without disturbing
that existing read-only path.

---

> **Read this before implementing anything below.** This is a best-effort design
> from when it was written, not a final spec — and docs in this repo go stale fast
> in both directions (see `finalize_stage_1.md`'s intro for two examples that did).
> Before starting implementation on any item here: (1) re-verify the relevant claim
> against the live code, don't trust the doc's description of current state; (2)
> ask the user clarifying questions about anything with a real tradeoff, a
> security/privacy implication, or an external dependency, rather than silently
> proceeding with whatever this doc currently says; (3) only then start writing code.

## 0. Decisions locked

| # | Decision | Choice |
|---|---|---|
| Personal doc storage | new DB tech (Postgres/pgvector) vs. reuse existing store pattern | **Cosmos DB**, new containers partitioned by `owner`/`consumerId` — same shape as `store/cosmos.py`'s existing `chatSessions`/`accessRequests` containers. No new infra class to run/operate. |
| Admin visibility into personal docs | admin "All principals" bypass extends to personal tier vs. not | **No admin access at all**, including the existing "All principals" toggle. Personal KB is private to its owner, full stop — **at the app layer**. This is a UX/policy guarantee, not an infra one: whoever holds the Cosmos connection string (DBA/Azure portal access) can still read raw container data regardless of any in-app bypass toggle. Revisit the app-layer stance only if a real compliance/legal-hold need arises later; don't oversell it as stronger than it is. |
| Personal-doc search quality at launch | ship existing TF-IDF vs. build embeddings now | **Real embeddings from day one, via a hosted API — not self-hosted.** See §0.1: Azure OpenAI `text-embedding-3-large`, not Qwen3-Embedding. |
| Vector search / scoring | Cosmos-native vector index vs. in-app scoring | **In-app cosine similarity** over each owner's chunk partition. No change to the production `governance-agent-db` Cosmos account's capabilities (confirmed today it only has `EnableServerless`, not `EnableNoSQLVectorSearch`). Personal-KB corpora are per-owner and small (dozens–hundreds of chunks), so scoring in Python is not a latency concern — the embedding-model call dominates either way. Revisit only if a user's corpus grows large enough that pulling a full partition per query becomes a real RU/latency cost. |
| PDF text extraction (all PDFs, not just scanned ones) | local library (pypdf/pdfplumber/PyMuPDF) with OCR fallback vs. one hosted path for everything | **Azure AI Document Intelligence for every PDF**, prebuilt-read model, via a **new dedicated resource** (see §0.1) — no local PDF-parsing library at all. Revised after comparing quality, not just cost: DI's ML-based layout model handles multi-column text, tables, and broken font/encoding maps more reliably than any of the three local-library candidates (this org's own `AraTest/IngestionPipeline` already relies on DI for tables for the same reason), and it's the only option of the four that also handles scanned/image-only PDFs — so one path covers both cases instead of a local-extract-then-OCR-fallback split. Also sidesteps PyMuPDF's AGPL-license question entirely, since no local library is introduced. Tradeoff accepted knowingly: every personal PDF's content now goes to Azure (not just scanned ones), and ingestion has a hard dependency on DI being reachable. $1.50/1000 pages — trivial at this org's size and stated low volume. |
| Tool surface | one merged search tool vs. separate tools per tier | **Separate MCP tools** — `search_knowledge`/`answer_from_knowledge` (company, unchanged) vs. new `search_my_documents`/`answer_from_my_documents`/`ingest_my_document`/`list_my_documents`/`delete_my_document` (personal). Matches the existing narrow-tool-per-backend philosophy; keeps audit trail and LLM reasoning provenance-visible. |
| Owner identity source | trust caller-supplied `owner` vs. derive server-side only | **Server-derived only.** Confirmed mechanism (not just described): `gateway/app.py`'s tool wrappers (e.g. `knowledge_search_knowledge`, `calendar_list_upcoming_meetings`, `app.py:477-506`) never accept `owner` as a parameter at all — it's hardcoded from `ctx.consumer_ctx.get()` (= `ConsumerRecord.name`, confirmed immutable — see §2) before calling `_govern`. New personal tools' wrappers must copy this exact shape. The raw per-backend MCP process (`mcp-knowledge`) does take `owner` as a literal arg, but is bound to localhost with the gateway as its sole client (`DEPLOY.md`), so that's not an exposed surface. |
| Approval gate on ingest/delete | same `write`-tier approval as other write tools vs. exempt | **No approval gate — immediate.** A user approving their own upload/delete of their own private document is friction with no security benefit. Risk tier stays `write` for audit-trail purposes; `approval_required` stays `False`. |
| Azure resource ownership for embeddings/OCR | reuse AraTest's existing shared resources vs. provision new ones | **New, dedicated resources**, owned by this app. See §0.1 — reusing AraTest's resources would couple this app to a different product's infra with no visibility into its quota/rotation/ownership. **Embeddings specifically were then revised again** (§0.1's "Embedding resource, revised a second time" note) to reuse an existing resource after all — a deliberate exception to this row, not a reversal of the reasoning. |

### 0.1 Embedding + OCR hosting (revised from the original "self-host Qwen3-Embedding" plan)

The original plan pulled Qwen3-Embedding forward from `STAGE2_PLAN.md` §6 on the
assumption it was "already in progress" shared infrastructure. Checked against the
live code: it isn't — no embedding-serving code exists anywhere in this repo today,
only the self-hosted **chat** model (`GOVERNANCE_CHAT_BASE_URL` → Qwen/sglang,
`DEPLOY.md`). Standing up a new self-hosted embedding model (and, for OCR, a new
self-hosted vision/OCR model) is a materially bigger lift than the original doc
implied, for a feature explicitly expected to be low-volume.

Checking `AraTest/backend_governence/real_backend/rag/` and `AraTest/IngestionPipeline/`
(the actual production company-KB ingestion pipeline) showed the org already solves
this with **hosted, pay-per-call Azure APIs** — no idle infra cost, code pattern
already proven:
- Embeddings: Azure OpenAI `text-embedding-3-large` (`IngestionPipeline/utils/openai_utils.py`'s `get_embeddings_large()`), **$0.13 / 1M tokens**.
- PDF text extraction (tables today, all PDF text as of this revision): Azure AI Document Intelligence (`utils/docintelligence_utils.py`), **$1.50 / 1000 pages** for the prebuilt-read model.

At personal-KB's expected volume ("we do not use embed or OCR that much"), hosted
pay-per-call is both cheaper and dramatically less build/ops effort than self-hosting
— no GPU sizing, no new serving process, no new dependency to keep healthy.

**PDF extraction specifically was revised a second time, after the license question
below surfaced a deeper "why do we need a local library at all" question.** A local
PDF-parsing library (pypdf/pdfplumber/PyMuPDF) only reads a PDF's own embedded text
layer — none of the three handle scanned/image-only pages, which was the original
reason Document Intelligence was in this design at all (as an OCR fallback). But DI's
prebuilt-read model extracts text from *any* PDF, text-layer or scanned, generally
more reliably than the local libraries for multi-column layout, tables, and broken
font/encoding maps, since it's a trained layout-understanding model rather than a
positional-glyph heuristic. Given that, running every PDF through DI and dropping the
local library entirely is simpler (one path instead of a local-extract/OCR-fallback
split), gets better extraction quality, and avoids the local-library licensing
question altogether. See the "PDF text extraction" row in §0's table for the accepted
tradeoffs (every PDF now leaves the app to Azure, not just scanned ones; ingestion
gains a hard dependency on DI's availability).

**But do not reuse AraTest's existing resources** (`AzureOpenAIService-LanguageModels`,
`agenticai-documentintelligence`, both in `RG_Agentic_AI`). Confirmed via `az cli`:
neither is referenced anywhere in this repo (`governance_agent`/`mcp-agent`) today —
they belong to a *different* product (`AraTest`/`backend_governence`'s "chatbot"
family — note the sibling `chatbot-production-db`/`us-chatbot-db` Cosmos accounts).
Reusing them would mean sharing quota, key rotation, and billing visibility with a
team/product this app has no relationship to. Since these resource types have **no
idle cost** — you only pay for actual calls — there is no cost reason to share, only
a coordination-avoidance reason not to.

**Decision: provision two new, dedicated resources in `RG_IVA_Prod`** (same resource
group as `governance-agent-db`, so this app owns every resource it depends on, same
pattern as its dedicated Cosmos account and dedicated chat-model VM):
- A new Azure OpenAI resource (e.g. `governance-agent-openai`) with a
  `text-embedding-3-large` deployment.
- A new Document Intelligence resource (e.g. `governance-agent-docintel`), S0
  (Standard) tier — F0 (free) caps at 500 pages/month AND only reads the first 2
  pages of any document, unusable for real multi-page uploads. S0 has no monthly
  base fee either; billing is still pure pay-per-page ($1.50/1000 pages) either way.
- Region: Canada Central, matching `governance-agent-db`, for latency/residency
  consistency -- confirmed `text-embedding-3-large` is actually offered there via
  the live Foundry model catalog (`az cognitiveservices model list --location
  canadacentral`), not assumed.
- A plain dedicated Cognitive Services resource each, **not** a full Azure AI Foundry
  hub+project — that construct is for teams doing agent-building/prompt-flow work in
  the Foundry portal; this only needs a segregated endpoint + key.
- Keys held only by the gateway backend's own env config, never shared with AraTest.

**Embedding resource, revised a second time: reused `general-usage-resource`
instead.** `RG_IVA_Prod` already had an existing multi-purpose AIServices resource
(`general-usage-resource`, eastus2) with a `text-embedding-3-large` deployment
already live on it — found while wiring up the actual endpoint, not part of the
original plan. Checked what else is on it first: `gpt-5.4`, `gpt-5.4-mini`,
`gpt-image-2`, `mistral-small-2503` — a real shared pool other things already call.
Given that, explicitly chose to reuse it anyway rather than provision a fresh
resource just for this, purely for the convenience of the deployment already
existing. This is a deliberate, known exception to the "new dedicated resource"
row above, not a reversal of that reasoning — accepted tradeoff: personal-KB
embedding calls now share quota/rate-limits with whatever else calls this
resource, and a future key rotation on it affects more than just this feature.
Revisit with a dedicated resource if that shared blast radius ever becomes a real
problem (e.g. embedding calls get rate-limited by unrelated traffic).
- Endpoint: use the **"Azure OpenAI Legacy API"** address from the resource's
  endpoint list (`https://general-usage-resource.openai.azure.com/`), NOT the
  newer unified `.services.ai.azure.com` one shown alongside it -- the `openai`
  Python SDK's `AzureOpenAI` client (what `embeddings_client.py` uses) expects the
  classic per-service hostname, not the Foundry unified inference endpoint.
- Document Intelligence still gets its own new, dedicated resource as planned
  above -- this exception applies to the embedding resource only.
- `embeddings_client.py`/`docintel_client.py` both `.rstrip("/")` whatever
  endpoint is configured, defensively -- the `openai` SDK has a known bug
  (openai-python#1894) where a trailing slash produces a double-slash URL and a
  404, and every endpoint the Azure portal displays includes one.

**Explicitly decided, not overlooked:**
- **Network ACLs on the new resources are left at default** (`publicNetworkAccess: Enabled`,
  no IP/VNet restriction) for now — API-key-only protection, consistent with how
  AraTest's equivalent resources already operate today. Because these are brand-new,
  dedicated-to-this-app resources, tightening this later (IP-allowlist the App
  Service's outbound IP, matching `governance-agent-db`'s existing pattern) needs no
  cross-team coordination if revisited.
- **Microsoft's Modified Abuse Monitoring was considered and declined for now** —
  the default ~30-day abuse-monitoring retention window is accepted, consistent with
  how company-KB content is already handled via the equivalent AraTest resources.
- **Embedding-call-failure behavior:** degrade gracefully to the existing local
  TF-IDF scoring (`knowledge_store.search`'s current algorithm) for that request if
  the embedding API call fails/times out, rather than hard-failing the search —
  consistent with company-tier `search_knowledge`'s own direct/proxy/local fallback
  chain (`mcp-knowledge/app.py`). Flagged here as the sane default; revisit if you'd
  rather fail loudly instead.

---

## 1. What's already there (discovered, not built new)

`governance_core/knowledge_store.py` is already a full per-owner document store:
`ingest_document()`, chunking, text extraction, and `search()`/`answer()` that filter
by `owner`. It has just never been wired to an ingest route — `policy/manifest.py`
has a standing comment: *"Deliberately no ingest_knowledge_text/ingest_knowledge_file
entries: the knowledge backend is read-only, mirroring AraTestEnvBE's existing Azure
Blob/AI Search knowledge base."* `KnowledgePage.tsx` even had an ingest form once and
dropped it because the route 405'd — there was no ingest path to hit.

So the split between "general company KB" (owned by AraTestEnvBE's external
pipeline, mirrored via Azure AI Search) and a personal, app-owned store already
exists conceptually in this codebase. It was simply never turned on because there
was no product need yet.

**Known gap to fix regardless of everything else:** PDF "extraction" today is
`payload.decode("latin-1", "ignore")` on the raw bytes — not real PDF parsing. On an
actual PDF (compressed streams, binary structure) this produces garbage. Needs
routing PDF bytes through Azure AI Document Intelligence before personal PDF upload
can work at all (see §0.1 for why a hosted call replaces a local library entirely
here, unlike the other formats `extract_text()` already handles locally).

The store-backend-selection pattern this design reuses already exists too:
`store/__init__.py`'s `get_store()` picks `LocalPolicyStore` → `FilePolicyStore` →
`CosmosPolicyStore` by env var, all behind one `PolicyStore` interface, and
`store/cosmos.py` already partitions per-user containers (`chatSessions`,
`accessRequests`) by `consumerId`. Personal knowledge storage should be the same
shape, not a new pattern.

---

## 2. Architecture

Two isolated tiers, not one merged store:

| | Company KB (existing) | Personal KB (new) |
|---|---|---|
| Ownership | External (AraTestEnvBE's pipeline) | This app owns it fully |
| Storage | Azure AI Search (shared index) | Cosmos DB, new containers, partitioned by owner |
| Write path | None from this app (by design) | Ingest-only-your-own, via new tools |
| MCP tools | `search_knowledge` / `answer_from_knowledge` (unchanged) | `search_my_documents` / `answer_from_my_documents` / `ingest_my_document` / `list_my_documents` / `delete_my_document` |
| Visibility | Everyone entitled to the "knowledge" category | Owner only — no admin bypass (app-layer; see §0) |
| Retrieval | Azure AI Search hybrid/semantic search | Azure OpenAI `text-embedding-3-large` + in-app cosine similarity over Cosmos-stored chunks (§0.1) |

### Isolation

- Every read/write path derives `owner` from server-side session claims only —
  never a client- or MCP-caller-supplied argument (mechanism confirmed in §0's
  "Owner identity source" row).
- `owner` = `ConsumerRecord.name`. Confirmed immutable: the admin consumer-edit
  endpoint's field whitelist (`gateway/backend/admin_policy.py:206`) allows editing
  `full_name` (display name) but not `name`/`consumer_id` — so this is safe to use
  as a durable Cosmos partition key.
- **New gap this surfaced, in scope for this build:** `delete_consumer`
  (`admin_policy.py:200`) does not cascade into personal-KB documents. If a `name`
  is ever reused for a new consumer after the old one is deleted, the new person
  would silently inherit the old owner's private documents. Add a cascade-delete of
  that owner's personal-KB documents (and chunks) on `delete_consumer`.
- Cosmos partition key = `owner`/`consumerId`, matching `chatSessions`/
  `accessRequests`. A point read/query needs the partition key, so cross-tenant
  reads require an actual bug in the partition-key derivation, not just a missed
  `WHERE` clause — stronger isolation than a single shared table with an
  application-level filter.
- No admin bypass anywhere in the personal-tier path — not even the existing
  "All principals" toggle used for company-tier document listing (app-layer
  guarantee only; see §0).

### RAG pipeline: shared extraction/chunking, hosted embeddings, split storage

Use one extraction/chunking implementation for both tiers — don't fork two RAG
stacks. PDF text extraction comes from the new dedicated Document Intelligence
resource and embeddings from the new dedicated Azure OpenAI resource (both §0.1);
vector store + access-control layer are what differ between tiers.

### Chat behavior

Both tool sets are exposed to the LLM/orchestrator with distinct descriptions.
System prompt instructs the model that it's fine — encouraged, even — to call both
when relevant, and to label citations by source ("Company KB" vs. "My documents")
in its answer, so provenance stays visible to the user and in the audit trail.

---

## 3. New / changed pieces

- **Fix PDF extraction** in `knowledge_store.py` — route PDF bytes through the new
  Document Intelligence resource's prebuilt-read model (no local PDF-parsing
  library) — blocking for personal PDF upload to work at all, independent of
  everything else here. Other formats `extract_text()` already handles
  (docx/pptx/xlsx/txt/md/csv) are unaffected and stay local.
- **New Azure resources** (§0.1): dedicated Azure OpenAI resource
  (`text-embedding-3-large` deployment) + dedicated Document Intelligence resource,
  both in `RG_IVA_Prod`, own keys in gateway env config.
- **New MCP tools** in `mcp-knowledge`: `ingest_my_document`, `search_my_documents`,
  `answer_from_my_documents`, `list_my_documents`, `delete_my_document` — new
  `policy/manifest.py` entries with an explicit `write` risk tier (no approval
  gate — see §0) for ingest/delete (per the Stage 2 "no tool registers without a
  risk tier" rule).
- **New Cosmos containers** in `store/cosmos.py`: personal knowledge documents +
  chunks (with embedding vectors stored inline, scored in-app), partitioned by
  `owner`.
- **New gateway routes** in `gateway/backend/knowledge.py` for personal upload /
  list / delete, deriving `owner` from session the same way existing routes do —
  and the same hardcoded-in-the-wrapper pattern for the chat/orchestrator tool path
  (`gateway/app.py`).
- **Cascade-delete** personal-KB documents when `delete_consumer` runs (new gap
  found in §2, not in the original design).
- **Frontend**: a "My documents" section in `KnowledgePage.tsx` with a real upload
  control (the ingest form dropped earlier, now backed by a route that isn't a
  405), kept structurally separate from "Indexed documents" (company).
- **Orchestrator / system prompt**: describe both tool sets, instruct
  source-labeled citations.
- **Audit**: `audit.log_policy_change` entries for `ingest_personal_knowledge` /
  `delete_personal_knowledge`, matching the existing action-logging pattern.

---

## 4. Open items before/during implementation

- Per-document size/type limits for personal upload (PDF plus whatever else
  `extract_text()` already supports — docx/pptx/xlsx/txt/md/csv), and a per-owner
  total-storage or total-document-count cap. Not yet decided — needs concrete
  numbers before ingest ships (affects Cosmos item-size limits, RU cost, and
  embedding-API cost exposure to a single runaway upload).
- Exact naming/provisioning of the two new Azure resources (`governance-agent-openai`,
  `governance-agent-docintel` are placeholder names in §0.1) — confirm before
  provisioning.

## Revision history

- **2026-08-17, initial design-review pass:** the original version of this doc
  locked "self-host Qwen3-Embedding" and left PDF-OCR/library choice open. After
  verifying claims against live code and checking actual Azure resources
  (`az cognitiveservices account list/show`, `az cosmosdb show`), both were revised:
  embeddings and OCR moved to new dedicated hosted Azure resources (§0.1), vector
  scoring moved to in-app cosine similarity instead of enabling Cosmos-native vector
  search on the production `governance-agent-db` account, PDF extraction moved to
  PyMuPDF, and the ingest/delete approval-gate question was resolved to "no gate."
  Two gaps not in the original doc were also found and added to scope: `owner`
  identity-stability (confirmed safe) and `delete_consumer` not cascading into
  personal-KB documents (confirmed gap, added to §2/§3).
- **2026-08-17, same-day follow-up:** PyMuPDF's AGPL license (vs. permissive
  pypdf/pdfplumber) came up as an unresolved open item. Rather than just swapping
  to a different local library, revisited whether a local library was needed at
  all — since Document Intelligence was already in the design as the scanned-PDF
  OCR fallback, and its extraction quality is generally more reliable than any of
  the three local candidates on ordinary text-layer PDFs too (multi-column layout,
  tables, broken font/encoding maps). Decision: **route all PDF extraction through
  Document Intelligence, no local PDF-parsing library at all** — removes the
  license question entirely rather than resolving it, and is expected to produce
  better extraction quality, not just less hassle. Accepted tradeoff: every
  personal PDF now leaves the app to Azure (not just scanned ones), and PDF
  ingestion has a hard dependency on Document Intelligence's availability.
- **2026-08-17, implementation pass:** built out per this doc — see §3 for the
  file list. Notable deviations found/made while actually wiring it up, not
  during design: (1) split the "knowledge" category's tool wildcard into an
  explicit list plus a new `personal_knowledge` category, since a bare `"*"`
  would have silently handed the new write-risk personal tools to every existing
  company-KB-entitled consumer; (2) granted `personal_knowledge` to every
  department by default (§2) rather than requiring per-consumer admin grants;
  (3) reused the pre-existing `general-usage-resource` for embeddings instead of
  a new dedicated resource after all (§0.1's "revised a second time" note) — a
  deliberate, known exception, not a reversal of the isolation reasoning.
  Confirmed live via `az cli` rather than assumed: `governance-agent-db`'s
  resource group/database name, the two new Cosmos containers were created
  (`personalKnowledgeDocuments`/`personalKnowledgeChunks`, both PK `/owner`),
  `text-embedding-3-large`'s availability in Canada Central via the Foundry
  model catalog, and Document Intelligence's current pricing/tiers. **Open before
  this is live in production:** the Document Intelligence resource still needs
  provisioning; both category/department edits need a manual admin-UI step to
  reach the already-populated production Cosmos store (see §2's cascade-delete
  note and the caveat this pass added there); env var names in
  `mcp-knowledge/.env.local.example` need reconciling against whatever the
  portal actually named things.
