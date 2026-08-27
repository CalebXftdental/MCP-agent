# Single entry point for the VS Code Azure App Service extension's
# appService.preDeployTask (see .vscode/tasks.json + .vscode/settings.json) --
# runs the same two gates deploy.ps1 already runs by hand, so a plain
# "Deploy to Web App" click gets them too, not just ./deploy.ps1 runs.
# A non-zero exit here BLOCKS the extension's deploy.
$ErrorActionPreference = "Stop"

Write-Host "== pre-deploy: build frontend ==" -ForegroundColor Cyan
& "$PSScriptRoot\build-frontend.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ABORT: frontend build failed." -ForegroundColor Red
    exit 1
}

Write-Host "`n== pre-deploy: MCP tool registration check ==" -ForegroundColor Cyan
& "$PSScriptRoot\check-tool-registration.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ABORT: tool registration/content-drift check failed -- see output above." -ForegroundColor Red
    exit 1
}

Write-Host "`nPre-deploy checks passed." -ForegroundColor Green
