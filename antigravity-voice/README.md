# Antigravity Voice (VS Code extension)

A voice assistant inside the editor: **Listen → Transcribe → Think → Speak.**
With a Gemini key it answers coding questions with awareness of your active
file; without one it echoes back your transcription.

> **Status:** macOS-focused (uses the macOS `say`/`afplay` commands). The
> cross-platform engine lives in the [`levity-voice-mcp`](../levity-voice-mcp)
> server; porting this extension to it is on the roadmap.

## Develop / run

```bash
npm install
npm run compile          # builds ./out
npm run setup-sidecar    # creates sidecar/.venv and installs Python deps
```

Press **F5** in VS Code to launch an Extension Development Host.

## Package a .vsix

```bash
npx vsce package
```

> `vsce` requires a `publisher` field in `package.json` (your VS Code
> Marketplace publisher ID). Add it before packaging:
> `"publisher": "<your-marketplace-id>"`.

## Usage

- **Hotkey:** `Cmd+Alt+V` (macOS) / `Ctrl+Alt+V` (Win/Linux) to start/stop.
- **Command Palette:** search "Antigravity".
- **Settings:** *"Antigravity: Open Voice Settings"*, or edit `antigravity.*` keys.
- **API keys:** *"Antigravity: Set Gemini API Key"* / *"Set Picovoice Access Key"* —
  stored in the OS keychain via VS Code SecretStorage, never on disk.

## Trigger modes

- **Tap-to-talk** — press the hotkey to start, press again (or pause) to stop.
- **Wake word** — always-on Picovoice Porcupine detection (needs a free
  Picovoice access key; supports custom `.ppn` keywords).

## Components

- `src/` — the TypeScript extension (voice pipeline, TTS, settings webview).
- `sidecar/voice_worker.py` — Python sidecar: mic capture, Whisper STT, and
  Porcupine wake-word, speaking JSON over stdio.

Licensed under [MIT](../LICENSE).
