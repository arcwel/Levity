# Levity Voice

A lightweight MCP server that gives Claude Desktop a voice. Every response is spoken aloud using a two-tier TTS system: local system TTS for short/fast replies, and Gemini 2.5 Flash TTS for longer, more natural-sounding speech. Also supports voice input via Whisper STT for yes/no confirmations and free-form transcription.

Works on **macOS**, **Windows 11**, and **Linux**.

## Features

- **Two-tier TTS** — short text (<200 chars) uses your system's built-in TTS for instant playback; longer text uses Google Gemini 2.5 Flash TTS for high-quality, natural speech
- **Voice input (STT)** — Whisper-based microphone capture for confirmations (`voice_confirm`) and free-form listening (`voice_listen`)
- **Cross-platform** — macOS (`say`/`afplay`), Windows (`System.Speech.Synthesis`), and Linux (`espeak-ng`/`aplay`)
- **Stale instance management** — PID lock file detects and cleans up orphaned server processes on startup
- **Menu bar app** (macOS) — toggle the server, voice responses, and listen mode from the macOS menu bar
- **Hooks** — shell scripts in `hooks/` that fire on server events (e.g. suppress double-speaking after Claude Code stop)
- **Multi-host daemon** — optional `multi-host/` mode shares one audio engine across Claude Desktop and Antigravity via a Unix-domain socket, so both apps use the same coordinated voice
- **Launch at login** — optional LaunchAgent integration (macOS)

## Requirements

- Python 3.10+
- Claude Desktop

### Platform-specific

| Platform | Local TTS | Audio Playback | Extra Install |
|----------|-----------|----------------|---------------|
| **macOS** | `say` (built-in) | `afplay` (built-in) | None |
| **Windows 11** | `System.Speech.Synthesis` (built-in) | `System.Media.SoundPlayer` (built-in) | None |
| **Linux** | `espeak-ng` or `espeak` | `aplay`, `paplay`, or `ffplay` | `sudo apt install espeak-ng` |

### Voice input (STT) — optional

`voice_confirm` and `voice_listen` require `sounddevice`, `numpy`, and `openai-whisper` (included in `requirements.txt`). Whisper runs locally — no API key needed for STT.

## Installation

### macOS / Linux

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/levity-voice.git ~/.levity-voice

# Create a virtual environment and install dependencies
cd ~/.levity-voice
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Windows

```powershell
# Clone the repo
git clone https://github.com/YOUR_USERNAME/levity-voice.git %USERPROFILE%\.levity-voice

# Create a virtual environment and install dependencies
cd %USERPROFILE%\.levity-voice
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### Linux: install system TTS

```bash
# Debian/Ubuntu
sudo apt install espeak-ng

# Fedora
sudo dnf install espeak-ng

# Arch
sudo pacman -S espeak-ng
```

### Configure Claude Desktop

Add this to your Claude Desktop MCP config, replacing `YOUR_USERNAME` with your system username.

**macOS** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "levity-voice": {
      "command": "/Users/YOUR_USERNAME/.levity-voice/venv/bin/python",
      "args": ["/Users/YOUR_USERNAME/.levity-voice/server.py"]
    }
  }
}
```

