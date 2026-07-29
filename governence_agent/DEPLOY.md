# Deploying Stage 1 to Azure App Service

**Stage 1 = one App Service running both processes:** the public **Governance
Gateway** (the only exposed endpoint) *and* the consolidated **mcp-minierp**
backend (bound to localhost, the gateway's sole client). A `startup.sh` launches
both. This gives you the full governed surface — dashboard, login/RBAC, the 26
governed tools, and the Qwen chat — from a single folder deploy via the Azure
extension.

> **⚠️ The one thing that will block live data:** `db-api.frontierdental.com` is
> **IP-allowlisted**. Your App Service's **outbound IP addresses** must be added
> to that allowlist or every tool call fails with a 403 at the DB (auth never
> even runs). Get the outbound IPs from *App Service → Networking → Outbound
> addresses* (or `az webapp show ... --query outboundIpAddresses`) and have them
> allowlisted on db-api. Everything else works without this; only live data
> needs it.

---

## What gets deployed

- **Deploy root:** this `governance_agent/` folder.
- **Ships:** `gateway/` (React dashboard **built** to `gateway/frontend/dist/` --
  see Step 0 below -- plus the legacy static shell it falls back to),
  `governance_core/` (auth, store, policy), `mcp-minierp/` (the consolidated
  backend), `mcp-office/`, `mcp-email/`, `mcp-knowledge/`, `mcp-calendar/`,
  `mcp-code/` (the other local-only backends `startup.sh` launches),
  `minierp_core/` (shared DB client), `startup.sh`, `requirements.txt`,
  `.deployment`.
- **Exclude:** `.venv/`, `**/__pycache__/`, `_smoke/`, the four legacy
  `mcp-minierp-orders|accounts|finance|shipments/` folders, `governance_service/`,
  `gateway/frontend/node_modules/`, `gateway/frontend/src/` (only the built
  `dist/` is served at runtime), `**/.env.local`, `**/*.pyc`. The VS Code
  extension's zip-ignore (`.vscode/settings.json`) already has these.

## Dependency versions are exact-pinned, not floating

Every `requirements.txt` under `governance_agent/` (root, `gateway/`, and each
`mcp-*/`) pins its direct dependencies with `==`, not `>=`. This is deliberate,
not stylistic: on 2026-07-28, `mcp>=1.10.0` let Oryx install whatever `mcp`
release was newest *at deploy time*, and that release had renamed
`streamablehttp_client` and moved `FastMCP`'s import path out from under this
code — every backend process crash-looped (`ModuleNotFoundError`/`ImportError`)
until the app hit its startup timeout and Azure gave up on it. The same class of
break was latent in every other unbounded dependency (`uvicorn`, `starlette`,
`httpx`, `openai`, `anthropic`, `azure-cosmos`, `azure-search-documents`,
`azure-core`); they're now pinned too, to the versions verified running
2026-07-28.

**When you deliberately want to upgrade one:** bump it locally, reinstall in
`governance_agent/.venv`, actually boot the gateway and exercise the dashboard
(don't just `pip install` and assume), then bump the same pin everywhere it
appears (root + `gateway/` + any `mcp-*/` that also lists it) before deploying.
Never widen a pin back to `>=` to "fix" an install error — that's exactly what
broke production.

## Prerequisites

- Azure subscription + resource group.
- **App Service (Linux), Python 3.12**, **Always On** enabled, **1 instance**.
- VS Code **Azure App Service** extension (or `az` CLI).

---

## Step 1 — Create the Web App

Azure extension: App Services → **+** (Create Web App, Advanced) → Python 3.12,
Linux, plan B1+. Or:
```bash
az group create -n frontier-gov-rg -l westus2
az appservice plan create -g frontier-gov-rg -n frontier-gov-plan --is-linux --sku B1
az webapp create -g frontier-gov-rg -p frontier-gov-plan -n frontier-governance --runtime "PYTHON:3.12"
```

## Step 2 — Configure BEFORE deploying

**Startup command:** `bash startup.sh`
```bash
az webapp config set -g frontier-gov-rg -n frontier-governance --startup-file "bash startup.sh"
az webapp config set -g frontier-gov-rg -n frontier-governance --number-of-workers 1 --always-on true
az webapp config set -g frontier-gov-rg -n frontier-governance --health-check-path "/health"
az webapp config appsettings set -g frontier-gov-rg -n frontier-governance --settings WEBSITES_CONTAINER_START_TIME_LIMIT=1800
```
`WEBSITES_CONTAINER_START_TIME_LIMIT=1800` matters here specifically: `startup.sh`
boots seven Python processes (gateway + six backends) in one container, which can
run close to or past Azure's default container-start timeout — observed causing
a failed/retried startup on 2026-07-28 even once the app itself was otherwise
healthy. Without it, a slow-but-fine boot gets killed and looks like a crash.

If `--health-check-path` errors as an unrecognized argument (older `az` CLI
versions), set it via `az resource update -g frontier-gov-rg -n frontier-governance
--resource-type Microsoft.Web/sites --set properties.siteConfig.healthCheckPath=/health`
instead.

**App settings (environment variables)** — secrets should be Key Vault references.
`appservice.settings.json` in this folder is a ready-to-edit bulk-import file
(`az webapp config appsettings set --settings @appservice.settings.json`).

| Setting | Req | Value / notes |
|---|:--:|---|
| `GOVERNANCE_SESSION_SECRET` | ✅🔑 | Random 32+ bytes; signs session cookies. Unset ⇒ login disabled. |
| `GATEWAY_ALLOWED_HOSTS` | ✅ | `frontier-governance.azurewebsites.net` (+ custom domains, comma-sep). **Without it the MCP transport rejects every `/mcp` request (DNS-rebind guard).** |
| `GOVERNANCE_STORE_FILE` | ✅¹ | `/home/data/policy.json` (persistent — survives restart/redeploy). ¹Omit if using Cosmos (below). |
| `GOVERNANCE_AUDIT_FILE` | ✅ | `/home/data/audit.jsonl` (durable "who asked" monitor; survives restart). |
| `GOVERNANCE_ADMIN_USER` | ✅ | e.g. `admin` — seeds the first admin on first boot (only if the store file doesn't exist yet). |
| `GOVERNANCE_ADMIN_PASSWORD` | ✅🔑 | First admin password; rotate after first login. |
| `GOVERNANCE_COOKIE_SECURE` | ✅ | `true` (HTTPS — App Service provides it). |
| `MINIERP_USERNAME` / `MINIERP_PASSWORD` | ✅🔑 | Default miniERP account (orders/accounts/shipments domains); or `MINIERP_TOKEN` for a pre-issued JWT. |
| `MINIERP_ADMIN_USERNAME` / `MINIERP_ADMIN_PASSWORD` | ✅🔑 | The **administrator** account the **finance** domain uses (AR/AP/GL/PO/Vendor). Two miniERP accounts are required. |
| `MINIERP_MCP_URL` | ✅ | `http://127.0.0.1:8021/mcp` — gateway → the local backend. Must match `MINIERP_LOCAL_PORT` (default 8021). |
| `GOVERNANCE_CHAT_BASE_URL` | ✅ | `http://20.120.220.8:8000/v1` (self-hosted Qwen/sglang). |
| `GOVERNANCE_CHAT_MODEL` | ✅ | `/home/azureuser/models/Qwen3.6-27B-FP8`. |
| `GOVERNANCE_CHAT_API_KEY` | ✅🔑 | Qwen endpoint key (from `Qwen3.6_27B/localllm.env`). |
| `GOVERNANCE_CHAT_MAX_TOKENS` | rec | `2048`. |
| `GOVERNANCE_CHAT_THINKING` | rec | `off` (faster/cheaper for tool routing). |
| `USE_LOCAL_LLM` | ✅ | `true` (default) uses the Qwen backend above; set to `false` to switch the chat orchestrator to the Claude Sonnet backup below (e.g. if the Qwen VM is down). |
| `GOVERNANCE_ANTHROPIC_ENDPOINT` | opt¹ | Anthropic-compatible endpoint, e.g. Azure AI Foundry's `https://<resource>.services.ai.azure.com/anthropic`. ¹Only read when `USE_LOCAL_LLM=false`. |
| `GOVERNANCE_ANTHROPIC_API_KEY` | opt¹🔑 | Key for the endpoint above — Key Vault ref. |
| `GOVERNANCE_ANTHROPIC_MODEL` | opt¹ | Deployment/model name, e.g. `claude-sonnet-4-6`. |
| `MINIERP_GRAPHQL_URL` / `MINIERP_AUTH_URL` | opt | Default to the prod db-api endpoints; override only if they change. |
| `GOVERNANCE_KEY_PEPPER` | rec🔑 | Extra secret mixed into API-key hashes. |
| `GOVERNANCE_DEFAULT_RATE_LIMIT_PER_HOUR` | rec | e.g. `1000`. |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | ✅ | `true` (also in `.deployment`). Only takes effect with `az webapp deployment source config-zip` or the Azure extension — **not** `az webapp deploy`, which has been observed to skip the build entirely on this app despite this setting. See Step 3. |
| `WEBSITES_CONTAINER_START_TIME_LIMIT` | ✅ | `1800` — the seven-process `startup.sh` boot needs more than Azure's default container-start allowance. |
| `ONLYOFFICE_DOCUMENT_SERVER_URL` | opt¹ | The deployed Document Server's public HTTPS URL (Container App / ACI / App Service for Containers — see `ONLYOFFICE.md`). ¹Unset ⇒ artifact preview/edit stays a structural signal dump, no editor embed. |
| `ONLYOFFICE_JWT_SECRET` | opt🔑 | Must match the `JWT_SECRET` env var set on the Document Server resource itself, exactly. |
| `ONLYOFFICE_EDIT_MODE` | opt | `edit` to allow in-editor saves + the AI edit tool; `view` (default) for read-only preview. |
| `GATEWAY_PUBLIC_URL` | opt | This App Service's own public HTTPS URL. Read by `mcp-office`'s `edit_office_document` tool (it has no incoming request to derive its own base URL from, unlike the gateway's routes). |

## Step 3 — Build the frontend, then deploy

**The frontend build must run before every deploy, on your machine (or CI) — not
on Azure.** Oryx's zip-deploy build (`SCM_DO_BUILD_DURING_DEPLOYMENT=true`) only
runs `pip install -r requirements.txt` at the deploy root; it never sees or
builds the nested `gateway/frontend/` Node project. If you skip this step, the
gateway falls back to serving the old static shell (`gateway/static/app.html`)
instead of the current dashboard — no error, just stale UI.

Requires Node.js installed locally. From `governance_agent/`:
```bash
./build-frontend.sh          # or: .\build-frontend.ps1  on Windows
```
This runs `npm ci && npm run build` in `gateway/frontend`, producing
`gateway/frontend/dist/`, which `gateway/backend/deps.py` and
`gateway/backend/__init__.py` serve directly (`/dashboard*` → `dist/index.html`,
`/assets/*` → `dist/assets/`). Rerun it any time frontend source changes.

**Azure extension:** after building, set the zip-ignore in `.vscode/settings.json`
(already added in this folder), then right-click the `governance_agent` folder →
**Deploy to Web App**.

**az CLI** (from inside `governance_agent/`, after building — zip the folder
respecting `.vscode/settings.json`'s ignore list first):
```bash
az webapp deployment source config-zip -g frontier-gov-rg -n frontier-governance --src <path-to-zip>
```

> **⚠️ Do NOT use `az webapp deploy --type zip` (OneDeploy) for this app.**
> Verified the hard way on 2026-07-28: `az webapp deploy` reported
> `Build successful. Time: 0(s)` and shipped the zip's files as-is *without
> running `pip install`* — despite `SCM_DO_BUILD_DURING_DEPLOYMENT=true` being
> set. The site then crash-looped (`ModuleNotFoundError`/`ImportError` for
> every dependency) until it hit Azure's startup timeout and the deploy failed.
> `az webapp deployment source config-zip` (used above) and the VS Code
> extension both correctly trigger the Oryx build — `config-zip`'s own
> deprecation warning ("use `az webapp deploy` instead") is safe to ignore for
> this app; that "successor" command is the one that silently broke it.
>
> Either way, **verify the build actually ran** by watching for
> `Status: Building the app...` taking real time (tens of seconds, not `0(s)`)
> before `Build successful` in the CLI output — that's the tell.

## Step 4 — Verify

```bash
curl https://frontier-governance.azurewebsites.net/health     # {"status":"ok","service":"governance-gateway"}
```
1. Open `https://<app>.azurewebsites.net/dashboard` → sign in as the seeded admin.
2. `/dashboard/admin` → create the users/consumers you need and assign **categories**
   (e.g. `accounts`, `orders`; `analytics` only for cross-customer ranking).
3. Open `/dashboard/chat` and ask a question (e.g. *"find the customer jane@example.com"*).
   - If tool results come back as governed errors, check the **db-api IP allowlist**
     (the warning at the top) and the miniERP creds.
4. Machine callers (email bot, etc.) hit `https://<app>.azurewebsites.net/mcp` with
   `Authorization: Bearer <their API key>` (mint keys in the Consumers panel).

---

## Caveats (Stage 1)

- **One instance only.** Rate-limit counters, session scope, and the audit ring buffer
  are per-process. The file store + durable audit sink persist, but cross-instance
  coordination does not — keep the plan at 1 instance until Redis/Cosmos land.
- **Persistence:** `/home/data` survives restarts and redeploys (wwwroot does not).
  Back up `policy.json`.
- **The backend is a background process** started by `startup.sh`; it isn't supervised
  independently (fine for a POC — a full crash restarts the whole instance).
- **Secrets → Key Vault references**, not plaintext app settings.

## Using Cosmos DB for the policy store (durable, shared)

`store/cosmos.py` is implemented. To back per-user policy + category scope with Cosmos
instead of the local file (durable across restarts/redeploys and shared across instances):

1. Create a **Cosmos DB (Core/SQL) account** in the same region. **No manual container
   setup needed** — the app **auto-creates** the database + containers on first boot
   (`consumers` `/consumerId`, `categories` `/id`, `accessRequests` `/consumerId`,
   `config` `/id`) and seeds the default categories + admin.
2. Set these app settings **instead of** `GOVERNANCE_STORE_FILE` (Cosmos takes precedence
   if both are set):

| Setting | Value |
|---|---|
| `GOVERNANCE_COSMOS_URL` 🔑 | `https://<account>.documents.azure.com:443/` |
| `GOVERNANCE_COSMOS_KEY` 🔑 | account primary key *(or use `GOVERNANCE_COSMOS_CONNECTION_STRING` alone)* |
| `GOVERNANCE_COSMOS_DATABASE` | `governance` |
| `GOVERNANCE_COSMOS_CACHE_TTL_SEC` | `30` (read-cache freshness) |

Keep `GOVERNANCE_AUDIT_FILE` (audit stays JSONL for now). **Caveat:** Cosmos makes
*policy* durable + shared, but rate-limit counters, session scope, and the audit ring
buffer are still per-instance — for true multi-instance scale-out you also need Redis
(counters/session) + a shared audit sink. Until then, still run **1 instance**.

## Later

- **Split the backend to Container Apps** (private ingress + mTLS) and point
  `MINIERP_MCP_URL` at its internal FQDN — zero gateway code change.
- **Redis** for distributed rate-limit/session + a **Cosmos/Log Analytics audit sink**
  → then you can scale past 1 instance.
- **Stage 2** (delegated Teams identity): `X-On-Behalf-Of` + `actor ∩ subject`.
