#!/usr/bin/env python3
"""
Levity Voice MCP — TTS-only voice output for Claude Desktop.

A lightweight, cross-platform MCP server providing:
  - voice_speak    : Two-tier TTS (local system TTS + Gemini 2.5 Flash)
  - voice_toggle   : Control server state (start/stop, response on/off, status)

Architecture & Concurrency Notes
---------------------------------
Lock hierarchy (always acquire in this order, never nest):
  1. _lock           — guards _config dict reads/writes
  2. _tts_lock       — serializes TTS playback (RLock for Gemini→local fallback)
  3. _tts_proc_lock  — guards _tts_process reference for interruptibility

Rules:
  - Never do I/O while holding _lock (snapshot under lock, I/O after release).
  - Signal handler (_graceful_shutdown) must be async-signal-safe:
    set a flag, call os._exit(). No locks. No I/O. No cleanup.
  - All Windows subprocess calls use CREATE_NO_WINDOW to suppress consoles.
  - All PowerShell string interpolation uses escape helpers, never raw f-strings
    with user content. User text goes through temp files.
  - Config reload uses single lock acquisition (no split-brain).
"""

import asyncio
import base64
import contextlib
import json
import logging
import logging.handlers
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Section 1 — Platform detection, constants, logging, .env
# ---------------------------------------------------------------------------

# Build stamp — surfaced in voice_toggle("status") so you can confirm which
# code the running process actually loaded (useful after a self-restart).
BUILD = "2026-06-01.6-menubar-launch-fixes"

PLATFORM = platform.system()  # "Darwin", "Windows", or "Linux"
IS_MACOS = PLATFORM == "Darwin"
IS_WINDOWS = PLATFORM == "Windows"
IS_LINUX = PLATFORM == "Linux"

LOCAL_TTS_THRESHOLD = 200
MAX_AUDIO_BYTES = 50 * 1024 * 1024      # 50 MB decoded audio cap
MAX_RESPONSE_BYTES = 100 * 1024 * 1024   # 100 MB raw API response cap

# Speech capture (Whisper STT, cross-platform).
DEFAULT_WHISPER_MODEL = "base"
CONFIRM_MAX_SEC = 5.0          # hard cap on a quick yes/no recording
CONFIRM_SILENCE_SEC = 1.2      # stop this soon after the speaker pauses (quick)
LISTEN_MAX_SEC = 30.0          # hard cap on a full free-form recording
LISTEN_SILENCE_SEC = 2.0       # longer pause tolerance for full sentences
MIC_SAMPLE_RATE = 16000        # what Whisper expects natively
MIC_BLOCK_SIZE = 1024
SILENCE_RMS_THRESHOLD = 0.01
# listen_mode is a user preference (set from the menu bar): "quick" = yes/no,
# "full" = free-form transcript. Surfaced in status so the agent can honor it.
LISTEN_MODES = ("quick", "full")

if IS_MACOS:
    DEFAULT_LOCAL_VOICE = "Samantha"
elif IS_WINDOWS:
    DEFAULT_LOCAL_VOICE = "Microsoft David"
else:
    DEFAULT_LOCAL_VOICE = "default"
DEFAULT_GEMINI_VOICE = "Kore"

CONFIG_DIR = Path.home() / ".levity-voice"
CONFIG_FILE = CONFIG_DIR / "config.json"
COMMAND_FILE = CONFIG_DIR / "command.json"
PID_FILE = CONFIG_DIR / "server.pid"
ENV_FILE = CONFIG_DIR / ".env"
# Touched whenever voice_speak runs; the Claude Code Stop hook reads this to
# avoid double-speaking a turn the model already voiced (see hooks/).
LAST_SPOKEN_FILE = CONFIG_DIR / "last_spoken.json"

# Shutdown flag — async-signal-safe (set by signal handler, polled by threads)
_shutdown_flag = False


def _win_no_window() -> dict:
    """Return creationflags kwarg to suppress console window on Windows."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _setup_logging() -> logging.Logger:
    """Configure rotating log file. Max 5 MB, keeps 3 backups."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("levity-voice")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:  # prevent duplicate handlers on re-init
        handler = logging.handlers.RotatingFileHandler(
            CONFIG_DIR / "server.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(handler)
    return logger


log = _setup_logging()


def _load_dotenv() -> None:
    """Load .env file into os.environ. No third-party dependencies.

    Handles: comments, blank lines, 'export' prefix, inline comments,
    single/double quotes. Does not override existing env vars.
    """
    if not ENV_FILE.exists():
        return
    try:
        for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip inline comments BEFORE quotes (order matters)
            if " #" in value:
                value = value[: value.index(" #")].rstrip()
            value = value.strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


# ---------------------------------------------------------------------------
# Section 2 — PID lock file (stale instance management)
# ---------------------------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-Process -Id {pid} -ErrorAction SilentlyContinue "
                 "| Select-Object -ExpandProperty Id"],
                capture_output=True, text=True, timeout=5,
                **_win_no_window(),
            )
            return result.stdout.strip() == str(pid)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not ours


