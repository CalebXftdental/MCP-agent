#!/usr/bin/env bash
# Builds the React dashboard (gateway/frontend) into gateway/frontend/dist,
# which the gateway serves directly (see gateway/backend/deps.py and
# gateway/backend/__init__.py). Run this before every deploy -- Oryx's
# zip-deploy build only runs `pip install` at the deploy root and never sees
# this nested frontend, so nothing builds it for you.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/gateway/frontend"
npm ci
npm run build
