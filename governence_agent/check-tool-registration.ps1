# Pre-deploy gate: MCP tool registration must be consistent across all three
# places a tool has to be wired (physical backend, policy/manifest.py, gateway
# wrapper), and the gateway's LLM-facing description must not have silently
# dropped a caveat manifest.py already flagged. See
# _smoke/test_tool_registration_consistency.py's own module docstring for the
# full rationale and the two real incidents (2026-08-17 registration gap,
# 2026-08-19 content drift) that motivated each check it runs.
#
# Run this before every deploy -- either directly, via deploy.ps1 (which calls
# it), or automatically as part of the VS Code Azure extension's "Deploy to Web
# App" via appService.preDeployTask (see .vscode/tasks.json at the repo root).
# A non-zero exit here means: do not deploy, a tool is unreachable or its
# description has drifted.
$ErrorActionPreference = "Stop"
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\_smoke\test_tool_registration_consistency.py"
exit $LASTEXITCODE
