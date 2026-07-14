# Claude Code Context

Read `AGENTS.md` and `context.md` first. They are the shared source of truth.

## Current behavior

- Claude's primary session window is the only reset trigger.
- Codex may return `usage.primary: null`; treat that as valid and show only its weekly window when present.
- A reset alert contains the Claude-reset headline and available weekly lines. Never calculate or display a Codex session countdown.
- Telegram can target one or more comma-separated `TELEGRAM_CHAT_IDS`; `TELEGRAM_CHAT_ID` is a legacy fallback. `/usage` replies only to the originating configured chat.
- Per-chat delivery progress is persisted so a retry does not duplicate an alert in a destination that already received it.

## Guardrails

- No secrets outside ignored `.env`; no percentages or account details leave the Mac for the VPS.
- No hard-coded reset intervals; always use the reported `windowMinutes`.
- Keep standard library only. Tests must not touch Telegram or the network.
- After changing `common.py`, redeploy the VPS alongside the Mac update.
