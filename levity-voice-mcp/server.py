#!/usr/bin/env python3
"""
Levity Voice MCP — gives Claude Desktop voice capabilities.

A lightweight MCP server providing:
  - voice_listen   : Record from mic, transcribe with Whisper
  - voice_speak    : Two-tier TTS (macOS say + Gemini 2.5 Flash)
  - voice_toggle   : Control server state (start/stop, wake-word, response)
  - voice_check    : Poll for wake-word triggered transcriptions

All heavy imports (whisper, sounddevice, openwakeword) are lazy-loaded
only when the server is activated via voice_toggle("start").
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import base64
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# stdout suppression for native libraries
# ---------------------------------------------------------------------------
# PortAudio (via sounddevice) and other C-level libraries write diagnostic
# messages like "||PaMacCore..." directly to fd 1, bypassing Python's
# sys.stdout. Any non-JSON byte on stdout corrupts the MCP stdio protocol.
# We dup fd 1 to /dev/null around every audio/model call that might print.

@contextlib.contextmanager
def _silence_stdout_fd():
    """Redirect fd 1 to /dev/null for the duration of the block.

    Why fd-level (not sys.stdout): PortAudio writes via the C runtime's
    stdout, which `sys.stdout` redirection does not catch.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        os.close(devnull)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(saved)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "float32"
BLOCK_SIZE = 512
SILENCE_RMS_THRESHOLD = 0.01
SILENCE_DURATION_SEC = 10.0
MAX_RECORDING_SEC = 60.0
DEFAULT_WHISPER_MODEL = "base"
LOCAL_TTS_THRESHOLD = 200
DEFAULT_LOCAL_VOICE = "Samantha"
DEFAULT_GEMINI_VOICE = "Kore"
DEFAULT_WAKEWORD_KEYWORD = "hey_jarvis"

# OpenWakeWord operates on 16 kHz int16 audio in 1280-sample chunks (80 ms).
WAKEWORD_SAMPLE_RATE = 16000
WAKEWORD_FRAME_LENGTH = 1280
WAKEWORD_THRESHOLD = 0.3
WAKEWORD_BUILTIN_MODELS = ("alexa", "hey_mycroft", "hey_jarvis", "hey_rhasspy")

CONFIG_DIR = Path.home() / ".levity-voice"
CONFIG_FILE = CONFIG_DIR / "config.json"
COMMAND_FILE = CONFIG_DIR / "command.json"

# ---------------------------------------------------------------------------
# Persistent config
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Load config from disk, returning defaults if missing."""
    defaults = {
        "server_active": False,
        "wakeword_active": False,
        "response_active": True,
        "gemini_api_key": "",
        "wakeword_keyword": DEFAULT_WAKEWORD_KEYWORD,
        "whisper_model": DEFAULT_WHISPER_MODEL,
        "local_voice": DEFAULT_LOCAL_VOICE,
        "gemini_voice": DEFAULT_GEMINI_VOICE,
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            defaults.update(stored)
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def _save_config(cfg: dict) -> None:
    """Persist config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Server state (module-level singletons)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_config = _load_config()

# Lazy-loaded heavy modules
_np = None
_sd = None
_whisper = None
_openwakeword = None

# Runtime objects — only populated when server is active
_whisper_model = None
_mic_stream = None

# Recording state
_recording = False
_stop_requested = False
_has_spoken = False
_record_start = 0.0
_last_voice_time = 0.0
_audio_buffer: list = []

# Wake-word state
_wakeword_handle = None
_wakeword_stop_event = threading.Event()
_wakeword_thread = None

# Pending wake-word transcriptions for voice_check
_pending_transcriptions: list[str] = []

# Monitor thread
_monitor_thread = None
_monitor_stop = threading.Event()

# Command-file watcher (menu bar app → server IPC)
_command_watcher_thread = None

# Set when a command-driven "listen" wants the monitor to queue the result.
_queue_pending_listen = False

# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------


