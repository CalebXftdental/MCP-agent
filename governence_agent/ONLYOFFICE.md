# ONLYOFFICE Document Server integration (artifact preview/edit)

Lets the artifact workbench embed a real editor for generated DOCX/XLSX/PPTX artifacts
instead of a structural-signal dump, and lets the LLM apply small edits via a tool call.
This is a separate service from the gateway/backends in `startup.sh` — Document Server
ships as its own container and isn't just another `uvicorn` process.

## Local dev

This is meant to stay running for the whole time you're developing this feature -- it's
not a one-off smoke setup. Document Server is stateless in this architecture (it fetches,
edits, and hands the result back; `artifact_store` is the actual source of truth), so
there's no data to lose by leaving the container up indefinitely, restarting it, or even
recreating it from scratch.

```bash
docker run -d --name onlyoffice -p 8080:80 --restart unless-stopped \
  -e JWT_ENABLED=true -e JWT_SECRET=<random 32+ byte string> \
  onlyoffice/documentserver
```

`--restart unless-stopped` means it comes back on its own after a Docker Desktop or
machine restart -- `docker stop onlyoffice` / `docker start onlyoffice` to pause/resume
manually (e.g. to free up RAM) without losing the container.

Then point BOTH `gateway/.env.local` and `mcp-office/.env.local` at it, with the **same**
secret on both sides and on Document Server (verify with `docker exec onlyoffice
/var/www/onlyoffice/documentserver/npm/json -f /etc/onlyoffice/documentserver/local.json
'services.CoAuthoring.secret.session.string'`):

```
ONLYOFFICE_DOCUMENT_SERVER_URL=http://localhost:8080
ONLYOFFICE_EDIT_MODE=edit
ONLYOFFICE_JWT_SECRET=<same value as JWT_SECRET above>
GOVERNANCE_COOKIE_SECURE=false     # gateway/.env.local only -- see below
```

`mcp-office/.env.local` additionally needs:

```
GATEWAY_PUBLIC_URL=http://host.docker.internal:<gateway-port>
```

`GATEWAY_PUBLIC_URL` is read by mcp-office's `edit_office_document` tool (the
LLM-driven edit path) -- it needs an externally-reachable gateway base URL to hand
Document Builder, and unlike the gateway's own routes it has no incoming HTTP request to
derive that from. It must be `host.docker.internal`, not `localhost` -- Document Server
runs its own fetches from *inside* its Linux container, where `localhost` means the
container itself, not your Windows host (confirmed reachable via `docker exec onlyoffice
getent hosts host.docker.internal`).

Without a shared JWT secret, config signing/callback verification on both sides is a
no-op and anyone who can reach the editor URL can load/save any artifact -- fine for local
dev, not for anything reachable outside your machine.

**Testing the embedded interactive editor** (not the AI-edit-tool path) in a real browser:
browse to `http://host.docker.internal:<gateway-port>/dashboard`, **not**
`http://localhost:<gateway-port>/dashboard`. The gateway derives its own base URL for
`document.url`/`callbackUrl` from whatever host header your browser used
(`request.base_url`) -- if that's `localhost`, Document Server's container can't resolve
it back to your host at all, and the embedded editor will fail to load the document. This
is a URL-you-type-in-the-browser convention, not an env var.

`GOVERNANCE_COOKIE_SECURE=false` is unrelated to ONLYOFFICE specifically, but you'll hit
it in the same breath: dashboard sessions default to `Secure`-only cookies, which plain
`http://localhost` won't carry -- set this or login silently won't persist.

## Production hosting

**Deployed** (POC phase): Azure Container Apps, `Consumption`-only workload profile,
`minReplicas=0` (scale-to-zero) -- same resource group/region as the governance App
Service. Chosen over App Service for Containers or ACI specifically for the scale-to-zero
cost behavior while usage during the POC is unknown; revisit if usage turns out frequent
enough that the cold-start tax (Document Server's own boot -- font/theme generation, a
real tens-of-seconds delay -- not just container start) becomes a UX problem, in which
case either pin `minReplicas=1` or move to reserved capacity (App Service for Containers).
It doesn't fit into the single Azure App Service instance the rest of Stage 1 runs in
(`DEPLOY.md`) since Document Server ships as a container, not a Python process
`startup.sh` can spawn -- hence its own separate resource regardless of which hosting
option gets picked.

Container env vars set on the Document Server resource itself: `JWT_ENABLED=true`,
`JWT_SECRET=<matches ONLYOFFICE_JWT_SECRET on the gateway, exactly>`. Ingress: HTTP,
public ("accepting traffic from anywhere" -- not limited to the Container Apps
Environment), target port 80, 2 CPU / 4Gi minimum (Document Server's own stated minimum;
the default Consumption starting point of 0.5 CPU/1Gi is not enough and will fail/crash
during its own startup).

On the gateway side (`DEPLOY.md`'s settings table): `ONLYOFFICE_DOCUMENT_SERVER_URL` set
to the Container App's `https://<name>.<hash>.<region>.azurecontainerapps.io` FQDN,
`ONLYOFFICE_JWT_SECRET` matching the container's `JWT_SECRET`, `GATEWAY_PUBLIC_URL` set to
the governance App Service's own real public hostname (not `host.docker.internal` --
that was purely a local-Docker-Desktop networking artifact, irrelevant once both sides are
real Azure resources).

Licensing: Document Server **Community Edition** is free (AGPLv3) for this internal-use
case — no obligation to publish source as long as Document Server's own code isn't
modified. It comes with no vendor SLA/support and no white-labeling; factor that into
whichever hosting choice you make.

## LLM-driven edits (`edit_office_document`)

A user can add a review comment describing a change ("change B4 to 500") and click
"Ask AI to apply this" in the workbench (`gateway/static/app.html`) — that hands the
comment to the existing chat orchestrator (same entry point as every other tool call,
not a separate automation path), and the model can call the new `edit_office_document`
MCP tool (`mcp-office/app.py`) to apply it.

This does **not** use the interactive editor or the browser at all — it drives
ONLYOFFICE's headless **Document Builder** scripting engine (`POST {server}/docbuilder`),
which ships as part of the same Document Server container. The tool only accepts a small
whitelisted set of operations (`mcp-office/onlyoffice_builder.py`):

- xlsx: `{"op":"set_cell","sheet":"Sheet1","cell":"B4","value":"500"}` (`sheet` optional,
  defaults to the active sheet)
- docx: `{"op":"replace_text","find":"TBD","replace":"Q3 2026"}`

Letting the model emit arbitrary Document Builder script would be an unaudited
code-execution surface, so this whitelist -- not free-form scripting -- is the tool's
actual contract. Extending it to more ops (or pptx) means adding to that whitelist and
its script-generation function, not opening up raw script access.

Each call opens the artifact's current file (fetched via the Document Server itself,
authenticated with the same scoped `oo_token` mechanism as the embedded editor), applies
the ops, and saves the result as a new artifact version through the same
`artifact_store.create_version` path the manual "New version" button and the ONLYOFFICE
save-callback both use.
