# Levity

**Voice-driven AI development — talk to your coding assistant and hear it talk back.**

Levity is a hands-free voice layer for AI-assisted development. It captures your
voice, transcribes it locally with [Whisper](https://github.com/openai/whisper),
and speaks responses aloud using a two-tier text-to-speech engine (a fast local
system voice, with an optional cloud upgrade via Gemini 2.5 Flash TTS).

The project ships **two independent components** — use either or both:

| Component | What it is | Use it when |
| :-- | :-- | :-- |
| [`levity-voice-mcp`](#1-levity-voice-mcp-claude-desktop) | An [MCP](https://modelcontextprotocol.io) server that gives **Claude Desktop** a voice. | You want to talk to Claude Desktop hands-free. |
| [`antigravity-voice`](#2-antigravity-voice-vs-code-extension) | A **VS Code / Antigravity** extension with its own STT, TTS, wake-word, and settings UI. | You want voice control inside your editor. |

Both share the same design: **local-first, bring-your-own-key (BYOK), no telemetry.**

---

## Platform support

| Platform | Status | Notes |
| :-- | :-- | :-- |
| **macOS** | ✅ Supported | Primary target. Uses the built-in `say` and `afplay` commands for speech. |
| **Windows 11** | 🔜 Coming soon | In active development — not yet functional. See [Windows: coming soon](#windows-coming-soon). |
| **Linux** | 🔜 Coming soon | The architecture supports it; audio shims are planned. Contributions welcome. |

> **Heads-up:** today the speech features depend on macOS-only commands
> (`say`, `afplay`). Windows and Linux support is on the way — see
> [Windows: coming soon](#windows-coming-soon).

---

## Prerequisites

Shared across both components:

- **Python 3.9+** on your `PATH` (`python3 --version`).
- **A working microphone**, and permission for your terminal / editor / Claude
  Desktop to access it (macOS: System Settings → Privacy & Security → Microphone).
- **~150 MB–1.5 GB disk** for the Whisper model (downloaded on first run; size
  depends on the model you choose — `base` is ~140 MB).
- **Xcode Command Line Tools** on macOS (`xcode-select --install`) — needed to
  build some audio dependencies.

Optional, depending on features you enable:

- **Gemini API key** (free tier available at
  [aistudio.google.com](https://aistudio.google.com/app/apikey)) — enables the
  higher-quality cloud TTS voice and, in the extension, AI command processing.
  Without it, everything falls back to the local system voice.
- **Picovoice access key** (free at [console.picovoice.ai](https://console.picovoice.ai/))
  — only the **extension's** wake-word mode needs this. The MCP server uses
  [OpenWakeWord](https://github.com/dscripka/openWakeWord) instead, which needs
  no key (see [Wake-word engines](#wake-word-engines)).

---

## 1. `levity-voice-mcp` (Claude Desktop)

A lightweight MCP server exposing voice tools to Claude Desktop:

- `voice_listen` — record from the mic and transcribe with Whisper.
- `voice_speak` — speak text aloud (local `say`, or Gemini TTS for longer replies).
- `voice_toggle` — start/stop the server, toggle responses, check status.
- `voice_check` — poll for wake-word-triggered transcriptions.

### Install (macOS, one-click)

1. In Finder, open the `levity-voice-mcp/` folder and **double-click `install.command`.**
   It creates a virtual environment, installs dependencies, copies the server to
   `~/.levity-voice/`, and registers itself in your Claude Desktop config.
2. **Restart Claude Desktop.**
3. In Claude, say or type: *"Start the voice server."*

### Install (manual / scripted)

```bash
cd levity-voice-mcp
./setup.sh          # creates ~/.levity-voice/venv and installs deps
```

`setup.sh` prints the exact JSON snippet to add to your Claude Desktop config at:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

```json
{
  "mcpServers": {
    "levity-voice": {
      "command": "/Users/you/.levity-voice/venv/bin/python",
      "args": ["/path/to/levity-voice-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop afterward.

### Optional: menu-bar app (macOS)

`menubar.py` adds a 🎙 menu-bar toggle for the server, wake-word, and voice
responses. It requires [`rumps`](https://github.com/jaredks/rumps) (installed by
the setup scripts via `requirements.txt`). Run it with the venv Python:

```bash
~/.levity-voice/venv/bin/python ~/.levity-voice/menubar.py
```

### Configuration

Settings live in `~/.levity-voice/config.json` (created on first run). Notable keys:

| Key | Default | Meaning |
| :-- | :-- | :-- |
| `whisper_model` | `base` | `tiny` / `base` / `small` / `medium` — accuracy vs speed. |
| `local_voice` | `Samantha` | Any macOS voice (run `say -v '?'` to list). |
| `gemini_api_key` | `""` | Set to enable cloud TTS. |
| `gemini_voice` | `Kore` | Gemini TTS persona. |

---

## 2. `antigravity-voice` (VS Code extension)

A full voice assistant inside the editor: **Listen → Transcribe → Think → Speak.**
With a Gemini key it answers coding questions with awareness of your active file;
without one it echoes back your transcription.

### Install (development build)

```bash
cd antigravity-voice
npm install
npm run compile        # builds ./out
npm run setup-sidecar  # creates sidecar/.venv and installs Python deps
```

Then press **F5** in VS Code to launch an Extension Development Host, or package
a `.vsix` with [`vsce`](https://github.com/microsoft/vscode-vsce):

```bash
npx vsce package
```

### Usage

- **Hotkey:** `Cmd+Alt+V` (macOS) / `Ctrl+Alt+V` (Windows/Linux) to start/stop.
- **Command Palette:** search "Antigravity" for all commands.
- **Settings:** run *"Antigravity: Open Voice Settings"* for a visual config panel,
  or edit `antigravity.*` keys in VS Code settings.
- **API keys:** *"Antigravity: Set Gemini API Key"* / *"Set Picovoice Access Key"* —
  stored securely in the OS keychain via VS Code SecretStorage, never on disk.

### Trigger modes

- **Tap-to-talk** — press the hotkey to start, press again (or pause) to stop.
- **Wake word** — always-on detection; say the keyword to start (needs a Picovoice key).

---

## Wake-word engines

The two components use **different** wake-word backends — a deliberate trade-off:

- **`levity-voice-mcp` → OpenWakeWord** — fully local, no account or key required.
  Built-in keywords: `alexa`, `hey_mycroft`, `hey_jarvis`, `hey_rhasspy`.
- **`antigravity-voice` → Picovoice Porcupine** — higher accuracy and more
  built-in keywords, but requires a free Picovoice access key, and supports
  custom `.ppn` keyword files trained at the Picovoice console.

---

## Privacy & security

- **Speech-to-text runs locally** (Whisper) — your audio is not sent anywhere
  unless you explicitly enable cloud TTS.
- **API keys are stored in the OS keychain** (extension) or your local config
  file (MCP server) — never committed to the repo and never logged.
- **No analytics or telemetry.**

---

## Windows: coming soon

Windows 11 support is in active development. The remaining work before it's
functional:

- **Local TTS** uses macOS `say`; needs a PowerShell `System.Speech` / SAPI shim.
- **Cloud TTS playback** uses macOS `afplay`; needs a Windows audio player.
- **Python discovery** assumes `python3` and a POSIX venv layout (`bin/python3`);
  Windows uses `python` / the `py` launcher and `Scripts\python.exe`.

Until these land, run Levity on macOS. PRs adding the Windows shims are welcome.

---

## Roadmap

- Cross-platform speech (Windows SAPI, Linux `espeak`/Piper).
- Fully offline AI tier (local LLM via Ollama for command interpretation).
- Pinned dependency versions for reproducible installs.

---

## License

[MIT](./LICENSE) © 2026 Anthony Guidry