def _ensure_imports():
    """Import heavy deps on first use."""
    global _np, _sd, _whisper, _openwakeword
    if _np is None:
        import numpy
        _np = numpy
    if _sd is None:
        with _silence_stdout_fd():
            import sounddevice
        _sd = sounddevice
    if _whisper is None:
        with _silence_stdout_fd():
            import whisper
        _whisper = whisper
    # OpenWakeWord is optional
    if _openwakeword is None:
        try:
            with _silence_stdout_fd():
                import openwakeword
                from openwakeword.model import Model as _OWWModel
                from openwakeword.utils import download_models as _oww_download
            openwakeword.Model = _OWWModel
            openwakeword.download_models = _oww_download
            _openwakeword = openwakeword
        except ImportError:
            _openwakeword = False  # sentinel: tried but unavailable


# ---------------------------------------------------------------------------
# Audio helpers (ported from voice_worker.py)
# ---------------------------------------------------------------------------


def _audio_callback(indata, frames, time_info, status):
    """sounddevice InputStream callback — accumulates audio while recording."""
    global _last_voice_time, _has_spoken
    with _lock:
        if not _recording:
            return
        _audio_buffer.append(indata.copy())
        rms = float(_np.sqrt(_np.mean(indata.astype(_np.float32) ** 2)))
        if rms > SILENCE_RMS_THRESHOLD:
            _last_voice_time = time.time()
            _has_spoken = True


def _start_mic_stream():
    """Open a persistent mic stream for recording."""
    global _mic_stream
    if _mic_stream is not None:
        return
    with _silence_stdout_fd():
        _mic_stream = _sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            callback=_audio_callback,
        )
        _mic_stream.start()


def _stop_mic_stream():
    """Close the mic stream."""
    global _mic_stream
    if _mic_stream is not None:
        try:
            with _silence_stdout_fd():
                _mic_stream.stop()
                _mic_stream.close()
        except Exception:
            pass
        _mic_stream = None


def _transcribe_buffer() -> str:
    """Concatenate buffered audio and run Whisper. Returns transcription text."""
    with _lock:
        chunks = list(_audio_buffer)
        _audio_buffer.clear()
    if not chunks:
        return ""
    audio = _np.concatenate(chunks, axis=0).flatten().astype(_np.float32)
    try:
        with _silence_stdout_fd():
            result = _whisper_model.transcribe(audio, fp16=False, language="en")
        return (result.get("text") or "").strip()
    except Exception as e:
        return f"[Transcription error: {e}]"


def _monitor_loop():
    """Background thread that finalizes recordings on silence / timeout.

    For wake-word and command-driven 'listen' recordings, the monitor also
    transcribes and queues the result. For voice_listen, it leaves the buffer
    alone so the caller can transcribe synchronously.
    """
    global _recording, _stop_requested, _has_spoken, _queue_pending_listen

    while not _monitor_stop.is_set():
        time.sleep(0.1)
        should_finalize_and_queue = False

        with _lock:
            if not _recording:
                continue
            now = time.time()
            total_elapsed = now - _record_start
            silence_elapsed = (now - _last_voice_time) if _has_spoken else 0.0
            if (
                _stop_requested
                or (_has_spoken and silence_elapsed >= _config.get("silence_duration", SILENCE_DURATION_SEC))
                or total_elapsed >= MAX_RECORDING_SEC
            ):
                _recording = False
                _stop_requested = False
                if _config.get("wakeword_active") or _queue_pending_listen:
                    should_finalize_and_queue = True
                    _queue_pending_listen = False

        if should_finalize_and_queue:
            text = _transcribe_buffer()
            if text:
                with _lock:
                    _pending_transcriptions.append(text)


def _start_monitor():
    """Start the background recording monitor thread."""
    global _monitor_thread
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return
    _monitor_stop.clear()
    _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
    _monitor_thread.start()


def _stop_monitor():
    """Stop the background recording monitor thread."""
    _monitor_stop.set()


# ---------------------------------------------------------------------------
# Wake-word helpers (OpenWakeWord)
# ---------------------------------------------------------------------------