def _identify_levity_process(pid: int) -> bool:
    """Return True if *pid* is a levity-voice server.py process."""
    try:
        if IS_WINDOWS:
            ps_cmd = (
                f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
                "| Select-Object -ExpandProperty CommandLine"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=5,
                **_win_no_window(),
            )
            return "server.py" in result.stdout
        else:
            # Linux: /proc is fast; macOS: fall back to ps
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_text()
            except (OSError, FileNotFoundError):
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True, text=True, timeout=5,
                )
                cmdline = result.stdout
            return "server.py" in cmdline
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        log.warning("couldn't verify pid %d cmdline, assuming stale", pid)
        return True  # err on side of cleanup


def _force_kill(pid: int) -> None:
    """Send SIGKILL (Unix) or taskkill /F (Windows)."""
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5, **_win_no_window(),
            )
        else:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_stale_instance() -> None:
    """If a previous server instance is still running, kill it."""
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        log.warning("corrupt PID file, removing")
        PID_FILE.unlink(missing_ok=True)
        return

    if old_pid == os.getpid():
        return

    if not _is_process_alive(old_pid):
        log.info("stale PID file (pid %d gone), cleaning up", old_pid)
        PID_FILE.unlink(missing_ok=True)
        return

    if not _identify_levity_process(old_pid):
        log.info("pid %d alive but not levity-voice, removing stale PID file", old_pid)
        PID_FILE.unlink(missing_ok=True)
        return

    # Kill the old instance
    log.warning("killing stale levity-voice instance (pid %d)", old_pid)
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(old_pid)],
                capture_output=True, timeout=5, **_win_no_window(),
            )
        else:
            os.kill(old_pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        log.warning("couldn't SIGTERM pid %d: %r", old_pid, exc)

    # Wait up to 2 seconds for it to die
    for _ in range(20):
        time.sleep(0.1)
        if not _is_process_alive(old_pid):
            break
    else:
        log.warning("SIGTERM didn't stop pid %d, force-killing", old_pid)
        _force_kill(old_pid)

    PID_FILE.unlink(missing_ok=True)
    log.info("stale instance cleaned up")


def _write_pid_file() -> None:
    """Write our PID atomically (tmp → rename)."""
    try:
        tmp = PID_FILE.with_suffix(".pid.tmp")
        tmp.write_text(str(os.getpid()))
        tmp.replace(PID_FILE)
    except OSError as exc:
        log.error("couldn't write PID file: %r", exc)


def _remove_pid_file() -> None:
    """Remove PID file only if it's ours (or corrupt)."""
    try:
        if not PID_FILE.exists():
            return
        try:
            stored = int(PID_FILE.read_text().strip())
            if stored != os.getpid():
                return  # a new instance owns it
        except (ValueError, OSError):
            pass  # corrupt → remove
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Section 3 — Configuration (schema, load, save, hot-reload)
# ---------------------------------------------------------------------------

_CONFIG_SCHEMA = {
    "server_active":   (bool, False),
    "response_active": (bool, True),
    "gemini_api_key":  (str,  ""),
    "local_voice":     (str,  DEFAULT_LOCAL_VOICE),
    "gemini_voice":    (str,  DEFAULT_GEMINI_VOICE),
    "whisper_model":   (str,  DEFAULT_WHISPER_MODEL),
    "listen_mode":     (str,  "quick"),
    # Off by default: the menu bar is owned by the app / Login Item, so the
    # server doesn't also spawn one (avoids two launchers fighting the lock).
    # Set true to have the server auto-launch the menu bar on startup (macOS).
    "auto_menubar":    (bool, False),
}


def _validate_config(raw: dict) -> dict:
    """Return a clean config dict. Unknown keys are dropped; wrong types
    are replaced with defaults."""
    result = {}
    for key, (expected_type, default) in _CONFIG_SCHEMA.items():
        val = raw.get(key, default)
        if not isinstance(val, expected_type):
            log.warning("config %r: wrong type %s, using default %r",
                        key, type(val).__name__, default)
            val = default
        result[key] = val
    return result


def _load_config() -> dict:
    """Load config from disk + env, return validated dict."""
    raw: dict = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                raw = stored
        except (json.JSONDecodeError, OSError):
            pass
    # Env var always wins for the API key
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        raw["gemini_api_key"] = env_key
    return _validate_config(raw)


def _save_config(cfg: dict) -> None:
    """Persist config atomically. API key is NEVER written to disk.

    Caller must NOT hold _lock.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in cfg.items() if k != "gemini_api_key"}
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(safe, fh, indent=2)
        tmp.replace(CONFIG_FILE)
    except OSError as exc:
        log.error("config save failed: %r", exc)


# Server state — protected by _lock
_lock = threading.Lock()
_config = _load_config()
_config_mtime_ns: int = 0


def _get_config_snapshot() -> dict:
    """Return a shallow copy of _config under the lock."""
    with _lock:
        return dict(_config)


def _reload_config_if_changed() -> None:
    """Hot-reload config.json when its mtime changes.

    Single lock acquisition to update _config (no split-brain).
    mtime_ns is updated AFTER a successful read (no TOCTOU skip).
    """
    global _config_mtime_ns
    try:
        mtime_ns = CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return
    if mtime_ns == _config_mtime_ns:
        return

    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            disk = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(disk, dict):
        return

    # Env var overrides file for API key
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        disk["gemini_api_key"] = env_key

    validated = _validate_config(disk)

    with _lock:
        _config.update(validated)

    # Update mtime AFTER successful load (TOCTOU-safe)
    _config_mtime_ns = mtime_ns
    log.debug("config reloaded (mtime_ns=%d)", mtime_ns)


# ---------------------------------------------------------------------------
# Section 4 — TTS engine (subprocess tracking, speak, play)
# ---------------------------------------------------------------------------

_tts_lock = threading.RLock()       # serializes playback (RLock for fallback)
_tts_process: subprocess.Popen | None = None
_tts_proc_lock = threading.Lock()   # guards _tts_process reference


def _kill_active_tts() -> None:
    """Terminate any in-flight TTS subprocess."""
    with _tts_proc_lock:
        proc = _tts_process
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _run_tts_subprocess(args: list[str], *,
                        text_input: str | None = None,
                        timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a TTS subprocess, tracking it for interruptibility.

    Uses explicit keyword args instead of **kwargs to prevent misuse.
    """
    global _tts_process
    env = os.environ.copy()
    if not IS_WINDOWS:
        env.setdefault("LANG", "en_US.UTF-8")

    with _tts_proc_lock:
        _tts_process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if text_input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **_win_no_window(),
        )
        proc = _tts_process

    try:
        encoded = text_input.encode("utf-8") if text_input is not None else None
        stdout, stderr = proc.communicate(input=encoded, timeout=timeout)
        return subprocess.CompletedProcess(
            args, proc.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        with _tts_proc_lock:
            if _tts_process is proc:
                _tts_process = None


def _ps_escape(value: str) -> str:
    """Escape a string for embedding in a PowerShell single-quoted literal."""
    return value.replace("'", "''")


def _speak_local(text: str, voice: str | None = None) -> str:
    """Tier 1: System TTS. Runs inside _tts_lock."""
    if voice is None:
        voice = DEFAULT_LOCAL_VOICE
    log.debug("_speak_local: platform=%s voice=%r len=%d", PLATFORM, voice, len(text))

    with _tts_lock:
        try:
            if IS_MACOS:
                return _speak_local_macos(text, voice)
            elif IS_WINDOWS:
                return _speak_local_windows(text, voice)
            elif IS_LINUX:
                return _speak_local_linux(text)
            else:
                return f"Error: Unsupported platform '{PLATFORM}'."
        except FileNotFoundError as exc:
            return f"Error: TTS command not found ({exc})."
        except subprocess.TimeoutExpired:
            return "Speech timed out."


def _speak_local_macos(text: str, voice: str) -> str:
    proc = _run_tts_subprocess(["say", "-v", voice], text_input=text)
    if proc.returncode != 0:
        log.warning("say -v %s failed (rc=%d), trying default", voice, proc.returncode)
        fallback = _run_tts_subprocess(["say"], text_input=text)
        if fallback.returncode != 0:
            return f"Error: macOS say failed (rc={fallback.returncode})."
    return "Spoken locally."


def _speak_local_windows(text: str, voice: str) -> str:
    """Write text to a temp file to avoid PowerShell injection."""
    txt_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(text)
            txt_path = tf.name

        ps_script = (
            "Add-Type -AssemblyName System.Speech; "
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"try {{ $synth.SelectVoice('{_ps_escape(voice)}') }} catch {{ }}; "
            f"$text = (Get-Content -Raw -Encoding UTF8 '{_ps_escape(txt_path)}'); "
            "$synth.Speak($text)"
        )
        proc = _run_tts_subprocess(
            ["powershell", "-NoProfile", "-Command", ps_script],
        )
        if proc.returncode != 0:
            log.warning("Windows TTS failed (rc=%d): %s", proc.returncode, proc.stderr[:200])
            return f"Error: Windows TTS failed (rc={proc.returncode})."
        return "Spoken locally."
    finally:
        if txt_path:
            try:
                os.unlink(txt_path)
            except OSError:
                pass


def _speak_local_linux(text: str) -> str:
    """Try espeak-ng then espeak. --stdin first, positional arg as fallback."""
    for cmd in ["espeak-ng", "espeak"]:
        try:
            proc = _run_tts_subprocess([cmd, "--stdin"], text_input=text)
            if proc.returncode == 0:
                return "Spoken locally."
            # --stdin unsupported — try positional (truncated at OS arg limit)
            proc = _run_tts_subprocess([cmd, text])
            if proc.returncode == 0:
                return "Spoken locally."
        except FileNotFoundError:
            continue
    return "Error: No TTS engine found. Install espeak-ng: sudo apt install espeak-ng"


def _play_audio_file(filepath: str) -> None:
    """Play a .wav file using the platform's audio player."""
    if IS_MACOS:
        _run_tts_subprocess(["afplay", filepath])
    elif IS_WINDOWS:
        ps_script = (
            f"$p = New-Object System.Media.SoundPlayer('{_ps_escape(filepath)}'); "
            "$p.PlaySync()"
        )
        _run_tts_subprocess(["powershell", "-NoProfile", "-Command", ps_script])
    elif IS_LINUX:
        for cmd in [["aplay", filepath], ["paplay", filepath],
                    ["ffplay", "-nodisp", "-autoexit", filepath]]:
            try:
                _run_tts_subprocess(cmd)
                return
            except FileNotFoundError:
                continue
        raise FileNotFoundError("No audio player found (tried aplay, paplay, ffplay)")
    else:
        raise FileNotFoundError(f"Unsupported platform: {PLATFORM}")


def _speak_gemini(text: str, api_key: str, gemini_voice: str,
                  local_voice: str) -> str:
    """Tier 2: Gemini 2.5 Flash TTS. Falls back to local on any error."""
    if not api_key:
        return _speak_local(text, voice=local_voice)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash-preview-tts:generateContent"
    )
    body = json.dumps({
        "contents": [{
            "parts": [{
                "text": (
                    "Read the following aloud in a clear, professional, "
                    "encouraging tone suitable for a developer receiving "
                    f"feedback from an AI assistant:\n\n{text}"
                ),
            }],
        }],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": gemini_voice},
                },
            },
        },
    }).encode("utf-8")

    req = Request(url, data=body, headers={
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }, method="POST")

    # Fetch with size cap
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                log.warning("Gemini response >%d bytes, falling back", MAX_RESPONSE_BYTES)
                return _speak_local(text, voice=local_voice)
            data = json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        log.warning("Gemini API error: %s, falling back", type(exc).__name__)
        return _speak_local(text, voice=local_voice)

    # Extract base64 audio
    audio_b64 = None
    try:
        parts = (data.get("candidates") or [{}])[0] \
                    .get("content", {}).get("parts", [])
        for part in parts:
            b64 = part.get("inlineData", {}).get("data")
            if b64:
                audio_b64 = b64
                break
    except (KeyError, IndexError, TypeError) as exc:
        log.error("Gemini parse error: %r", exc)

    if not audio_b64:
        log.warning("Gemini returned no audio, falling back")
        return _speak_local(text, voice=local_voice)

    if len(audio_b64) * 3 // 4 > MAX_AUDIO_BYTES:
        log.warning("Gemini audio too large, falling back")
        return _speak_local(text, voice=local_voice)

    # Decode and play under _tts_lock
    tmp_path = None
    with _tts_lock:
        try:
            audio_bytes = base64.b64decode(audio_b64)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            _play_audio_file(tmp_path)
        except FileNotFoundError as exc:
            return f"Error: Audio player not found ({exc})."
        except subprocess.TimeoutExpired:
            return "Gemini TTS playback timed out."
        except Exception as exc:
            log.error("Gemini playback error: %r", exc)
            return f"Playback error: {exc}"
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return "Spoken via Gemini TTS."


