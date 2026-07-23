#!/usr/bin/env bash
# Stage-1 Azure App Service startup: run BOTH the consolidated miniERP backend
# (internal, localhost) and the public Governance Gateway in one instance.
#
# Set the App Service "Startup Command" to:  bash startup.sh
#
# NOTE: with Oryx build-during-deploy the built app is extracted to a TEMP dir
# (e.g. /tmp/<id>), NOT /home/site/wwwroot. So resolve the app root from THIS
# script's own location rather than assuming a fixed path.
#
# ONE INSTANCE ONLY -- rate-limit counters, session scope, and the audit ring
# buffer are per-process/in-memory. Do not scale out until Redis/Cosmos land.
set -uo pipefail

APP_ROOT="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
echo "[startup] app root: $APP_ROOT"

# Persistent storage for the durable audit sink (/home persists across restarts).
mkdir -p /home/data /home/data/artifacts /home/data/state
export GOVERNANCE_ARTIFACT_DIR="${GOVERNANCE_ARTIFACT_DIR:-/home/data/artifacts}"
export GOVERNANCE_STATE_DIR="${GOVERNANCE_STATE_DIR:-/home/data/state}"

# 1) Consolidated backend -- bound to localhost; the gateway is its sole client.
#    Port must match MINIERP_MCP_URL (default http://127.0.0.1:8021/mcp).
cd "$APP_ROOT/mcp-minierp"
python -m uvicorn app:app --host 127.0.0.1 --port "${MINIERP_LOCAL_PORT:-8021}" --no-access-log &
echo "[startup] mcp-minierp backend starting on 127.0.0.1:${MINIERP_LOCAL_PORT:-8021}"

# 2) Office artifact backend -- local-only; the gateway is its sole client.
#    Port must match OFFICE_MCP_URL (default http://127.0.0.1:8030/mcp).
cd "$APP_ROOT/mcp-office"
python -m uvicorn app:app --host 127.0.0.1 --port "${OFFICE_LOCAL_PORT:-8030}" --no-access-log &
echo "[startup] mcp-office backend starting on 127.0.0.1:${OFFICE_LOCAL_PORT:-8030}"

# 3) Email draft backend -- local-only; send actions remain approval-gated.
#    Port must match EMAIL_MCP_URL (default http://127.0.0.1:8040/mcp).
cd "$APP_ROOT/mcp-email"
python -m uvicorn app:app --host 127.0.0.1 --port "${EMAIL_LOCAL_PORT:-8040}" --no-access-log &
echo "[startup] mcp-email backend starting on 127.0.0.1:${EMAIL_LOCAL_PORT:-8040}"

# 4) Knowledge backend -- local-only RAG adapter with local fallback.
#    Port must match KNOWLEDGE_MCP_URL (default http://127.0.0.1:8050/mcp).
cd "$APP_ROOT/mcp-knowledge"
python -m uvicorn app:app --host 127.0.0.1 --port "${KNOWLEDGE_LOCAL_PORT:-8050}" --no-access-log &
echo "[startup] mcp-knowledge backend starting on 127.0.0.1:${KNOWLEDGE_LOCAL_PORT:-8050}"

# 5) Calendar backend -- local-only; external event creation remains approval-gated.
#    Port must match CALENDAR_MCP_URL (default http://127.0.0.1:8060/mcp).
cd "$APP_ROOT/mcp-calendar"
python -m uvicorn app:app --host 127.0.0.1 --port "${CALENDAR_LOCAL_PORT:-8060}" --no-access-log &
echo "[startup] mcp-calendar backend starting on 127.0.0.1:${CALENDAR_LOCAL_PORT:-8060}"

# 6) Code planning backend -- local-only; read-only opencode planning lane.
#    Port must match CODE_MCP_URL (default http://127.0.0.1:8070/mcp).
cd "$APP_ROOT/mcp-code"
python -m uvicorn app:app --host 127.0.0.1 --port "${CODE_LOCAL_PORT:-8070}" --no-access-log &
echo "[startup] mcp-code backend starting on 127.0.0.1:${CODE_LOCAL_PORT:-8070}"

# 7) Public gateway -- the App Service web process, on the platform-assigned $PORT.
cd "$APP_ROOT/gateway"
echo "[startup] starting governance gateway on 0.0.0.0:${PORT:-8000}"
exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
