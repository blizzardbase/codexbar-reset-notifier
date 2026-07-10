# Agent Context

Instructions for any AI agent or contributor changing this repository.

## Product purpose

Deliver exactly one Telegram message when the Claude session window resets, including a dynamically calculated countdown to the Codex reset and both weekly reset times. It must keep working while the user's Mac is asleep or powered off. While the Mac is awake, an authorized `/usage` command returns live session and weekly usage to the configured chat.

This is a deterministic utility. It consumes **no** LLM, AI, or paid API tokens. Any change that introduces an AI call, a model API, or a Codex-automation dependency is out of scope and must be rejected.

## Architecture

Two halves that share one module.

| Component | Runs on | Responsibility |
| --- | --- | --- |
| `common.py` | both | Config load/validate, cycle projection, formatting, atomic JSON, Telegram payloads. The single source of truth. |
| `monitor.py` | Mac | Read CodexBar, slim the record, sync to VPS (`vps` mode) or evaluate and notify (`local` mode). |
| `usage_bot.py` | Mac | Long-poll Telegram, authorize the configured chat, and return live CodexBar usage for `/usage`. |
| `vps_notifier.py` | VPS | Ingest schedules, project cycles, deduplicate, send Telegram. |
| `configure_telegram.py` | Mac | Discover the chat id and write it to `.env`. |
| `launchagent.plist.template` | Mac | Scheduled monitor template rendered by `scripts/install_mac.sh`. |
| `usage-bot-launchagent.plist.template` | Mac | Persistent command-listener template rendered by `scripts/install_mac.sh`. |
| `scripts/*.sh` | Mac | Install, deploy, test, uninstall. All bash, all idempotent. |
| `tests/` | anywhere | `unittest` only. Never performs network access. |

### Files owned by each component

- **Mac-only:** `monitor.py`, `usage_bot.py`, `configure_telegram.py`, both LaunchAgent templates, `scripts/install_mac.sh`, `scripts/uninstall_mac.sh`.
- **VPS-only:** `vps_notifier.py`. Deployed together with `common.py`, `config.json`, `.env`, `requirements.txt`.
- **Shared:** `common.py`. Changing it affects both halves — redeploy the VPS after touching it.
- **Runtime, never committed:** `data/schedule.json`, `data/vps-state.json`, `data/state.json`, `data/*.log`.

## Source-of-truth rules

- The Mac is authoritative whenever it is online. CodexBar's `usage.primary.resetsAt` is the session anchor; `usage.secondary.resetsAt` is the weekly anchor.
- `windowMinutes` is the repeating interval. **Never hard-code five hours, seven days, or any other interval.** If a provider stops reporting `windowMinutes` and its anchor has passed, the window is unprojectable and must be reported as unavailable, not guessed.
- The VPS projects forward from the last confirmed anchor. Every live Mac sync overwrites the anchors, correcting drift.
- Only reset metadata crosses from the Mac to the VPS. `monitor.slim_record()` strips usage percentages, account emails, and everything else. Do not widen it. A live percentage may leave the Mac only in the `/usage` reply to the configured Telegram chat.
- CodexBar may report several records per provider. **Never take `entries[0]`.** `common.require_single_record()` returns the sole record or raises, naming the accounts it saw.
- `monitor.build_codexbar_command()` owns the exact argv and passes **no account flags**. `--account`, `--account-index`, and `--all-accounts` all address CodexBar *token accounts*; verified against 0.37.2, Claude answers `No token accounts configured for claude.` and Codex answers `codex does not support token accounts.` Passing any of them breaks both providers. Tests assert the argv element for element.
- CodexBar reports provider failures as a JSON `error` object on **stdout**, sometimes with exit 0. `run_codexbar()` checks both and raises `CodexbarError` carrying CodexBar's own message. **Never downgrade or swallow a provider error** — an expired-credentials failure must not be reinterpreted as anything benign.
## Security boundaries

- Secrets live only in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Never in `config.json`, never in code, never in logs.
- Never print, echo, or interpolate a token into an error message, a log line, or a commit.
- `/usage` must respond only when the incoming chat id exactly matches `TELEGRAM_CHAT_ID`; every other chat is ignored without a reply.
- Live usage percentages may be returned to the authorized Telegram chat, but must never be added to the Mac-to-VPS payload.
- No inbound VPS ports. Mac→VPS is outbound SSH with `BatchMode=yes`. VPS→Telegram is outbound HTTPS.
- No `sudo`. Nothing writes outside `vps_remote_dir` on the VPS or `~/Library/LaunchAgents` on the Mac.
- Never touch `/etc`, SSH server config, firewall rules, or system services.
- Uninstall scripts remove only entries carrying the `# codexbar-reset-notifier` marker, and only this project's two LaunchAgent labels.

## Configuration rules

