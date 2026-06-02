# Levity Voice (Cowork plugin)

Gives your AI assistant a real voice: it speaks replies aloud and can capture
spoken **yes/no** or **free-form** answers — hands-free.

## What's inside
- **`levity-voice` skill** — when you say "start voice" (or similar), the
  assistant starts the voice service and, from then on, speaks its replies and
  can listen for your answers.

## Requirement
This plugin drives the **`levity-voice` MCP server**, which must be installed
and configured in your host app. Install it once with Levity's
`install.command` (creates `~/.levity-voice/` and registers the server). If the
`voice_*` tools aren't available after installing this plugin, run that
installer and restart the host.

> Already have `levity-voice` in your `claude_desktop_config.json`? Keep it —
> this plugin only adds the skill, not a second server (which would conflict).

## Use
Say **"start voice"**, **"voice mode"**, or **"start Levity"**. To stop, say
"stop voice" or "mute voice".
