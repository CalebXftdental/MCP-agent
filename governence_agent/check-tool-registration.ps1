# Pre-deploy gate: MCP tool registration must be consistent across all three
# places a tool has to be wired (physical backend, policy/manifest.py, gateway
# wrapper), and the gateway's LLM-facing description must not have silently
# dropped a caveat manifest.py already flagged. See
# _smoke/test_tool_registration_consistency.py's own module docstring for the
# full rationale and the two real incidents (2026-08-17 registration gap,
# 2026-08-19 content drift) that motivated each check it runs.
#
# Run this before every deploy -- either directly, or via deploy.ps1 (which
# calls it). NOT wired into the VS Code Azure extension's "Deploy to Web App"
# (appService.preDeployTask, .vscode/settings.json) -- it was, but hung
# indefinitely under the extension's own task runner specifically (2026-08-26,
# no process left running, no error, never reproduced running it directly),
# so that hook is build-only now (build-frontend.ps1) and this is back to a
# manual pre-deploy step for that path. A non-zero exit here means: do not
# deploy, a tool is unreachable or its description has drifted.
$ErrorActionPreference = "Stop"
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\_smoke\test_tool_registration_consistency.py"
exit $LASTEXITCODE
