#!/bin/bash
set -euo pipefail

# Anchor all relative paths to the directory containing this script so the
# App Service startup command works no matter which directory Oryx uses.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[startup] Running from $SCRIPT_DIR"

echo "[startup] Installing governance-agent deps..."
python -m pip install -r requirements.txt -q

echo "[startup] Starting governance agent..."
exec python app.py
