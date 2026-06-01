#!/usr/bin/env bash
# Levity Voice MCP — One-click installer for macOS
# Double-click this file in Finder to install.

set -euo pipefail

CONFIG_DIR="$HOME/.levity-voice"
VENV_DIR="$CONFIG_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Levity Voice MCP — Installer           ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Step 1: Create config directory
echo "==> Creating config directory at $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"

# Step 2: Copy server.py to config dir for stable path
echo "==> Copying server.py to $CONFIG_DIR"
cp "$SCRIPT_DIR/server.py" "$CONFIG_DIR/server.py"

# Step 3: Create venv
echo "==> Creating Python venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"

# Step 4: Install deps
echo "==> Installing dependencies (this may take a few minutes on first run)..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q

# Step 5: Add to Claude Desktop config
echo "==> Configuring Claude Desktop..."

PYTHON_PATH="$VENV_DIR/bin/python"
SERVER_PATH="$CONFIG_DIR/server.py"

if [ -f "$CLAUDE_CONFIG" ]; then
    # Config exists — check if levity-voice is already there
    if grep -q "levity-voice" "$CLAUDE_CONFIG" 2>/dev/null; then
        echo "    levity-voice already in Claude config, skipping."
    else
        # Use python to safely merge into existing JSON
        "$VENV_DIR/bin/python" -c "
import json, sys

config_path = sys.argv[1]
python_path = sys.argv[2]
server_path = sys.argv[3]

with open(config_path) as f:
    config = json.load(f)

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['levity-voice'] = {
    'command': python_path,
    'args': [server_path]
}

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)

print('    Added levity-voice to Claude config.')
" "$CLAUDE_CONFIG" "$PYTHON_PATH" "$SERVER_PATH"
    fi
else
    # No config file — create one
    mkdir -p "$(dirname "$CLAUDE_CONFIG")"
    cat > "$CLAUDE_CONFIG" << CONFIGEOF
{
  "mcpServers": {
    "levity-voice": {
      "command": "$PYTHON_PATH",
      "args": ["$SERVER_PATH"]
    }
  }
}
CONFIGEOF
    echo "    Created Claude config with levity-voice."
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Installation complete!                  ║"
echo "╠══════════════════════════════════════════╣"
echo "║   Restart Claude Desktop to load the     ║"
echo "║   Levity Voice MCP server.               ║"
echo "║                                          ║"
echo "║   Then tell Claude:                      ║"
echo "║   'Start the voice server'               ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Press any key to close..."
read -n 1
