# Local dev launcher for the Governance Gateway: backend (FastAPI/uvicorn) +
# frontend (Vite/React dev server), both in THIS terminal. The backend runs as
# a no-new-window child process (its output interleaves into this console);
# the frontend runs in the foreground. Ctrl+C (or closing this terminal) stops
# the frontend, and the `finally` block below then kills the backend too.
#
# This does NOT start the mcp-* backends (minierp/office/email/knowledge/
# calendar/code) that startup.sh brings up for a full deploy -- those are
# only needed for the features that call out to them. Start the ones you need
# separately, or point gateway/.env.local at already-running instances.
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\dev-gateway.ps1
#         (or just `.\dev-gateway.ps1` from a PowerShell prompt)

$ErrorActionPreference = 'Stop'

$AppRoot = $PSScriptRoot
$Venv = Join-Path $AppRoot '.venv\Scripts\python.exe'
$GatewayDir = Join-Path $AppRoot 'gateway'
$FrontendDir = Join-Path $GatewayDir 'frontend'
$GatewayPort = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { '8020' }

if (-not (Test-Path $Venv)) {
    throw "Python venv not found at $Venv -- create it first (python -m venv .venv) and install gateway/requirements.txt"
}
if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
    throw "Frontend deps not installed -- run 'npm install' in $FrontendDir first"
}

Write-Host "[dev-gateway] backend  -> http://127.0.0.1:$GatewayPort  (gateway/app.py)"
Write-Host "[dev-gateway] frontend -> http://localhost:5173  (proxies /backend, /dashboard to the port above)"

$backend = Start-Process -FilePath $Venv `
    -ArgumentList @('-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', $GatewayPort, '--reload') `
    -WorkingDirectory $GatewayDir -NoNewWindow -PassThru

try {
    Push-Location $FrontendDir
    npm run dev
}
finally {
    Pop-Location
    if ($backend -and -not $backend.HasExited) {
        Write-Host "[dev-gateway] stopping backend (pid $($backend.Id))"
        Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    }
}