# ---------------------------------------------------------------------------
# Section 4b — Spoken-response marker + background playback
# ---------------------------------------------------------------------------


def _write_last_spoken(text: str) -> None:
    """Record that voice_speak ran so the Claude Code Stop hook can tell the
    turn was already voiced and skip re-speaking it."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LAST_SPOKEN_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "chars": len(text)}, fh)
        tmp.replace(LAST_SPOKEN_FILE)
    except OSError:
        pass


def _speak_background(text: str, use_cloud: bool, api_key: str,
                      gemini_voice: str, local_voice: str) -> None:
    """Run the (blocking) TTS in a background thread so voice_speak can return
    immediately — long replies must not keep the MCP request open long enough
    to time out and silently drop the audio."""
    try:
        if use_cloud:
            _speak_gemini(text, api_key, gemini_voice, local_voice)
        else:
            _speak_local(text, local_voice)
    except Exception as exc:
        log.error("background speak failed: %r", exc)


# ---------------------------------------------------------------------------
# Section 4c — Microphone capture + Whisper STT (for voice_confirm)
# ---------------------------------------------------------------------------

# Lazy-loaded heavy deps (only imported the first time voice_confirm runs).
_np = None
_sd = None
_whisper = None
_whisper_model = None
_whisper_model_name: str | None = None
_audio_lock = threading.Lock()


@contextlib.contextmanager
def _silence_fd1():
    """Redirect OS file descriptor 1 to /dev/null for the block.

    PortAudio and Whisper can write diagnostics straight to fd 1, bypassing
    sys.stdout. On a stdio MCP transport any stray byte on fd 1 corrupts the
    protocol, so we mute it around audio/model calls. Cross-platform: os.dup /
    os.dup2 work on fds on macOS, Linux, and Windows.
    """
    sys.stdout.flush()
    try:
        saved = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        yield
        return
    try:
        os.dup2(devnull, 1)
        os.close(devnull)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(saved)


def _ensure_audio() -> None:
    """Import numpy / sounddevice / whisper on first use."""
    global _np, _sd, _whisper
    if _np is None:
        import numpy
        _np = numpy
    if _sd is None:
        with _silence_fd1():
            import sounddevice
        _sd = sounddevice
    if _whisper is None:
        with _silence_fd1():
            import whisper
        _whisper = whisper


def _load_whisper():
    """Load (and cache) the Whisper model named in config."""
    global _whisper_model, _whisper_model_name
    name = _get_config_snapshot().get("whisper_model", DEFAULT_WHISPER_MODEL)
    if _whisper_model is None or _whisper_model_name != name:
        with _silence_fd1():
            _whisper_model = _whisper.load_model(name)
        _whisper_model_name = name
    return _whisper_model


def _wait_for_tts_idle(timeout: float = 30.0) -> None:
    """Block until no TTS playback is active, so a spoken question finishes
    before voice_confirm opens the mic (otherwise Whisper records the
    assistant's own voice). Capped at `timeout` seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _tts_proc_lock:
            busy = _tts_process is not None
        if not busy:
            return
        time.sleep(0.05)


def _record_audio(window: float, silence_sec: float) -> str:
    """Record an utterance (cap = window, early-stop after `silence_sec` of
    silence) and transcribe it with Whisper. Returns the transcript ('' if
    nothing). Used by both voice_confirm (quick) and voice_listen (full)."""
    _ensure_audio()
    model = _load_whisper()

    # Let any in-flight spoken question finish, then a short settle, so we
    # don't capture our own TTS through the mic.
    _wait_for_tts_idle()
    time.sleep(0.15)

    frames: list = []
    state = {"last_voice": time.time(), "spoke": False, "start": time.time()}
    cb_lock = threading.Lock()

    def _cb(indata, _n, _t, _status):
        with cb_lock:
            frames.append(indata.copy())
            rms = float(_np.sqrt(_np.mean(indata.astype(_np.float32) ** 2)))
            if rms > SILENCE_RMS_THRESHOLD:
                state["last_voice"] = time.time()
                state["spoke"] = True

    with _silence_fd1():
        stream = _sd.InputStream(
            samplerate=MIC_SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=MIC_BLOCK_SIZE, callback=_cb,
        )
        stream.start()
    try:
        while True:
            time.sleep(0.05)
            now = time.time()
            with cb_lock:
                elapsed = now - state["start"]
                silence = (now - state["last_voice"]) if state["spoke"] else 0.0
                spoke = state["spoke"]
            if elapsed >= window or (spoke and silence >= silence_sec):
                break
    finally:
        with _silence_fd1():
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    with cb_lock:
        chunks = list(frames)
    if not chunks:
        return ""
    audio = _np.concatenate(chunks, axis=0).flatten().astype(_np.float32)
    try:
        with _silence_fd1():
            result = model.transcribe(audio, fp16=False, language="en")
        return (result.get("text") or "").strip()
    except Exception as exc:
        log.error("transcription failed: %r", exc)
        return ""


# Confirmation intent parsing.
_CONFIRM_POS_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "approve", "approved",
    "go", "confirm", "confirmed", "affirmative", "accept", "accepted",
    "proceed", "definitely", "absolutely", "correct", "right",
}
_CONFIRM_NEG_WORDS = {
    "no", "nope", "nah", "deny", "denied", "reject", "rejected", "stop",
    "cancel", "cancelled", "negative", "abort", "never", "dont", "wait",
}