- Anything non-secret goes in `config.json`; ship a placeholder in `config.example.json`.
- `config.json` is git-ignored. `config.example.json` must never contain a real host, user, path, chat id, or token.
- Every config value is validated by `common.validate_config()` before any install, deploy, or run. Add new keys there with a type check and a clear error.
- `vps_check_interval_seconds` must be a divisor of 60 minutes (or 60 itself). cron's `*/N` step restarts hourly, so a non-divisor fires irregularly across the hour boundary. `common.cron_schedule()` owns this rule and is called from `validate_config()`.
- `vps_remote_dir` may contain spaces but never `%`, which cron reserves.
- Errors must name the offending key and never echo credentials.

## Notification behavior

- One message per trigger reset. The first entry of `providers` is the trigger; the second is the countdown companion.
- The exact production shape:

  ```text
  Claude session reset has happened. Codex will reset in about N minutes.

  Claude weekly reset: TIME, DAY LOCAL_TIMEZONE (N days N hours)
  Codex weekly reset: TIME, DAY LOCAL_TIMEZONE (N days N hours)
  ```

- `N` is always computed. No hard-coded Claude-to-Codex gap.
- No 15-minute warning. No separate Codex-reset message. No usage percentages.
- Deduplication key is the ISO timestamp of the trigger's last reset, stored under `resetsSent.trigger`.
- Decision table in `common.evaluate_reset()`: `send`, `seed` (first run, adopt silently), `duplicate`, `expired` (older than `RESET_GRACE_SECONDS`, record without announcing), `unavailable`.
- `evaluate_reset()` is pure. Only `mark_sent()` mutates state, and only the caller writes it.
- Staleness never silences a notification. A stale schedule warns on stderr and still projects — offline continuation is the point.
- `/usage`, `/usage bot`, and `/usage@botname` are accepted while the Mac is awake. The response contains live session and weekly usage and goes only to the originating configured chat.
- The Telegram update offset is stored atomically under ignored `data/`; old pre-install commands are not replayed.

## Testing requirements

- `unittest` only. No third-party test dependencies.
- **No test may perform network access or send a real Telegram message.** Build payloads with `build_telegram_request()`; patch `common.notify` when exercising `run_check`.
- Any change to projection, formatting, deduplication, or config validation needs a test.
- Keep coverage of: session projection per provider, differing window lengths, weekly projection, the Claude→Codex countdown, day/hour formatting, timezone conversion, DST behavior, duplicate prevention, anchor correction, offline continuation, stale data, missing provider data, invalid JSON, invalid config, atomic writes, and Telegram payload construction.

## Commands to run before changing code

```bash
python3 -m py_compile common.py monitor.py usage_bot.py vps_notifier.py configure_telegram.py
python3 -m unittest discover -v
for f in scripts/*.sh; do bash -n "$f"; done
python3 monitor.py --validate-config --config config.example.json
```

Before committing, additionally:

```bash
git diff --check
git status --short                     # .env, config.json, data/ must never appear
grep -rIn --exclude-dir=.git -E '[0-9]{8,10}:[A-Za-z0-9_-]{30,}' .   # bot-token shape
```

## Git safety

- Never commit `.env`, `config.json`, `data/`, logs, or runtime state.
- Never commit real hostnames, SSH aliases, usernames, email addresses, chat ids, or filesystem paths from a developer's machine.
- Work on a feature branch; never commit directly to `master` or `main`.
- Get approval before implementation begins. Once approved, in-scope commits, pushes, PR updates, CodeRabbit fix rounds, and the final merge are pre-authorized.
- Show and inspect the staged diff before each push. Never force-push without separate explicit approval.
- Keep fixing and verifying actionable CodeRabbit findings until no comments remain and every required check is green; then merge and sync the local default branch.
- Before final handoff, update `context.md`, this file, any other affected documentation, and the project Notion page.

## Public-release standards

- Standard library only. Python 3.9+. No third-party packages, ever.
- Every shell script: `set -euo pipefail`, quoted paths, safe with spaces, idempotent, no `sudo`.
- **`ssh` re-parses its command.** `ssh host cmd a b` concatenates the arguments into the single string `cmd a b` and hands it to the remote login shell, which word-splits and glob-expands it. Every remote argument must be quoted with `common.shell_quote()` first, or a path with a space and a cron `*` will arrive mangled. Testing the remote body by running `bash` directly does not exercise this.
- Deployment and uninstall must be reversible and must not disturb unrelated cron entries, LaunchAgents, or files.
- README must stay readable by a non-developer and must state plainly that no AI tokens are consumed.
- Document the projection assumption wherever offline behavior is described: the VPS projects from the last confirmed anchor and window length; a provider-side schedule change while the Mac is offline may cause temporary drift, corrected by the next live sync.
