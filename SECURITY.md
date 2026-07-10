# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository (Security → Report a vulnerability).

Include what you did, what happened, and what you expected. A proof of concept helps. Expect an acknowledgement within a week.

## What this project handles

The only secrets are a **Telegram bot token** and a **Telegram chat id**, both stored in `.env` (mode `600`, git-ignored) on the Mac and on the VPS.

The project holds **no Claude or Codex credentials**. It reads reset timestamps from the local CodexBar CLI and never authenticates to any AI provider. No prompts, conversations, code, or usage history are transmitted anywhere. No AI or LLM API is ever called.

## Trust boundaries

- **Mac → VPS**: outbound SSH with `BatchMode=yes`. Only `resetsAt` timestamps and `windowMinutes` integers are sent. Usage percentages and account emails are stripped by `monitor.slim_record()` before anything leaves the machine.
- **VPS → Telegram**: outbound HTTPS to `api.telegram.org`.
- **No inbound ports** are opened on the VPS. Nothing listens.
- Nothing runs as root. Nothing is written outside `vps_remote_dir` on the VPS or `~/Library/LaunchAgents` on the Mac. `/etc`, SSH server configuration, firewall rules, and system services are never touched.

A compromised VPS exposes reset timestamps and the Telegram bot token — nothing else.

## If your bot token leaks

Send `/revoke` to [@BotFather](https://t.me/BotFather) and select the bot. The old token stops working immediately. Put the new one in `.env`, then re-run `./scripts/deploy_vps.sh`.

## Hardening notes

- Use SSH key authentication and a dedicated non-root user on the VPS.
- `.env` is copied to the VPS by `deploy_vps.sh` because the VPS needs the token to send messages. It is written with mode `600`. Anyone who can read that file can send messages as your bot.
- Never commit `.env` or `config.json`. Both are git-ignored, and CI fails if either becomes tracked.
- Errors in this project never print the token or chat id.

## Supported versions

The tip of the default branch is supported. There are no released versions to backport to.
