<#
.SYNOPSIS
    Deterministic build + deploy + verify for the Stage-1 Governance Gateway
    (gateway + mcp-minierp + the other local-only backends), replacing ad-hoc
    VS Code "Deploy to Web App" clicks.

.WHY
    On 2026-08-10, VS Code-extension deploys to Governence-agent completed
    "successfully" (real Oryx build, fresh build artifacts) but there was no reliable
    way to confirm afterward that the frontend was freshly built or that the deploy
    actually reflected the current source -- file-timestamp/VFS inspection turned out
    to be misleading for this app's build mode. The only trustworthy check turned out
    to be exercising the live app itself. This script makes the steps that are
    supposed to happen before every deploy (per DEPLOY.md) actually happen every time,
    in order, and ends with a real /health check instead of guesswork.

.USAGE
    ./deploy.ps1
    ./deploy.ps1 -AppName Governence-agent -ResourceGroup RG_IVA_Prod
    ./deploy.ps1 -SkipFrontendBuild     # only if you just built it yourself
    ./deploy.ps1 -AllowDirty            # deploy uncommitted changes anyway (warns)
#>
param(
    [string]$ResourceGroup = "RG_IVA_Prod",
    [string]$AppName = "Governence-agent",
    [switch]$SkipFrontendBuild,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "== governence_agent (Stage 1) deploy ==" -ForegroundColor Cyan
Write-Host "Source folder : $root"
Write-Host "Target        : $AppName / $ResourceGroup"

# ── 1. Build the frontend -- Oryx's remote build only runs `pip install`, it never
#      builds the nested gateway/frontend/ Node project. Skipping this ships stale UI
#      with no error (DEPLOY.md Step 3). ────────────────────────────────────────────
if (-not $SkipFrontendBuild) {
    Write-Host "`nBuilding frontend..." -ForegroundColor Cyan
    & "$root/build-frontend.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ABORT: frontend build failed." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "Skipping frontend build (-SkipFrontendBuild) -- make sure gateway/frontend/dist is current." -ForegroundColor Yellow
}

$distIndex = Join-Path $root "gateway/frontend/dist/index.html"
if (-not (Test-Path $distIndex)) {
    Write-Host "ABORT: $distIndex not found -- frontend was never built. The gateway will silently fall back to the legacy static shell." -ForegroundColor Red
    exit 1
}

# ── 2. MCP tool registration must be consistent (backend <-> manifest.py <->
#      gateway wrapper) and the gateway's LLM-facing description must not have
#      silently dropped a caveat manifest.py already carries. See
#      _smoke/test_tool_registration_consistency.py's own docstring for why
#      each check exists. Same gate the VS Code Azure extension's "Deploy to
#      Web App" runs via appService.preDeployTask (.vscode/tasks.json) -- kept
#      here too so `./deploy.ps1` alone is a complete, deterministic gate. ────
Write-Host "`nChecking MCP tool registration..." -ForegroundColor Cyan
& "$root/check-tool-registration.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ABORT: tool registration/content-drift check failed -- see output above." -ForegroundColor Red
    exit 1
}

# ── 3. Refuse to ship uncommitted source changes (build output/deps are exempt) ────
Push-Location $root
try {
    $dirty = git status --porcelain . |
        Where-Object { $_ -notmatch 'gateway/frontend/(dist|node_modules)/' } |
        Where-Object { $_ -notmatch '\.venv/' }
    $sha = (git rev-parse HEAD).Trim()
} finally {
    Pop-Location
}

if ($dirty) {
    if (-not $AllowDirty) {
        Write-Host "ABORT: uncommitted changes in governence_agent/ -- commit first, or rerun with -AllowDirty." -ForegroundColor Red
        $dirty | ForEach-Object { Write-Host "  $_" }
        exit 1
    }
    Write-Host "WARNING: deploying with uncommitted changes on top of $sha" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "Working tree clean at commit $sha"
}

# ── 4. Zip, respecting the same exclude list as .vscode/settings.json's
#      appService.zipIgnorePattern (kept in sync manually -- see that file). ───────
$excludeRegex = '(^|[\\/])(\.venv|__pycache__|_smoke|governance_service|node_modules|\.git|data)([\\/]|$)' +
                '|gateway[\\/]frontend[\\/]src([\\/]|$)' +
                '|\.pyc$|\.env\.local$|appservice\.settings\.local\.json$|minierp\.env$|\.md$'

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zipPath = Join-Path $env:TEMP "governence_agent-$stamp.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

$stagingDir = Join-Path $env:TEMP "governence_agent-stage-$stamp"
if (Test-Path $stagingDir) { Remove-Item $stagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $stagingDir | Out-Null

Write-Host "`nStaging deploy contents (excluding dev-only paths)..." -ForegroundColor Cyan
Get-ChildItem -Path $root -Recurse -File -Force | Where-Object {
    $rel = $_.FullName.Substring($root.Length + 1) -replace '\\', '/'
    $rel -notmatch $excludeRegex
} | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length + 1)
    $dest = Join-Path $stagingDir $rel
    New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
    Copy-Item $_.FullName $dest
}

Compress-Archive -Path "$stagingDir/*" -DestinationPath $zipPath -CompressionLevel Optimal
Remove-Item $stagingDir -Recurse -Force

$zipSizeMb = [Math]::Round((Get-Item $zipPath).Length / 1MB, 2)
Write-Host "Zip built: $zipPath ($zipSizeMb MB)"

# ── 5. Deploy via config-zip -- NOT `az webapp deploy`/OneDeploy (documented in
#      DEPLOY.md to have silently skipped the build on this exact app before). ────
Write-Host "`nDeploying via config-zip (watch for a real 'Building...' phase, not 0s)..." -ForegroundColor Cyan
az webapp deployment source config-zip -g $ResourceGroup -n $AppName --src $zipPath

# ── 6. Verify liveness -- NOT file inspection (proved unreliable for this app's
#      build mode on 2026-08-10). Confirms the process restarted and is answering,
#      not that any specific fix is behaviorally live -- follow up with a real
#      question in the chat UI for that, the same way we just confirmed the
#      REGION fix. ──────────────────────────────────────────────────────────────
Write-Host "`nChecking /health..." -ForegroundColor Cyan
$hostName = az webapp show -g $ResourceGroup -n $AppName --query defaultHostName -o tsv
$healthUrl = "https://$hostName/health"
try {
    $health = Invoke-RestMethod -Uri $healthUrl -Method GET -TimeoutSec 30
    Write-Host "OK: $healthUrl -> $($health | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
    Write-Host "FAILED to reach $healthUrl : $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host "`nDeploy of commit $sha submitted and process is responding." -ForegroundColor Green
Write-Host "Now confirm the actual change behaviorally -- ask the chat UI a question that exercises it." -ForegroundColor Yellow
