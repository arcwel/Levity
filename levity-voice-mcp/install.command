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

# Step 2: Copy server + companion files to config dir for stable paths
echo "==> Copying server.py to $CONFIG_DIR"
cp "$SCRIPT_DIR/server.py" "$CONFIG_DIR/server.py"
# Menu-bar app (optional) and the Claude Code Stop hook.
[ -f "$SCRIPT_DIR/menubar.py" ] && cp "$SCRIPT_DIR/menubar.py" "$CONFIG_DIR/menubar.py"
# Menu-bar status icon (template PNG).
[ -f "$SCRIPT_DIR/assets/levity-icon.png" ] && cp "$SCRIPT_DIR/assets/levity-icon.png" "$CONFIG_DIR/levity-icon.png"
if [ -d "$SCRIPT_DIR/hooks" ]; then
    mkdir -p "$CONFIG_DIR/hooks"
    cp "$SCRIPT_DIR/hooks/"*.py "$CONFIG_DIR/hooks/" 2>/dev/null || true
    chmod +x "$CONFIG_DIR/hooks/"*.py 2>/dev/null || true
fi

# Step 3: Create venv
echo "==> Creating Python venv at $VENV_DIR"
python3 -m venv "$VENV_DIR"

# Step 4: Install deps
echo "==> Installing dependencies (this may take a few minutes on first run)..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q

# Step 5: Configure applications
echo "==> Configuring applications..."

PYTHON_PATH="$VENV_DIR/bin/python"
SERVER_PATH="$CONFIG_DIR/server.py"

register_mcp() {
    local config_path="$1"
    local app_name="$2"

    if [ -f "$config_path" ]; then
        if grep -q "levity-voice" "$config_path" 2>/dev/null; then
            echo "    levity-voice already in $app_name config, skipping."
        else
            "$VENV_DIR/bin/python" -c "
import json, sys
path = sys.argv[1]
python_path = sys.argv[2]
server_path = sys.argv[3]

with open(path) as f:
    config = json.load(f)

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['levity-voice'] = {
    'command': python_path,
    'args': [server_path]
}

with open(path, 'w') as f:
    json.dump(config, f, indent=4)
" "$config_path" "$PYTHON_PATH" "$SERVER_PATH"
            echo "    Added levity-voice to $app_name config."
        fi
    else
        # If the parent directory exists (which means the app is installed/has been run)
        local parent_dir
        parent_dir="$(dirname "$config_path")"
        if [ -d "$parent_dir" ]; then
            cat > "$config_path" << CONFIGEOF
{
    "mcpServers": {
        "levity-voice": {
            "command": "$PYTHON_PATH",
            "args": ["$SERVER_PATH"]
        }
    }
}
CONFIGEOF
            echo "    Created $app_name config with levity-voice."
        fi
    fi
}

register_mcp "$CLAUDE_CONFIG" "Claude Desktop"
register_mcp "$HOME/.gemini/antigravity-ide/mcp_config.json" "Antigravity IDE"
register_mcp "$HOME/.gemini/antigravity/mcp_config.json" "standalone Antigravity App"

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