**Windows** (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "levity-voice": {
      "command": "C:\\Users\\YOUR_USERNAME\\.levity-voice\\venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\YOUR_USERNAME\\.levity-voice\\server.py"]
    }
  }
}
```

**Linux** (`~/.config/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "levity-voice": {
      "command": "/home/YOUR_USERNAME/.levity-voice/venv/bin/python",
      "args": ["/home/YOUR_USERNAME/.levity-voice/server.py"]
    }
  }
}
```

> **Note:** Use the full absolute path — JSON does not expand `~`.
> Find your username: `whoami` (macOS/Linux) or `echo %USERNAME%` (Windows).

Restart Claude Desktop to pick up the new server.

### Optional: Gemini TTS

For higher-quality voice output on longer text, add a Gemini API key:

```bash
cp .env.example .env
# Edit .env and add your key (get one at https://aistudio.google.com/apikey)
```

Without a key, everything still works — it just uses local TTS for all text.

### Optional: Menu bar app (macOS only)

The menu bar app requires macOS (it uses `rumps` and Cocoa frameworks). On Windows and Linux, use the `voice_toggle` MCP tool to control the server instead.

```bash
# Run in the background
nohup ~/.levity-voice/venv/bin/python ~/.levity-voice/menubar.py &>/dev/null &
```

The menu bar icon lets you toggle the server, voice responses, and listen mode. You can also enable "Launch at Login" from the menu.

### Optional: Multi-host daemon

If you run both Claude Desktop and another MCP host (e.g. Antigravity), the `multi-host/` daemon shares one audio engine between them — one voice, no overlap. See `multi-host/README.md` for setup.

## Architecture

```
┌─────────────────┐         config.json          ┌─────────────────┐
│   server.py     │◄────────(read every 0.5s)────►│   menubar.py    │
│   (MCP server)  │                               │ (macOS only)    │
│                 │◄──── command.json (write) ─────│                 │
│  voice_speak    │         (atomic rename)        │   menu toggles  │
│  voice_toggle   │                               │   launch agent  │
│  voice_confirm  │                               └─────────────────┘
│  voice_listen   │
└─────────────────┘
        │
        ├── short text ──► System TTS (say / SAPI / espeak)
        ├── long text  ──► Gemini 2.5 Flash TTS ──► Audio playback
        └── mic input  ──► Whisper STT ──► text transcript

Multi-host (optional):
┌────────────┐  Unix socket  ┌───────────────────┐
│ levity_shim│──────────────►│ levity_voiced.py  │
│  (per host)│               │  (shared daemon)  │
└────────────┘               │  owns mic + TTS   │
                             └───────────────────┘
```

- **server.py** — FastMCP server exposing `voice_speak`, `voice_toggle`, `voice_confirm`, and `voice_listen` tools. Runs inside Claude Desktop's MCP host. Cross-platform: detects macOS/Windows/Linux and uses the appropriate TTS and audio playback backends. Includes PID lock file management to prevent stale instances.
- **menubar.py** — standalone macOS menu bar app using `rumps`. Reads `config.json` for display, writes `command.json` for one-shot commands. macOS only — on other platforms, use the `voice_toggle` MCP tool directly.
- **hooks/** — shell scripts that fire on server events. The included `stop` hook prevents the daemon from double-speaking a turn that the MCP server already voiced.
- **multi-host/** — optional shared daemon (`levity_voiced.py`) and per-host shim (`levity_shim.py`). Multiple MCP hosts share one microphone and one TTS engine over a Unix-domain socket.
- Communication between server and menubar is file-based IPC with atomic writes (tmp + rename) to prevent partial reads.

## Configuration

Settings live in `~/.levity-voice/config.json` (created automatically on first run):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `server_active` | bool | `false` | Whether TTS/STT is active |
| `response_active` | bool | `true` | Whether `voice_speak` produces audio |
| `local_voice` | string | `"Samantha"` | System voice name (macOS: `say -v ?`, Windows: see Speech settings, Linux: `espeak-ng --voices`) |
| `gemini_voice` | string | `"Kore"` | Gemini TTS voice name |
| `whisper_model` | string | `"base"` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large` |
| `listen_mode` | string | `"quick"` | `"quick"` = yes/no confirmation, `"full"` = free-form transcript |
| `auto_menubar` | bool | `true` | Auto-launch the menu bar app on server start (macOS only) |

The Gemini API key is stored in `~/.levity-voice/.env` (never in `config.json`).

## MCP Tools

### `voice_speak(text, force_local=False)`
Speak text aloud. Automatically chooses local or Gemini TTS based on text length. Refuses if `server_active` is false.

### `voice_toggle(action)`
Control server state. Actions: `start`, `stop`, `response_on`, `response_off`, `status`.

### `voice_confirm(timeout=5.0)`
Record a short yes/no reply from the microphone and return `{"decision": "yes"|"no"|"unclear", "transcript": "..."}`. Requires `server_active`.

### `voice_listen(timeout=30.0)`
Record free-form speech and return the Whisper transcript. Requires `server_active`.

## Tests

Run offline (no audio hardware, Whisper models, or network): `pip install pytest -r requirements.txt` then `python -m pytest -q`.
Tests needing real hardware carry the `hardware` marker and are deselected by default; run them with `python -m pytest -m hardware`.

## License

MIT
