<#
.SYNOPSIS
    Starts the Stage 1 legacy governance stack: 4 miniERP backends + gateway.
.DESCRIPTION
    Launches each service headlessly (no console window); stdout/stderr go to
    logs\<service>.log. Run from anywhere; paths are resolved relative to this
    script's location.
#>

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs'

if (-not (Test-Path $python)) {
    throw "venv not found at $python -- run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r gateway\requirements.txt"
}

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

$services = @(
    @{ Name = 'mcp-minierp-orders';    Port = 8021 },
    @{ Name = 'mcp-minierp-finance';   Port = 8022 },
    @{ Name = 'mcp-minierp-accounts';  Port = 8023 },
    @{ Name = 'mcp-minierp-shipments'; Port = 8024 },
    @{ Name = 'gateway';               Port = 8020 }
)

foreach ($svc in $services) {
    $dir = Join-Path $root $svc.Name
    $envLocal = Join-Path $dir '.env.local'
    if (-not (Test-Path $envLocal)) {
        Write-Warning "$($svc.Name): missing .env.local (copy .env.local.example and fill it in) -- skipping"
        continue
    }
    $log = Join-Path $logDir "$($svc.Name).log"
    Write-Host "Starting $($svc.Name) on :$($svc.Port) (log: $log) ..."
    $appPy = Join-Path $dir 'app.py'
    Start-Process -FilePath $python `
        -ArgumentList "`"$appPy`"" `
        -WorkingDirectory $dir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $log `
        -RedirectStandardError "$log.err"
    Start-Sleep -Seconds 1
}

Write-Host ""
Write-Host "All services launched headlessly. Gateway dashboard: http://localhost:8020/dashboard"
Write-Host "Logs: $logDir\<service>.log (+ .err)"
Write-Host "Run .\stop_legacy.ps1 to stop the stack."
