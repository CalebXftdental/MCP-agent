# Governance Agent — Azure App Service Deployable

This folder (`governance_service/`) is the **self-contained unit deployed to Azure App
Service**. Deploy its contents as the App Service deployment root — do not deploy
`governence_agent/` itself, or the `sqlagent`/`hubspot`/`middleware` imports in `app.py`
won't resolve.

## Startup command

From this folder as the deployment root:

```
bash startup.sh
```

(or `python app.py` directly, once deps are installed). Listens on `$PORT`/`$WEBSITES_PORT`
(falls back to `GOVERNANCE_PORT` for local dev). No subprocess is spawned — this is a single
process, unlike the chatbot's gateway+backend split.

## Network exposure (read before going live)

This service holds `MINIERP_*`/`HUBSPOT_*` credentials and is meant to be reachable **only**
from the chatbot service (and any future consumer), never from the public internet. On its
own, an App Service is still publicly addressable at `*.azurewebsites.net` regardless of
whether anything links to it. Before treating this as production:

1. Set Access Restrictions on this App Service to allow only the chatbot service's outbound
   IPs (cheap, works on any plan), or
2. VNet-integrate both services and put this one behind a Private Endpoint with public
   network access disabled (Premium v2/v3+ only, full isolation).

Until one of those is in place, the `GOVERNANCE_KEY_*` check in `edge.py` is the *only* thing
standing between the public internet and these credentials.

## Required env vars

See `.env.example` for the full list. At minimum: `MINIERP_USERNAME`/`MINIERP_PASSWORD` (or
`MINIERP_TOKEN`), `HUBSPOT_ACCESS_TOKEN`, and at least one `GOVERNANCE_KEY_<NAME>` (the
chatbot's `GOVERNANCE_API_KEY` must equal `GOVERNANCE_KEY_CHATBOT`'s value).

## Verifying a fresh deploy

1. `GET /health` → `{"status": "ok"}` — confirms the process is up and listening on the right
   port at all.
2. Open `/dashboard`, paste in a `GOVERNANCE_KEY_*` value. It should connect (no "Unauthorized"
   banner) and show empty tables (no traffic yet).
3. From the chatbot, ask a question that triggers a live-data tool (e.g. "what's the status of
   order SO0012345"). Refresh the dashboard — you should see one `call` event with a real
   `client_ip` and `consumer` (not "unknown"), the tool name, and a status. **If `client_ip` or
   `consumer` show "unknown"/blank on a real call, that's a sign `edge.py`'s contextvars aren't
   propagating from the middleware into the MCP tool handler** (an untested assumption about
   how the installed `mcp` SDK's Streamable HTTP handler schedules the tool call relative to
   Starlette's `BaseHTTPMiddleware` dispatch) — flag it back rather than assuming it's fine.
4. Send one request with a deliberately wrong `Authorization` header — confirm it shows up as
   an `auth_denied` row, not silently dropped.
