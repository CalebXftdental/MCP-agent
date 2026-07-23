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
- **Ships:** `gateway/` (+ static UI), `governance_core/` (auth, store, policy),
  `mcp-minierp/` (the consolidated backend), `minierp_core/` (shared DB client),
  `startup.sh`, `requirements.txt`, `.deployment`.
- **Exclude:** `.venv/`, `**/__pycache__/`, `_smoke/`, the four legacy
  `mcp-minierp-orders|accounts|finance|shipments/` folders, `governance_service/`,
  `**/.env.local`, `**/*.pyc`.

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
```

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
| `MINIERP_GRAPHQL_URL` / `MINIERP_AUTH_URL` | opt | Default to the prod db-api endpoints; override only if they change. |
| `GOVERNANCE_KEY_PEPPER` | rec🔑 | Extra secret mixed into API-key hashes. |
| `GOVERNANCE_DEFAULT_RATE_LIMIT_PER_HOUR` | rec | e.g. `1000`. |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | ✅ | `true` (also in `.deployment`). |

## Step 3 — Deploy

**Azure extension:** set the zip-ignore in `.vscode/settings.json` (already added in
this folder), then right-click the `governance_agent` folder → **Deploy to Web App**.

**az CLI** (from inside `governance_agent/`):
```bash
az webapp up -g frontier-gov-rg -n frontier-governance --runtime "PYTHON:3.12"
```

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