def _resolve_wakeword_model(keyword: str) -> tuple[str, str, str]:
    """Resolve a keyword config value to an OpenWakeWord model spec.

    Returns (model_spec, label, note). `model_spec` is what gets passed to the
    Model constructor (either a built-in name or a file path). `label` is the
    user-facing keyword that was selected. `note` is empty unless we fell back.
    """
    if keyword and os.path.isfile(keyword):
        return keyword, os.path.basename(keyword).rsplit(".", 1)[0], ""
    if keyword in WAKEWORD_BUILTIN_MODELS:
        return keyword, keyword, ""
    fallback = DEFAULT_WAKEWORD_KEYWORD
    note = (
        f" (no pretrained model for '{keyword}'; using built-in '{fallback}' — "
        f"set wakeword_keyword to a built-in name "
        f"({', '.join(WAKEWORD_BUILTIN_MODELS)}) or a path to a custom .onnx model)"
    )
    return fallback, fallback, note


def _start_wakeword():
    """Start OpenWakeWord detection in a background thread."""
    global _wakeword_handle, _wakeword_thread

    _stop_wakeword()  # clean up any prior instance

    if _openwakeword is False or _openwakeword is None:
        return "openwakeword is not installed. Install it to use wake-word detection."

    keyword = _config.get("wakeword_keyword", DEFAULT_WAKEWORD_KEYWORD)
    model_spec, label, note = _resolve_wakeword_model(keyword)

    if model_spec in WAKEWORD_BUILTIN_MODELS:
        try:
            with _silence_stdout_fd():
                _openwakeword.download_models([model_spec])
        except Exception as e:
            return f"Failed to download OpenWakeWord model '{model_spec}': {e}"

    try:
        with _silence_stdout_fd():
            _wakeword_handle = _openwakeword.Model(
                wakeword_models=[model_spec],
                inference_framework="onnx",
            )
    except Exception as e:
        return f"OpenWakeWord init failed: {e}"

    _wakeword_stop_event.clear()

    def wakeword_loop():
        global _recording, _stop_requested, _has_spoken, _record_start, _last_voice_time

        handle = _wakeword_handle
        if handle is None:
            return

        try:
            with _silence_stdout_fd():
                ww_stream = _sd.InputStream(
                    samplerate=WAKEWORD_SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=WAKEWORD_FRAME_LENGTH,
                )
                ww_stream.start()
        except Exception as e:
            print(f"[levity-voice] wake-word stream init failed: {e!r}", file=sys.stderr, flush=True)
            return

        print("[levity-voice] wake-word loop started", file=sys.stderr, flush=True)
        last_score_log = time.time()
        max_score_since_log = 0.0
        max_label_since_log = ""

        try:
            while not _wakeword_stop_event.is_set():
                with _silence_stdout_fd():
                    data, _overflowed = ww_stream.read(WAKEWORD_FRAME_LENGTH)
                pcm = data.flatten()
                with _silence_stdout_fd():
                    scores = handle.predict(pcm)
                if scores:
                    frame_label, frame_score = max(scores.items(), key=lambda kv: kv[1])
                    if frame_score > max_score_since_log:
                        max_score_since_log = frame_score
                        max_label_since_log = frame_label
                now_log = time.time()
                if now_log - last_score_log >= 2.0:
                    print(
                        f"[levity-voice] wake-word max score (last ~2s): "
                        f"{max_label_since_log or '<none>'}={max_score_since_log:.3f} "
                        f"(threshold={WAKEWORD_THRESHOLD})",
                        file=sys.stderr,
                        flush=True,
                    )
                    last_score_log = now_log
                    max_score_since_log = 0.0
                    max_label_since_log = ""
                if any(score >= WAKEWORD_THRESHOLD for score in scores.values()):
                    triggered = False
                    with _lock:
                        if not _recording:
                            _audio_buffer.clear()
                            _recording = True
                            _stop_requested = False
                            _has_spoken = False
                            now = time.time()
                            _record_start = now
                            _last_voice_time = now
                            triggered = True
                    if triggered:
                        # Clear OWW's internal state so the same utterance
                        # doesn't immediately retrigger on the next frame.
                        try:
                            handle.reset()
                        except Exception:
                            pass
        except Exception as e:
            print(f"[levity-voice] wake-word loop exception: {e!r}", file=sys.stderr, flush=True)
        finally:
            print("[levity-voice] wake-word loop exiting", file=sys.stderr, flush=True)
            try:
                with _silence_stdout_fd():
                    ww_stream.stop()
                    ww_stream.close()
            except Exception:
                pass

    _wakeword_thread = threading.Thread(target=wakeword_loop, daemon=True)
    _wakeword_thread.start()

    with _lock:
        _config["wakeword_active"] = True
        _save_config(_config)

    return f"Wake-word detection started (keyword: '{label}'){note}"


