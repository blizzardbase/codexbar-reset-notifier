# Contributing

Thanks for taking a look. This project is deliberately small and deliberately boring: it reads timestamps and does arithmetic on them.

## Non-negotiable constraints

These are not style preferences. A change that breaks one of them will be declined.

1. **Python standard library only.** No third-party packages, ever. `requirements.txt` stays empty, and CI enforces it.
2. **No AI, LLM, or paid API calls.** The whole point is that this costs nothing to run. If a feature needs a model, it belongs in a different project.
3. **No hard-coded reset intervals.** Projection uses the provider's own `windowMinutes`. A window whose anchor has passed without a reported interval is *unprojectable*, not five hours.
4. **Secrets live only in `.env`.** Never in `config.json`, never in code, never in a log line, never in an error message.
5. **No inbound VPS ports, no `sudo`, no writes outside `vps_remote_dir`.**
6. **Tests never touch the network** and never send a real Telegram message.

## Before you open a pull request

```bash
python3 -m py_compile common.py monitor.py vps_notifier.py configure_telegram.py tests/*.py
python3 -m unittest discover -v
for f in scripts/*.sh; do bash -n "$f"; done
shellcheck scripts/*.sh          # if you have it
cp config.example.json config.json && python3 monitor.py --validate-config && rm config.json
git diff --check
```

CI runs all of this across Python 3.9 through 3.13, plus a scan for secret-shaped strings and a check that `.env`, `config.json`, and `data/` are untracked.

## Where things live

`common.py` is the single source of truth — config validation, cycle projection, formatting, atomic writes, Telegram payloads. Both `monitor.py` (Mac) and `vps_notifier.py` (VPS) import it, so behavior cannot drift between the two halves. **If you change `common.py`, the VPS must be redeployed.**

Read `AGENTS.md` for the full invariant list before changing logic.

## Testing rules

Any change to projection, formatting, deduplication, account selection, or config validation needs a test.

Build Telegram payloads with `build_telegram_request()` and assert on the URL and body. When exercising `run_check`, patch `common.notify`. If a test would open a socket, it is wrong.

Time is injected, never read from the clock inside pure functions — pass `now` explicitly so tests are deterministic.

## Shell scripts

Every script: `#!/usr/bin/env bash`, `set -euo pipefail`, every path quoted, safe with spaces, idempotent, and reversible.

Anything passed to `ssh` gets re-parsed by the remote login shell — `ssh host cmd a b` sends the single string `cmd a b`. Quote remote arguments with `common.shell_quote()`. A path with a space or a cron `*` will otherwise arrive mangled.

Uninstall scripts must remove only what this project created: the `# codexbar-reset-notifier` cron marker and the `local.codexbar-reset-notifier` LaunchAgent label. Unrelated entries survive untouched.

## Calling CodexBar

`monitor.build_codexbar_command()` owns the argument list, and tests assert it element for element. Two traps:

- `--account` and `--all-accounts` only address CodexBar **token accounts**. A provider signed in with OAuth or cookies rejects them with `No token accounts configured`. Never pass either flag unconditionally — it breaks Claude.
- CodexBar reports provider failures as a JSON `error` object on **stdout**, sometimes while exiting 0. Check the payload, not just the return code.

## Commit and PR style

Small, focused commits with a one-line summary and a body explaining *why*. Branch off the default branch; never commit to it directly. Fill in the pull request template.

## Reporting bugs

Open an issue using the bug report template. Include `python3 --version`, your OS, whether you run in `local` or `vps` mode, and the output of `python3 monitor.py --status`. **Redact your bot token and chat id.**