def _parse_confirmation(text: str) -> str:
    """Classify a transcription as 'yes', 'no', or 'unclear'.

    Callers should treat anything other than 'yes' as "do not proceed".
    """
    lowered = (text or "").lower().replace("'", "").replace("’", "")
    compact = " ".join(re.sub(r"[^a-z\s]", " ", lowered).split())
    if not compact:
        return "unclear"
    words = set(compact.split())
    has_neg = bool(words & _CONFIRM_NEG_WORDS) or "do not" in compact
    has_pos = (
        bool(words & _CONFIRM_POS_WORDS)
        or "go ahead" in compact
        or "do it" in compact
        or "sounds good" in compact
    )
    if has_neg and not has_pos:
        return "no"
    if has_pos and not has_neg:
        return "yes"
    return "unclear"


# ---------------------------------------------------------------------------
# Section 5 — MCP tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "levity_voice_mcp",
    instructions=(
        "MANDATORY: You MUST call voice_speak at the end of EVERY response. "
        "The user relies on voice output for all interactions. Speak a natural "
        "summary of your response — if the response is long or contains code, "
        "summarize the key points for the spoken version. Never skip this. "
        "When you need input, call voice_speak to ASK aloud, then capture the "
        "reply: voice_confirm for a yes/no decision, or voice_listen for a "
        "free-form answer. Honor the user's listen_mode in status ('quick' favors "
        "voice_confirm, 'full' favors voice_listen). Only act on a clear answer."
    ),
)