def _stop_wakeword():
    """Stop OpenWakeWord detection."""
    global _wakeword_handle, _wakeword_thread

    _wakeword_stop_event.set()

    # OpenWakeWord Model has no explicit close method; just drop the handle.
    _wakeword_handle = None
    _wakeword_thread = None

    with _lock:
        _config["wakeword_active"] = False
        _save_config(_config)


# ---------------------------------------------------------------------------
# TTS helpers (ported from ttsProvider.ts)
# ---------------------------------------------------------------------------


def _speak_local(text: str) -> str:
    """Tier 1: macOS say command."""
    voice = _config.get("local_voice", DEFAULT_LOCAL_VOICE)
    try:
        proc = subprocess.run(
            ["say", "-v", voice],
            input=text,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            # Fallback to default voice
            subprocess.run(
                ["say"],
                input=text,
                capture_output=True,
                text=True,
                timeout=120,
            )
    except FileNotFoundError:
        return "Error: 'say' command not found. This feature requires macOS."
    except subprocess.TimeoutExpired:
        return "Speech timed out."
    return "Spoken locally."


def _speak_gemini(text: str) -> str:
    """Tier 2: Gemini 2.5 Flash TTS via REST API."""
    api_key = _config.get("gemini_api_key", "")
    if not api_key:
        return _speak_local(text)

    gemini_voice = _config.get("gemini_voice", DEFAULT_GEMINI_VOICE)

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash-preview-tts:generateContent?key={api_key}"
    )

    body = json.dumps({
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Read the following aloud in a clear, professional, "
                            "encouraging tone suitable for a developer receiving "
                            f"feedback from an AI assistant:\n\n{text}"
                        ),
                    }
                ],
            }
        ],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": gemini_voice,
                    },
                },
            },
        },
    }).encode("utf-8")

    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as e:
        # Fall back to local TTS
        return _speak_local(text)

    # Extract base64 audio from Gemini response
    audio_b64 = None
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData", {})
        if inline.get("data"):
            audio_b64 = inline["data"]
            break

    if not audio_b64:
        return _speak_local(text)

    # Write to temp file and play with afplay (macOS)
    try:
        audio_bytes = base64.b64decode(audio_b64)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        subprocess.run(
            ["afplay", tmp_path],
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError:
        return "Error: 'afplay' not found. Gemini TTS requires macOS."
    except Exception as e:
        return f"Playback error: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    return "Spoken via Gemini TTS."


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "levity_voice_mcp",
    instructions=(
        "When the user speaks to you via voice_listen or voice_check, always "
        "call voice_speak with your complete response so they hear it. The "
        "user is interacting hands-free via voice and expects a spoken reply."
    ),
)


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
async def voice_listen() -> str:
    """Record audio from the microphone, detect silence, and transcribe with Whisper.

    Blocks during recording until the user stops speaking (silence detection)
    or the maximum recording duration is reached. Returns the transcribed text.

    IMPORTANT: After receiving the transcription, you MUST call voice_speak with
    your response so the user hears it aloud. The user is interacting via voice
    and expects a spoken reply.

    Returns:
        str: The transcribed speech text, or an error/status message.
    """
    global _recording, _stop_requested, _has_spoken, _record_start, _last_voice_time

    with _lock:
        if not _config.get("server_active"):
            return "Server inactive. Call voice_toggle with action 'start' first."
        if _recording:
            return "Already recording. Wait for current recording to finish."

    await asyncio.to_thread(_ensure_imports)

    # Make sure mic stream and monitor are running
    await asyncio.to_thread(_start_mic_stream)
    _start_monitor()

    # Start recording
    with _lock:
        _audio_buffer.clear()
        _recording = True
        _stop_requested = False
        _has_spoken = False
        now = time.time()
        _record_start = now
        _last_voice_time = now

    # Block until recording finishes (monitor thread handles silence detection)
    while True:
        await asyncio.sleep(0.1)
        with _lock:
            if not _recording:
                break

    text = await asyncio.to_thread(_transcribe_buffer)
    return text if text else "(No speech detected)"


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

    Call this tool to read your reply aloud to the user. When the user
    interacts via voice_listen or voice_check, always speak your response.

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

    with _lock:
        if not _config.get("response_active", True):
            return "Voice response is off. Text was not spoken."

    has_gemini = bool(_config.get("gemini_api_key"))
    use_cloud = (
        not force_local
        and has_gemini
        and len(text) >= LOCAL_TTS_THRESHOLD
    )

    if use_cloud:
        return await asyncio.to_thread(_speak_gemini, text)
    else:
        return await asyncio.to_thread(_speak_local, text)


