---
name: levity-voice
description: Start and drive the Levity voice server — speak replies aloud and capture spoken answers (yes/no or free-form) hands-free. Use when the user says "start voice", "voice mode", "start Levity", "talk to me", "read your answers aloud", or wants to interact by voice.
---

# Levity Voice

Levity gives this assistant a real voice via the `levity-voice` MCP server.
When this skill is invoked, drive the voice loop with these tools.

## On invocation
1. **Start the voice service** — call `voice_toggle` with action `"start"`.
   The service must be active before speaking or listening. (Re-running is
   safe; it reports "already active".)
2. **Speak every reply from now on** — end each response by calling
   `voice_speak` with a natural spoken version of your answer. If the reply is
   long or contains code, pass a short spoken summary, not the raw text.
3. **Confirm what mode the user wants** — `voice_toggle("status")` returns
   `listen_mode`: `quick` favors yes/no, `full` favors free-form answers.

## Capturing spoken input
Always `voice_speak` the question first, then capture the answer:
- **`voice_confirm`** — quick spoken yes/no (≤5s) → `{"decision":"yes|no|unclear"}`.
  Use before anything needing approval ("Should I run this?"). Only act on `yes`;
  treat `no`/`unclear` as "do not proceed".
- **`voice_listen`** — full free-form reply (up to ~30s) → the transcript.
  Use for open questions ("Which option?", "What should I name it?").

## Controls (`voice_toggle` actions)
| Action | Effect |
| :-- | :-- |
| `start` / `stop` | Activate / deactivate the voice service |
| `response_on` / `response_off` | Unmute / mute spoken replies |
| `mode_quick` / `mode_full` | Set the preferred input style |
| `status` | Current state (server, response, mode, platform, build) |

## Notes
- Requires the `levity-voice` MCP server configured in the host app. If the
  `voice_*` tools aren't available, the user should run Levity's
  `install.command` and restart the host.
- The macOS menu-bar app is an optional visual controller — independent of this
  skill, which works through the MCP tools alone.
