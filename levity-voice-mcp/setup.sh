#!/usr/bin/env bash
# Levity Voice MCP — setup script
# Creates venv, installs deps, prints Claude Desktop config snippet.

set -euo pipefail

CONFIG_DIR="$HOME/.levity-voice"
VENV_DIR="$CONFIG_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Creating config directory at $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"

echo "==> Creating Python venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"

echo "==> Installing dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "==> Setup complete!"
echo ""
echo "Add the following to your claude_desktop_config.json"
echo "(typically at ~/Library/Application Support/Claude/claude_desktop_config.json):"
echo ""
cat <<EOF
{
  "mcpServers": {
    "levity-voice": {
      "command": "$VENV_DIR/bin/python",
      "args": ["$SCRIPT_DIR/server.py"]
    }
  }
}
EOF
echo ""
echo "Then restart Claude Desktop to load the server."