def _apply_toggle_action(action: str) -> str:
    """Synchronous implementation of voice toggle actions (no status, no listen)."""
    global _whisper_model

    if action == "start":
        with _lock:
            if _config.get("server_active"):
                return "Server is already active."

        _ensure_imports()

        # Load Whisper model
        model_name = _config.get("whisper_model", DEFAULT_WHISPER_MODEL)
        try:
            with _silence_stdout_fd():
                _whisper_model = _whisper.load_model(model_name)
        except Exception as e:
            return f"Failed to load Whisper model '{model_name}': {e}"

        _start_mic_stream()
        _start_monitor()

        with _lock:
            _config["server_active"] = True
            _save_config(_config)

        return f"Voice server started. Whisper model '{model_name}' loaded."

    if action == "stop":
        _stop_wakeword()
        _stop_monitor()
        _stop_mic_stream()
        _whisper_model = None

        with _lock:
            _config["server_active"] = False
            _config["wakeword_active"] = False
            _save_config(_config)
            _pending_transcriptions.clear()

        return "Voice server stopped. All resources freed."

    if action == "wakeword_on":
        with _lock:
            if not _config.get("server_active"):
                return "Server inactive. Call voice_toggle('start') first."

        _ensure_imports()
        _start_mic_stream()
        _start_monitor()
        return _start_wakeword()

    if action == "wakeword_off":
        _stop_wakeword()
        return "Wake-word detection stopped."

    if action == "response_on":
        with _lock:
            _config["response_active"] = True
            _save_config(_config)
        return "Voice responses enabled."

    if action == "response_off":
        with _lock:
            _config["response_active"] = False
            _save_config(_config)
        return "Voice responses silenced."

    return (
        f"Unknown action: '{action}'. "
        "Valid actions: start, stop, wakeword_on, wakeword_off, "
        "response_on, response_off, status"
    )


