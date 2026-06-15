#!/usr/bin/env python3
"""
levity-voiced — the shared Levity voice daemon (SCAFFOLD, branch: multi-host-daemon).

One persistent process that owns the microphone, Whisper, and TTS. Per-host MCP
shims (levity_shim.py) forward voice requests here over a Unix-domain socket, so
Claude Desktop + Antigravity can all use one coordinated voice with no
single-instance churn.

It reuses the proven audio engine from the installed single-instance server
(~/.levity-voice/server.py) rather than duplicating it — this daemon only adds
the IPC server + cross-host coordination.

IPC protocol (one JSON object per line, over the socket):
  request : {"op": "speak"|"confirm"|"listen"|"toggle"|"status", ...params}
  reply   : {"ok": true, "result": <any>} | {"ok": false, "error": "..."}

STATUS: scaffold. Core dispatch works by delegating to the engine; the
coordination policy (speak queue vs interrupt, single-capture lock) is wired but
needs the test matrix in docs/multi-host-voice-daemon.md before merge.
"""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".levity-voice"
SOCKET_PATH = CONFIG_DIR / "voiced.sock"
PID_FILE = CONFIG_DIR / "voiced.pid"

# Reuse the installed single-instance server as the audio engine library.
sys.path.insert(0, str(CONFIG_DIR))
try:
    import server as engine  # ~/.levity-voice/server.py
except Exception as exc:  # pragma: no cover - scaffold guard
    sys.stderr.write(f"[levity-voiced] cannot import engine (~/.levity-voice/server.py): {exc!r}\n")
    raise

# Serializes capture so two hosts never open the mic at once.
_capture_lock = threading.Lock()


def _dispatch(req: dict) -> dict:
    op = (req.get("op") or "").strip().lower()

    if op == "status":
        snap = engine._get_config_snapshot()
        return {"ok": True, "result": {
            "server_active": snap.get("server_active", False),
            "response_active": snap.get("response_active", True),
            "listen_mode": snap.get("listen_mode", "quick"),
            "whisper_model": snap.get("whisper_model", engine.DEFAULT_WHISPER_MODEL),
            "platform": engine.PLATFORM,
            "daemon": True,
        }}

    if op == "speak":
        text = req.get("text", "")
        if not text.strip():
            return {"ok": True, "result": "Nothing to say."}
        snap = engine._get_config_snapshot()
        if not snap.get("response_active", True):
            return {"ok": True, "result": "Voice response is off."}
        api_key = snap.get("gemini_api_key", "")
        use_cloud = (not req.get("force_local")) and bool(api_key) and \
            len(text) >= engine.LOCAL_TTS_THRESHOLD
        engine._write_last_spoken(text)
        engine._kill_active_tts()  # interrupt policy: newest wins (see plan)
        threading.Thread(
            target=engine._speak_background,
            args=(text, use_cloud, api_key,
                  snap.get("gemini_voice", engine.DEFAULT_GEMINI_VOICE),
                  snap.get("local_voice", engine.DEFAULT_LOCAL_VOICE)),
            daemon=True,
        ).start()
        return {"ok": True, "result": "Speaking (daemon)."}

    if op in ("confirm", "listen"):
        if not engine._get_config_snapshot().get("server_active", False):
            return {"ok": False, "error": "Server inactive."}
        if not _capture_lock.acquire(blocking=False):
            return {"ok": False, "error": "Already listening (another host holds the mic)."}
        try:
            if op == "confirm":
                window = max(2.0, min(float(req.get("timeout", 5.0)), 15.0))
                text = engine._record_audio(window, engine.CONFIRM_SILENCE_SEC)
                return {"ok": True, "result": {
                    "decision": engine._parse_confirmation(text), "transcript": text}}
            window = max(3.0, min(float(req.get("timeout", 30.0)), 60.0))
            text = engine._record_audio(window, engine.LISTEN_SILENCE_SEC)
            return {"ok": True, "result": text or "(no speech detected)"}
        finally:
            _capture_lock.release()

    if op == "toggle":
        return {"ok": True, "result": engine._apply_toggle_action(req.get("action", "status"))}

    return {"ok": False, "error": f"unknown op: {op!r}"}


def _handle(conn: socket.socket) -> None:
    with conn:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        try:
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            resp = _dispatch(req)
        except Exception as exc:
            resp = {"ok": False, "error": repr(exc)}
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))


def main() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Single daemon instance.
    if SOCKET_PATH.exists():
        try:
            test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            test.settimeout(1)
            test.connect(str(SOCKET_PATH))
            test.close()
            sys.stderr.write("[levity-voiced] another daemon is already running\n")
            return 0
        except OSError:
            SOCKET_PATH.unlink(missing_ok=True)  # stale socket
    PID_FILE.write_text(str(os.getpid()))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)  # user-only
    srv.listen(8)
    sys.stderr.write(f"[levity-voiced] listening on {SOCKET_PATH}\n")
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        SOCKET_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
