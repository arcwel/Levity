#!/usr/bin/env python3
"""
Levity — Claude Code "Stop" hook for guaranteed spoken responses.

Claude Desktop has no way to run code when a turn ends, so spoken output there
depends on the model remembering to call voice_speak (best-effort). Claude Code
DOES support hooks, so we can make it deterministic: this script runs on every
"Stop" event, reads the assistant's final message, and speaks it — independent
of whether the model called any tool.

Guarantee: speaks every completed turn UNLESS
  - the user turned voice off          (config.json -> response_active: false)
  - the model already voiced this turn (last_spoken.json is recent), or
  - macOS `say` is unavailable.

Install: see hooks/README.md (registers this under "Stop" in settings.json).

The hook always exits 0 — a TTS problem must never block the session.
"""

import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLATFORM = platform.system()  # "Darwin", "Windows", or "Linux"
CONFIG_DIR = Path.home() / ".levity-voice"
CONFIG_FILE = CONFIG_DIR / "config.json"
LAST_SPOKEN_FILE = CONFIG_DIR / "last_spoken.json"

DEFAULT_LOCAL_VOICE = "Samantha"
# If voice_speak ran within this many seconds, assume the model already voiced
# this turn and stay quiet (prevents double-speak).
DEDUP_WINDOW_SEC = 20.0
# Keep spoken output snappy; long replies get trimmed to roughly this length.
MAX_SPEAK_CHARS = 700


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _already_spoken_recently() -> bool:
    try:
        with open(LAST_SPOKEN_FILE) as f:
            ts = float(json.load(f).get("ts", 0))
        return (time.time() - ts) < DEDUP_WINDOW_SEC
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def _last_assistant_text(transcript_path: str) -> str:
    """Pull the text of the final assistant message from a Claude Code transcript."""
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return ""

    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg = entry.get("message") if isinstance(entry, dict) else None
        role = (entry.get("type") or (msg or {}).get("role") or "")
        if role != "assistant" or not isinstance(msg, dict):
            continue

        content = msg.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            text = " ".join(p for p in parts if p).strip()
            if text:
                return text
    return ""


def _clean_for_speech(text: str) -> str:
    """Drop fenced code blocks and trim, so we read prose, not syntax."""
    out, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    cleaned = " ".join(" ".join(out).split())
    if len(cleaned) > MAX_SPEAK_CHARS:
        cleaned = cleaned[:MAX_SPEAK_CHARS].rsplit(" ", 1)[0] + ", and more on screen."
    return cleaned


def main() -> int:
    # Read the Stop-hook payload from stdin (contains transcript_path).
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    cfg = _load_config()
    if not cfg.get("response_active", True):
        return 0  # user turned voice off
    if _already_spoken_recently():
        return 0  # model already voiced this turn

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        return 0

    text = _clean_for_speech(_last_assistant_text(transcript_path))
    if not text:
        return 0

    voice = cfg.get("local_voice", DEFAULT_LOCAL_VOICE)
    if _speak(text, voice):
        # Mark as spoken so a rapid follow-up doesn't double up.
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LAST_SPOKEN_FILE, "w") as f:
                json.dump({"ts": time.time(), "chars": len(text)}, f)
        except OSError:
            pass

    return 0


def _speak(text: str, voice: str) -> bool:
    """Speak `text` with the OS TTS engine. Returns True if launched."""
    try:
        if PLATFORM == "Darwin":
            proc = subprocess.Popen(
                ["say", "-v", voice], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
            )
            proc.stdin.write(text)
            proc.stdin.close()
            return True
        if PLATFORM == "Windows":
            # Write to a temp file to avoid PowerShell injection.
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as tf:
                tf.write(text)
                txt_path = tf.name
            esc_voice = voice.replace("'", "''")
            esc_path = txt_path.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"try {{ $s.SelectVoice('{esc_voice}') }} catch {{ }}; "
                f"$t = Get-Content -Raw -Encoding UTF8 '{esc_path}'; $s.Speak($t)"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        # Linux: espeak-ng / espeak
        for cmd in ("espeak-ng", "espeak"):
            try:
                proc = subprocess.Popen(
                    [cmd, "--stdin"], stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                )
                proc.stdin.write(text)
                proc.stdin.close()
                return True
            except FileNotFoundError:
                continue
        return False
    except (FileNotFoundError, OSError):
        return False
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
