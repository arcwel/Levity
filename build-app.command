#!/usr/bin/env bash
# Build "Levity Voice.app" (menu-bar launcher) into ~/Applications.
# Double-click in Finder, or run:  bash build-app.command
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_ICNS="$SCRIPT_DIR/levity-voice-mcp/assets/levity.icns"
APPICON="$SCRIPT_DIR/levity-voice-mcp/assets/levity-appicon.png"
APP="$HOME/Applications/Levity Voice.app"

echo "==> Assembling app bundle at: $APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> Preparing icon..."
if [ -f "$SRC_ICNS" ]; then
    cp "$SRC_ICNS" "$APP/Contents/Resources/levity.icns"        # prebuilt, no deps
else
    PY="$HOME/.levity-voice/venv/bin/python"; command -v "$PY" >/dev/null 2>&1 || PY="python3"
    "$PY" - "$APPICON" "$APP/Contents/Resources/levity.icns" <<'PY'
import sys
from PIL import Image
Image.open(sys.argv[1]).convert("RGBA").resize((1024,1024)).save(sys.argv[2], format="ICNS")
PY
fi
cat > "$APP/Contents/MacOS/LevityVoice" <<'LAUNCH'
#!/bin/bash
# Start the menu-bar controller DETACHED (not `exec`) — running python as the
# app bundle's main process breaks the menu-bar status item; a detached child
# (like `nohup ... &`) renders correctly.
PY="$HOME/.levity-voice/venv/bin/python"
SCRIPT="$HOME/.levity-voice/menubar.py"
if [ ! -x "$PY" ] || [ ! -f "$SCRIPT" ]; then
  osascript -e 'display alert "Levity Voice" message "Levity is not installed yet. Run install.command first."'
  exit 1
fi
nohup "$PY" "$SCRIPT" >/dev/null 2>&1 &
exit 0
LAUNCH
chmod +x "$APP/Contents/MacOS/LevityVoice"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Levity Voice</string>
  <key>CFBundleDisplayName</key><string>Levity Voice</string>
  <key>CFBundleIdentifier</key><string>com.levity.voice.app</string>
  <key>CFBundleExecutable</key><string>LevityVoice</string>
  <key>CFBundleIconFile</key><string>levity</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
# refresh icon cache for this bundle
touch "$APP"
echo ""
echo "Built: $APP"
echo "Open it from ~/Applications (first time: right-click → Open to bypass Gatekeeper)."
read -n 1 -s -r -p "Press any key to close..."; echo
