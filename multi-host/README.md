# Multi-Host Voice Daemon (WIP — branch `multi-host-daemon`)

**Scaffold only. Not on `main`.** Lets Levity run in Claude Desktop +
Antigravity simultaneously by replacing the single-instance server with:

- **`levity_voiced.py`** — one shared daemon that owns the mic / Whisper / TTS.
  Reuses the audio engine from `~/.levity-voice/server.py`; adds a Unix-socket
  IPC server and cross-host coordination (one speaker at a time, single mic
  capture lock).
- **`levity_shim.py`** — a thin per-host MCP server. Same `voice_*` tools, but
  forwards every call to the daemon. Owns no audio, so many run at once with no
  single-instance churn.

Full design, coordination rules, and test matrix: `../docs/multi-host-voice-daemon.md`.

## Try it (manual, on the branch)

```bash
# 1) start the daemon (or let the shim auto-start it)
~/.levity-voice/venv/bin/python multi-host/levity_voiced.py &

# 2) point a host at the shim instead of server.py, e.g. in
#    claude_desktop_config.json / ~/.gemini/config/mcp_config.json:
#      "command": "~/.levity-voice/venv/bin/python",
#      "args": ["<repo>/multi-host/levity_shim.py"]
```

## Status / TODO (see plan)
- [ ] Decide speak policy: interrupt (current) vs queue.
- [ ] Daemon lifecycle: launchd agent vs first-shim-spawns (current).
- [ ] Crash recovery: shim re-spawns daemon and retries.
- [ ] Run the full multi-host test matrix before merging to `main`.
