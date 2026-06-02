# Development Plan — Multi-Host Voice Daemon

**Status:** Proposal for review. The current shipped architecture
(single-instance server) stays on `main`; this design will be built on a branch
(`multi-host-daemon`) and tested before any merge.

## Problem

Each host (Claude Desktop, Antigravity IDE, Antigravity.app) launches its **own**
copy of `server.py`, and they all share `~/.levity-voice/`. The server enforces a
**single instance** (PID lock) so processes don't fight over the mic/speaker.

Consequence: starting Levity in a second host **kills** the first host's server;
if the host auto-respawns it, they **ping-pong** ("instance churn" — repeated
`stale PID file` / `server started`, voice cutting out). So Levity is reliable
with **one host at a time**, not all three simultaneously.

## Goal

Let Levity run in all hosts **at once**, sharing **one** coordinated voice:
one mic, one speaker, no churn, no overlapping speech.

## Proposed architecture: daemon + thin shims

```
Claude Desktop ─┐
Antigravity IDE ─┼─ (stdio MCP) ─ levity-shim ─┐
Antigravity.app ─┘                              ├─(local IPC)─ levity-voiced (daemon)
                              menu-bar app ──────┘                 │
                                                        owns: mic · Whisper · TTS · state
```

- **`levity-voiced` (daemon)** — one persistent process that owns the
  microphone, the Whisper model, and TTS output, plus all state
  (`config.json`, `listen_mode`, `response_active`). Single-instance (it's the
  only thing touching audio). Auto-started by the first shim that needs it (or a
  Login Item).
- **`levity-shim` (per host)** — a lightweight MCP stdio server exposing the
  same tools (`voice_speak`, `voice_confirm`, `voice_listen`, `voice_toggle`).
  It does **no audio**; it forwards each call to the daemon over IPC and returns
  the result. Many shims can run at once with no contention (they don't own the
  mic), which removes the single-instance kill entirely.
- **Menu-bar app** — connects to the daemon for status/controls (as it largely
  does today via the command/config files).

## Coordination rules (enforced in the daemon)

- **Speak:** serialize. Policy options (config): `interrupt` (newest cuts off
  current — today's behavior) or `queue` (play in order). One speaker at a time.
- **Listen/confirm:** a single global capture lock. If a second host requests
  capture while one is active, return `busy` immediately (never two mics open).
- **State is global:** `response_active`, `listen_mode`, voices apply to all
  hosts uniformly (one source of truth in the daemon).
- Optional: tag which host requested speech (so the daemon could prefix or
  route), if we ever want per-host voices.

## IPC options (decide at review)

| Option | Pros | Cons |
| :-- | :-- | :-- |
| **Unix domain socket** (recommended) | low latency, bidirectional, clean lifecycle | a bit more code |
| File-based (`command.json` + response file) | already partly built, dead simple | polling latency, no streaming |
| Localhost HTTP | language-agnostic, easy to debug | port management, heavier |

## Phases

1. **Extract the audio engine** from `server.py` into `levity-voiced` with an
   IPC server. Define the protocol: `speak{text,force_local}`,
   `confirm{timeout}`, `listen{timeout}`, `toggle{action}`, `status`.
2. **Build `levity-shim`** — thin MCP stdio server forwarding to the daemon;
   auto-starts the daemon if it's not running; clean error if the daemon is down.
3. **Daemon coordination** — implement speak serialization (+ policy), the
   single-capture lock with `busy`, and global state.
4. **Menu bar** — point it at the daemon's IPC/state.
5. **Repoint hosts** — `claude_desktop_config.json` and Antigravity
   `mcp_config.json` launch `levity-shim` instead of `server.py`.
6. **Test matrix** — all three hosts open simultaneously: confirm no churn,
   speech never overlaps, only one mic opens at a time, state stays consistent,
   daemon survives a host quitting, and recovers if the daemon is killed.

## Risks / open questions

- **Daemon lifecycle:** first-shim-spawns vs a launchd LaunchAgent. Crash
  recovery (a shim should respawn the daemon and retry).
- **GUI session:** the daemon must run in the user's GUI/Aqua session for the
  menu-bar status item and audio (same constraint we already rely on).
- **Security:** local socket file permissions (user-only); no network exposure.
- **Backward compatibility:** keep the single-instance `server.py` working
  (default) so nothing breaks if a host points at it directly.
- **Cross-platform:** the daemon/shim split is OS-agnostic; audio backends stay
  per-platform as today.

## Rollout

- Build + test on branch `multi-host-daemon`.
- Keep `main` = single-instance (current, shipped).
- Merge only after the test matrix passes and you've signed off.
