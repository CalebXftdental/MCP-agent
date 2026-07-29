# Builds the React dashboard (gateway/frontend) into gateway/frontend/dist,
# which the gateway serves directly (see gateway/backend/deps.py and
# gateway/backend/__init__.py). Run this before every deploy -- Oryx's
# zip-deploy build only runs `pip install` at the deploy root and never sees
# this nested frontend, so nothing builds it for you.
$ErrorActionPreference = "Stop"
$frontendDir = Join-Path $PSScriptRoot "gateway\frontend"
Push-Location $frontendDir
try {
    npm ci
    npm run build
} finally {
    Pop-Location
}