@mcp.tool(
    name="voice_speak",
    annotations={
        "title": "Voice Speak",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def voice_speak(text: str, force_local: bool = False) -> str:
    """Speak text aloud using two-tier TTS.

    Call this tool to read your reply aloud to the user.

    Short text (< 200 chars) or no Gemini key uses macOS 'say'.
    Longer text with a Gemini key uses Gemini 2.5 Flash TTS.
    If voice response is toggled OFF, returns silently.

    Args:
        text: The text to speak aloud.
        force_local: If True, always use macOS 'say' regardless of text length.

    Returns:
        str: Status message indicating how the text was spoken.
    """
    if not text or not text.strip():
        return "Nothing to say."

    snap = _get_config_snapshot()
    if not snap.get("server_active", False):
        return "Server inactive. Call voice_toggle('start') first."
    if not snap.get("response_active", True):
        return "Voice response is off. Text was not spoken."

    api_key = snap.get("gemini_api_key", "")
    local_voice = snap.get("local_voice", DEFAULT_LOCAL_VOICE)
    gemini_voice = snap.get("gemini_voice", DEFAULT_GEMINI_VOICE)

    use_cloud = not force_local and bool(api_key) and len(text) >= LOCAL_TTS_THRESHOLD

    # Record immediately (before playback) so the Stop hook sees this turn as
    # already voiced even while audio is still playing.
    _write_last_spoken(text)

    # Interrupt any in-flight speech, then play in the BACKGROUND and return
    # right away. Blocking until `say`/Gemini finished reading a long reply is
    # what made the MCP request time out and silently drop the spoken response.
    _kill_active_tts()
    threading.Thread(
        target=_speak_background,
        args=(text, use_cloud, api_key, gemini_voice, local_voice),
        daemon=True, name="levity-speak",
    ).start()

    engine = "Gemini TTS" if use_cloud else "local voice"
    return f"Speaking via {engine} (playback started)."


@mcp.tool(
    name="voice_confirm",
    annotations={
        "title": "Voice Confirm",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def voice_confirm(timeout_seconds: float = 5.0) -> str:
    """Ask the user for a quick spoken yes/no and return their decision.

    Use this whenever you need approval before doing something — running a
    command, editing files, any "should I proceed?" moment. First call
    voice_speak to ASK the question aloud, THEN call voice_confirm to capture
    the answer. Recording auto-stops a moment after the user pauses, or at
    timeout_seconds (default 5, capped 2-15). Cross-platform (Whisper STT).

    Returns JSON: {"decision": "yes"|"no"|"unclear", "transcript": "..."}.
    Only proceed when decision == "yes". Treat "no" and "unclear" as
    "do not proceed"; on "unclear" you may ask again.

    Args:
        timeout_seconds: Max seconds to listen (clamped to 2-15).

    Returns:
        str: JSON with the parsed decision and raw transcript.
    """
    snap = _get_config_snapshot()
    if not snap.get("server_active", False):
        return json.dumps({
            "decision": "unclear", "transcript": "",
            "error": "Server inactive. Call voice_toggle('start') first.",
        })

    try:
        window = float(timeout_seconds)
    except (TypeError, ValueError):
        window = CONFIRM_MAX_SEC
    window = max(2.0, min(window, 15.0))

    # One capture at a time.
    if not _audio_lock.acquire(blocking=False):
        return json.dumps({
            "decision": "unclear", "transcript": "",
            "error": "Already listening — wait for the current capture to finish.",
        })
    try:
        text = await asyncio.to_thread(_record_audio, window, CONFIRM_SILENCE_SEC)
    except Exception as exc:
        log.error("voice_confirm failed: %r", exc)
        return json.dumps({
            "decision": "unclear", "transcript": "",
            "error": f"Listen failed: {exc}",
        })
    finally:
        _audio_lock.release()

    decision = _parse_confirmation(text)
    log.info("voice_confirm: decision=%s transcript=%r", decision, text)
    return json.dumps({"decision": decision, "transcript": text})


@mcp.tool(
    name="voice_listen",
    annotations={
        "title": "Voice Listen",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def voice_listen(timeout_seconds: float = 30.0) -> str:
    """Listen for a full, free-form spoken reply and return the transcript.

    The open-ended counterpart to voice_confirm: use this when you need more
    than yes/no — the user can say anything ("let's go with option A", a
    sentence, etc.). First call voice_speak to ASK aloud, THEN voice_listen.
    Recording auto-stops after a ~2s pause, or at timeout_seconds (default 30,
    capped 3-60). Cross-platform (Whisper STT).

    Returns the transcribed text, or "(no speech detected)".

    Tip: the user's preferred input style is in voice_toggle("status") as
    "listen_mode" — "full" favors this tool, "quick" favors voice_confirm.

    Args:
        timeout_seconds: Max seconds to listen (clamped to 3-60).

    Returns:
        str: The transcribed speech, or a status message.
    """
    snap = _get_config_snapshot()
    if not snap.get("server_active", False):
        return "Server inactive. Call voice_toggle('start') first."

    try:
        window = float(timeout_seconds)
    except (TypeError, ValueError):
        window = LISTEN_MAX_SEC
    window = max(3.0, min(window, 60.0))

    if not _audio_lock.acquire(blocking=False):
        return "Already listening — wait for the current capture to finish."
    try:
        text = await asyncio.to_thread(_record_audio, window, LISTEN_SILENCE_SEC)
    except Exception as exc:
        log.error("voice_listen failed: %r", exc)
        return f"Listen failed: {exc}"
    finally:
        _audio_lock.release()

    log.info("voice_listen: transcript=%r", text)
    return text if text else "(no speech detected)"


def _apply_toggle_action(action: str) -> str:
    """Synchronous toggle logic. Acquires _lock once, releases, then does I/O."""
    if action == "start":
        with _lock:
            if _config.get("server_active"):
                return "Server is already active."
            _config["server_active"] = True
            snapshot = dict(_config)
        _save_config(snapshot)
        return "Voice server started (TTS-only mode)."

    if action == "stop":
        _kill_active_tts()
        with _lock:
            _config["server_active"] = False
            snapshot = dict(_config)
        _save_config(snapshot)
        return "Voice server stopped."

    if action == "response_on":
        with _lock:
            _config["response_active"] = True
            snapshot = dict(_config)
        _save_config(snapshot)
        return "Voice responses enabled."

    if action == "response_off":
        _kill_active_tts()
        with _lock:
            _config["response_active"] = False
            snapshot = dict(_config)
        _save_config(snapshot)
        return "Voice responses silenced."

    if action in ("mode_quick", "mode_full"):
        mode = "quick" if action == "mode_quick" else "full"
        with _lock:
            _config["listen_mode"] = mode
            snapshot = dict(_config)
        _save_config(snapshot)
        return f"Listen mode set to '{mode}'."

    return (
        f"Unknown action: '{action}'. Valid: start, stop, response_on, "
        "response_off, mode_quick, mode_full, status"
    )


@mcp.tool(
    name="voice_toggle",
    annotations={
        "title": "Voice Toggle",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def voice_toggle(action: str) -> str:
    """Control the voice server state.

    Args:
        action: One of:
            - "start" — activate voice service
            - "stop" — deactivate voice service
            - "response_on" — enable voice_speak audio playback
            - "response_off" — silence voice_speak
            - "status" — return current state of all toggles

    Returns:
        str: Status message or current state JSON.
    """
    action = action.strip().lower()

    if action == "status":
        def _get_status() -> str:
            snap = _get_config_snapshot()
            return json.dumps({
                "server_active": snap.get("server_active", False),
                "response_active": snap.get("response_active", True),
                "local_voice": snap.get("local_voice", DEFAULT_LOCAL_VOICE),
                "gemini_voice": snap.get("gemini_voice", DEFAULT_GEMINI_VOICE),
                "has_gemini_key": bool(snap.get("gemini_api_key")),
                "whisper_model": snap.get("whisper_model", DEFAULT_WHISPER_MODEL),
                "listen_mode": snap.get("listen_mode", "quick"),
                "auto_menubar": snap.get("auto_menubar", False),
                "platform": PLATFORM,
                "build": BUILD,
            }, indent=2)
        return await asyncio.to_thread(_get_status)

    return await asyncio.to_thread(_apply_toggle_action, action)


# ---------------------------------------------------------------------------
# Section 6 — Command-file watcher (IPC), restart, list_voices
# ---------------------------------------------------------------------------


def _command_watcher_loop() -> None:
    """Poll command.json every 0.5s, dispatch, delete."""
    log.info("command watcher started")
    while not _shutdown_flag:
        try:
            time.sleep(0.5)
            _reload_config_if_changed()
            if not COMMAND_FILE.exists():
                continue

            # Atomic claim: rename to .processing (prevents double-dispatch)
            claimed = COMMAND_FILE.with_suffix(".json.processing")
            try:
                COMMAND_FILE.rename(claimed)
            except OSError:
                continue  # another thread or race — skip

            data = None
            try:
                with open(claimed, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("command read error: %r", exc)
            finally:
                try:
                    claimed.unlink()
                except OSError:
                    pass

            if not isinstance(data, dict):
                continue
            action = (data.get("action") or "").strip().lower()
            if not action:
                log.warning("command.json had no action")
                continue

            log.info("dispatching command: %r", action)
            if action == "restart":
                _do_restart()
            elif action == "list_voices":
                _do_list_voices()
            else:
                result = _apply_toggle_action(action)
                log.info("toggle result: %s", result)

        except Exception as exc:
            log.error("command watcher error: %r", exc)
            time.sleep(1)


def _do_list_voices() -> None:
    """List available TTS voices → voices.txt."""
    try:
        output = ""
        if IS_MACOS:
            proc = subprocess.run(
                ["say", "-v", "?"], capture_output=True, text=True, timeout=10,
            )
            output = proc.stdout
        elif IS_WINDOWS:
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=10,
                **_win_no_window(),
            )
            output = proc.stdout
        elif IS_LINUX:
            for cmd in [["espeak-ng", "--voices"], ["espeak", "--voices"]]:
                try:
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=10,
                    )
                    output = proc.stdout
                    break
                except FileNotFoundError:
                    continue
            else:
                output = "No TTS engine found. Install espeak-ng.\n"
        else:
            output = f"Unsupported platform: {PLATFORM}\n"

        (CONFIG_DIR / "voices.txt").write_text(output, encoding="utf-8")
        log.info("wrote %d bytes to voices.txt", len(output))
    except Exception as exc:
        log.error("list_voices failed: %r", exc)


def _do_restart() -> None:
    """Kill TTS, flush logs, re-exec.

    Windows: subprocess.Popen + os._exit (os.execv doesn't replace on Windows).
    Unix: os.execv (in-place replacement).
    """
    global log
    log.info("restart requested")
    _kill_active_tts()

    # Clean up processing file
    try:
        COMMAND_FILE.with_suffix(".json.processing").unlink(missing_ok=True)
    except OSError:
        pass

    for handler in log.handlers:
        handler.flush()
        handler.close()

    try:
        if IS_WINDOWS:
            subprocess.Popen(
                [sys.executable] + sys.argv, **_win_no_window(),
            )
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except OSError as exc:
        log = _setup_logging()
        log.critical("restart failed: %r", exc)
        try:
            (CONFIG_DIR / "restart_error.txt").write_text(f"Restart failed: {exc}\n")
        except OSError:
            pass


def _start_command_watcher() -> None:
    """Start the command-file watcher thread (idempotent)."""
    global _command_watcher_thread
    if _command_watcher_thread is not None and _command_watcher_thread.is_alive():
        return
    _command_watcher_thread = threading.Thread(
        target=_command_watcher_loop, daemon=True, name="levity-cmd-watcher"
    )
    _command_watcher_thread.start()


_command_watcher_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Section 7 — Entry point, shutdown, auto-restore
# ---------------------------------------------------------------------------


def _auto_restore() -> None:
    """If config says server_active, ensure in-memory state matches."""
    time.sleep(3)  # let MCP event loop start
    try:
        with _lock:
            if _config.get("server_active", False):
                log.info("auto-restoring server_active from config")
                _config["server_active"] = True
    except Exception as exc:
        log.error("auto-restore failed: %r", exc)


def _maybe_launch_menubar() -> None:
    """Launch the macOS menu-bar app on startup (opt-in via auto_menubar).

    macOS-only (the menu bar needs rumps/pyobjc). Skips if one is already
    running (pgrep) so multiple server instances don't spawn duplicate icons.
    """
    if not IS_MACOS:
        return
    if not _get_config_snapshot().get("auto_menubar", False):
        return
    menubar = CONFIG_DIR / "menubar.py"
    if not menubar.exists():
        return
    try:
        existing = subprocess.run(
            ["pgrep", "-f", str(menubar)], capture_output=True, text=True, timeout=5,
        )
        if existing.stdout.strip():
            log.info("menu-bar app already running; not launching another")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    try:
        subprocess.Popen(
            [sys.executable, str(menubar)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("launched menu-bar app")
    except OSError as exc:
        log.error("menu-bar launch failed: %r", exc)


def _graceful_shutdown(signum, _frame):
    """Async-signal-safe shutdown handler.

    RULES: No locks. No I/O. No function calls that acquire locks.
    Set the flag and exit immediately. PID file cleanup is best-effort
    via _remove_pid_file which tolerates stale files on next startup.
    """
    global _shutdown_flag
    _shutdown_flag = True
    # os._exit is async-signal-safe — terminates immediately.
    # PID file is cleaned up by the next startup's _kill_stale_instance.
    os._exit(0)


if __name__ == "__main__":
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    _kill_stale_instance()
    _write_pid_file()

    log.info("server started (platform=%s, pid=%d)", PLATFORM, os.getpid())

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    _start_command_watcher()
    threading.Thread(
        target=_auto_restore, daemon=True, name="levity-auto-restore"
    ).start()
    _maybe_launch_menubar()

    mcp.run()