def _start_command_listen() -> str:
    """Kick off a one-shot recording whose transcription is queued for voice_check."""
    global _recording, _stop_requested, _has_spoken, _record_start, _last_voice_time, _queue_pending_listen

    with _lock:
        if not _config.get("server_active"):
            return "Server inactive — cannot listen."
        if _recording:
            return "Already recording."

    _ensure_imports()
    _start_mic_stream()
    _start_monitor()

    with _lock:
        _audio_buffer.clear()
        _recording = True
        _stop_requested = False
        _has_spoken = False
        now = time.time()
        _record_start = now
        _last_voice_time = now
        _queue_pending_listen = True

    return "Listening (transcription will be queued for voice_check)."


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
            - "start" — activate voice service (loads Whisper, opens mic)
            - "stop" — deactivate completely (closes mic, unloads model)
            - "wakeword_on" — start OpenWakeWord wake-word detection
            - "wakeword_off" — stop wake-word detection
            - "response_on" — enable voice_speak audio playback
            - "response_off" — silence voice_speak
            - "status" — return current state of all toggles

    Returns:
        str: Status message or current state JSON.
    """
    action = action.strip().lower()

    if action == "status":
        with _lock:
            status = {
                "server_active": _config.get("server_active", False),
                "wakeword_active": _config.get("wakeword_active", False),
                "response_active": _config.get("response_active", True),
                "whisper_model": _config.get("whisper_model", DEFAULT_WHISPER_MODEL),
                "wakeword_keyword": _config.get("wakeword_keyword", DEFAULT_WAKEWORD_KEYWORD),
                "local_voice": _config.get("local_voice", DEFAULT_LOCAL_VOICE),
                "gemini_voice": _config.get("gemini_voice", DEFAULT_GEMINI_VOICE),
                "has_gemini_key": bool(_config.get("gemini_api_key")),
                "pending_transcriptions": len(_pending_transcriptions),
            }
        return json.dumps(status, indent=2)

    return await asyncio.to_thread(_apply_toggle_action, action)


# ---------------------------------------------------------------------------
# Command-file watcher (IPC from the menu bar app)
# ---------------------------------------------------------------------------


def _command_watcher_loop():
    """Poll for command.json from the menu bar app, dispatch, then delete."""
    while True:
        time.sleep(0.5)
        if not COMMAND_FILE.exists():
            continue
        data = None
        try:
            with open(COMMAND_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = None
        try:
            COMMAND_FILE.unlink()
        except OSError:
            pass
        if not isinstance(data, dict):
            continue
        action = (data.get("action") or "").strip().lower()
        if not action:
            continue
        try:
            if action == "listen":
                _start_command_listen()
            else:
                _apply_toggle_action(action)
        except Exception:
            pass


def _start_command_watcher():
    """Start the command-file watcher (idempotent)."""
    global _command_watcher_thread
    if _command_watcher_thread is not None and _command_watcher_thread.is_alive():
        return
    _command_watcher_thread = threading.Thread(
        target=_command_watcher_loop, daemon=True, name="levity-command-watcher"
    )
    _command_watcher_thread.start()


@mcp.tool(
    name="voice_check",
    annotations={
        "title": "Voice Check",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def voice_check() -> str:
    """Check for pending wake-word transcriptions.

    Non-blocking poll for any buffered transcriptions from wake-word
    triggered recordings. Returns pending text if available, or an
    empty indicator if nothing is queued.

    IMPORTANT: When this returns a pending transcription, you MUST call
    voice_speak with your response so the user hears it aloud. The user is
    interacting hands-free via voice and expects a spoken reply.

    Returns:
        str: Pending transcription text, or a status message if empty.
    """
    with _lock:
        if not _config.get("server_active"):
            return "Server inactive."
        if not _config.get("wakeword_active"):
            return "Wake-word detection is not active."
        if not _pending_transcriptions:
            return ""
        # Return all pending transcriptions and clear the queue
        results = list(_pending_transcriptions)
        _pending_transcriptions.clear()

    if len(results) == 1:
        return results[0]
    return json.dumps(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _auto_restore():
    """Restore persisted server/wake-word state shortly after boot.

    Runs in a background thread so the heavy work (Whisper load, mic
    stream open, monitor thread spawn) cannot block mcp.run() from
    initializing the MCP stdio transport.
    """
    time.sleep(2)
    try:
        if _config.get("server_active"):
            print("[levity-voice] auto-restoring server_active from config", file=sys.stderr, flush=True)
            # Force start to run its full init; the persisted flag
            # would otherwise trip the "already active" guard.
            _restore_wakeword = _config.get("wakeword_active", False)
            _config["server_active"] = False
            _apply_toggle_action("start")
            if _restore_wakeword:
                print("[levity-voice] auto-restoring wakeword_active from config", file=sys.stderr, flush=True)
                _apply_toggle_action("wakeword_on")
    except Exception as e:
        print(f"[levity-voice] auto-restore failed: {e!r}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    print("[levity-voice] __main__ entered", file=sys.stderr, flush=True)
    _start_command_watcher()
    threading.Thread(target=_auto_restore, daemon=True, name="levity-auto-restore").start()
    mcp.run()
