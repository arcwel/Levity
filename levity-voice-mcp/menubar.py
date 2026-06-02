#!/usr/bin/env python3
"""Levity Voice menu bar app — quick toggles for the MCP server (macOS).

Communicates with the MCP server via two files in ~/.levity-voice/:
  - config.json   — status (read every 2s to refresh the menu)
  - command.json  — one-shot commands (written on user click; server deletes after)

Supported commands match the cross-platform TTS server:
  start, stop, response_on, response_off, restart, mode_quick, mode_full.

The menu bar app is its own process; quitting it does not stop the MCP server.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import rumps

CONFIG_DIR = Path.home() / ".levity-voice"
CONFIG_FILE = CONFIG_DIR / "config.json"
COMMAND_FILE = CONFIG_DIR / "command.json"
MENUBAR_PID_FILE = CONFIG_DIR / "menubar.pid"
# Optional custom status-bar icon (template PNG). Falls back to an emoji title.
ICON_FILE = CONFIG_DIR / "levity-icon.png"

LAUNCH_AGENT_LABEL = "com.levity.voice.menubar"
LAUNCH_AGENT_PATH = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

ICON_OFF = "🎙✕"
ICON_IDLE = "🎙"

POLL_INTERVAL_SEC = 2.0


def _write_command(action: str) -> None:
    """Write a one-shot command for the MCP server to consume."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = COMMAND_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"action": action}))
    tmp.replace(COMMAND_FILE)


def _read_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _launch_agent_plist() -> str:
    python = Path.home() / ".levity-voice/venv/bin/python"
    script = Path.home() / ".levity-voice/menubar.py"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCH_AGENT_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{python}</string>\n"
        f"        <string>{script}</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <false/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _already_running() -> bool:
    """Single-instance guard: True if another live menu-bar app is running."""
    try:
        pid = int(MENUBAR_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid():
        try:
            os.kill(pid, 0)
            return True  # a live instance owns the PID
        except (ProcessLookupError, PermissionError):
            pass
    try:
        MENUBAR_PID_FILE.write_text(str(os.getpid()))
    except OSError:
        pass
    return False


class LevityVoiceApp(rumps.App):
    def __init__(self):
        # Use the Levity logo as a template status-bar icon if present;
        # otherwise fall back to the emoji title.
        icon = str(ICON_FILE) if ICON_FILE.exists() else None
        super().__init__(
            name="LevityVoice",
            title=None if icon else ICON_OFF,
            icon=icon,
            template=True,
            quit_button=None,
        )
        self._has_icon = icon is not None

        # Response Mode (at the top): Quick = yes/no, Full = free-form transcript.
        # Two top-level checkable items (more reliable than a submenu in rumps),
        # under a disabled header that acts as the section label.
        self.mode_header = rumps.MenuItem("Response Mode")  # no callback = header
        self.mode_quick_item = rumps.MenuItem("  Quick (Yes / No)", callback=self.set_mode_quick)
        self.mode_full_item = rumps.MenuItem("  Full (Free-form)", callback=self.set_mode_full)

        self.server_item = rumps.MenuItem("Server: OFF", callback=self.toggle_server)
        self.response_item = rumps.MenuItem("Voice Response: ON", callback=self.toggle_response)
        self.restart_item = rumps.MenuItem("Restart Server", callback=self.restart_server)
        self.launch_item = rumps.MenuItem("Launch at Login", callback=self.toggle_launch_at_login)
        self.quit_item = rumps.MenuItem("Quit", callback=self.quit_app)

        self.menu = [
            self.mode_header,
            self.mode_quick_item,
            self.mode_full_item,
            None,
            self.server_item,
            self.response_item,
            None,
            self.restart_item,
            None,
            self.launch_item,
            None,
            self.quit_item,
        ]

        self._last_cfg: dict = {}
        self._refresh()
        self.timer = rumps.Timer(self._tick, POLL_INTERVAL_SEC)
        self.timer.start()

    def _tick(self, _sender) -> None:
        self._refresh()

    def _refresh(self) -> None:
        cfg = _read_config()
        if cfg == self._last_cfg:
            return
        self._last_cfg = cfg

        server = bool(cfg.get("server_active"))
        resp = bool(cfg.get("response_active", True))
        mode = cfg.get("listen_mode", "quick")

        if not self._has_icon:
            self.title = ICON_IDLE if server else ICON_OFF

        # Reflect the current response mode (radio-style checkmarks).
        self.mode_quick_item.state = 1 if mode == "quick" else 0
        self.mode_full_item.state = 1 if mode == "full" else 0

        self.server_item.title = f"Server: {'ON' if server else 'OFF'}"
        self.server_item.state = 1 if server else 0

        self.response_item.title = f"Voice Response: {'ON' if resp else 'OFF'}"
        self.response_item.state = 1 if resp else 0

        self.restart_item.set_callback(self.restart_server if server else None)

        self.launch_item.state = 1 if LAUNCH_AGENT_PATH.exists() else 0

    def toggle_server(self, _sender) -> None:
        cur = bool(self._last_cfg.get("server_active"))
        _write_command("stop" if cur else "start")

    def toggle_response(self, _sender) -> None:
        cur = bool(self._last_cfg.get("response_active", True))
        _write_command("response_off" if cur else "response_on")

    def restart_server(self, _sender) -> None:
        _write_command("restart")

    def set_mode_quick(self, _sender) -> None:
        _write_command("mode_quick")
        # Optimistic UI update so the checkmark moves instantly.
        self.mode_quick_item.state = 1
        self.mode_full_item.state = 0
        self._last_cfg = {}  # force a real refresh on next tick

    def set_mode_full(self, _sender) -> None:
        _write_command("mode_full")
        self.mode_quick_item.state = 0
        self.mode_full_item.state = 1
        self._last_cfg = {}

    def toggle_launch_at_login(self, _sender) -> None:
        if LAUNCH_AGENT_PATH.exists():
            subprocess.run(
                ["launchctl", "unload", str(LAUNCH_AGENT_PATH)],
                check=False,
                capture_output=True,
            )
            try:
                LAUNCH_AGENT_PATH.unlink()
            except OSError as e:
                rumps.alert("Levity Voice", f"Couldn't remove plist: {e}")
                return
        else:
            LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAUNCH_AGENT_PATH.write_text(_launch_agent_plist())
            subprocess.run(
                ["launchctl", "load", str(LAUNCH_AGENT_PATH)],
                check=False,
                capture_output=True,
            )
        self._last_cfg = {}  # force redraw
        self._refresh()

    def quit_app(self, _sender) -> None:
        rumps.quit_application()


if __name__ == "__main__":
    if _already_running():
        sys.exit(0)  # another menu-bar instance is already up
    LevityVoiceApp().run()
