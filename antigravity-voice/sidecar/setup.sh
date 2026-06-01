#!/usr/bin/env bash
# Antigravity Voice — Python sidecar setup.
# Creates a virtual environment in `.venv/` and installs the deps in
# requirements.txt. The TypeScript SidecarManager prefers `.venv/bin/python3`
# when it exists, so once this script completes the extension will pick it up
# automatically.
#
# Usage:
#   chmod +x sidecar/setup.sh
#   ./sidecar/setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

echo "=== Antigravity Voice sidecar setup ==="

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH. Install Python 3.9+ and retry." >&2
    exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
else
    echo "Reusing existing virtual environment at $VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

echo "Upgrading pip..."
pip install --upgrade pip --quiet

echo "Installing requirements (this can take a few minutes — torch is large)..."
pip install -r "$REQ_FILE"

echo ""
echo "=== Setup complete ==="
echo "Python: $(python --version)"
echo "Venv:   $VENV_DIR"
echo ""
echo "Smoke test:  echo '{\"action\":\"listen\"}' | $VENV_DIR/bin/python3 $SCRIPT_DIR/voice_worker.py"
