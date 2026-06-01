# Guaranteed spoken responses in Claude Code

`claude_code_speak_hook.py` makes Levity speak **every** completed turn in
Claude Code / Cowork — deterministically, without depending on the model
remembering to call `voice_speak`.

It runs on the Claude Code **`Stop`** event, reads the assistant's final
message from the transcript, and reads it aloud with the macOS `say` voice from
your Levity config.

## Why a hook (and why Claude Desktop is different)

Spoken output can only be *guaranteed* by something that runs when a turn ends.

| Runtime | Mechanism | Guarantee |
| :-- | :-- | :-- |
| **Claude Code / Cowork** | this `Stop` hook | ✅ Every turn, unless toggled off / `say` missing |
| **Antigravity extension** | in-code `TTSProvider.speak()` | ✅ Every turn, unless toggled off |
| **Claude Desktop** | model calls `voice_speak` | ⚠️ Best-effort — Desktop has no hook system |

## Install

1. Make the hook executable:

   ```bash
   chmod +x ~/.levity-voice/hooks/claude_code_speak_hook.py   # after copying it there
   ```

   (Or run it via `python3 <path>` and skip the chmod.)

2. Add a `Stop` hook to your Claude Code settings — either
   `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project):

   ```json
   {
     "hooks": {
       "Stop": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "python3 \"$HOME/.levity-voice/hooks/claude_code_speak_hook.py\""
             }
           ]
         }
       ]
     }
   }
   ```

3. Restart Claude Code (or start a new session) so the hook loads.

## Turning it off

The hook honors the same switch as the MCP server. To mute:

```bash
# In a Levity session, tell Claude: "voice off"   (runs voice_toggle response_off)
# or edit ~/.levity-voice/config.json:
#   { "response_active": false }
```

## No double-speak

If the model already called `voice_speak` this turn, the MCP server writes
`~/.levity-voice/last_spoken.json`. The hook checks it and stays quiet when a
reply was voiced in the last 20 seconds, so you never hear the response twice.
