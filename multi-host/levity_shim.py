#!/usr/bin/env python3
"""
levity-shim — thin per-host MCP server (SCAFFOLD, branch: multi-host-daemon).

Each host (Claude Desktop, Antigravity IDE, Antigravity.app) runs its own copy
of this shim. It exposes the same voice tools but does NO audio itself — every
call is forwarded to the shared daemon (levity_voiced.py) over a Unix socket.
Because the shim owns no mic/speaker, many shims run at once with no contention
(this is what removes the single-instance churn).

If the daemon isn't running, the shim starts it, then connects.

STATUS: scaffold. Forwarding works; see docs/multi-host-voice-daemon.md for the
remaining coordination/testing work before this replaces server.py in the host
configs.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

CONFIG_DIR = Path.home() / ".levity-voice"
SOCKET_PATH = CONFIG_DIR / "voiced.sock"
DAEMON = Path(__file__).resolve().parent / "levity_voiced.py"


def _ensure_daemon() -> None:
    """Start the daemon if its socket isn't accepting connections."""
    if _try_connect() is not None:
        return
    try:
        subprocess.Popen(
            [sys.executable, str(DAEMON)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return
    for _ in range(50):  # wait up to ~5s for the socket
        time.sleep(0.1)
        if _try_connect() is not None:
            return


def _try_connect():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(str(SOCKET_PATH))
        return s
    except OSError:
        return None


def _call(req: dict, timeout: float = 90.0):
    """Send one request to the daemon and return its parsed result."""
    _ensure_daemon()
    s = _try_connect()
    if s is None:
        return {"ok": False, "error": "voice daemon unavailable"}
    try:
        s.settimeout(timeout)
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}
    finally:
        s.close()


mcp = FastMCP(
    "levity_voice",
    instructions=(
        "MANDATORY: call voice_speak at the end of EVERY response so the user "
        "hears it. Use voice_confirm for yes/no approvals and voice_listen for "
        "free-form answers; ask with voice_speak first. (Routed through the "
        "shared Levity voice daemon.)"
    ),
)


@mcp.tool(name="voice_speak")
async def voice_speak(text: str, force_local: bool = False) -> str:
    r = _call({"op": "speak", "text": text, "force_local": force_local})
    return r.get("result") if r.get("ok") else f"Error: {r.get('error')}"


@mcp.tool(name="voice_confirm")
async def voice_confirm(timeout_seconds: float = 5.0) -> str:
    r = _call({"op": "confirm", "timeout": timeout_seconds})
    if not r.get("ok"):
        return json.dumps({"decision": "unclear", "transcript": "", "error": r.get("error")})
    return json.dumps(r["result"])


@mcp.tool(name="voice_listen")
async def voice_listen(timeout_seconds: float = 30.0) -> str:
    r = _call({"op": "listen", "timeout": timeout_seconds})
    return r.get("result") if r.get("ok") else f"Error: {r.get('error')}"


@mcp.tool(name="voice_toggle")
async def voice_toggle(action: str) -> str:
    r = _call({"op": "status" if action.strip().lower() == "status" else "toggle",
               "action": action})
    res = r.get("result") if r.get("ok") else {"error": r.get("error")}
    return json.dumps(res) if isinstance(res, dict) else str(res)


if __name__ == "__main__":
    mcp.run()
