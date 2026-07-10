# Context

Handoff document for future sessions and agents. Read this before changing anything.

## The problem

Claude's session window resets on a fixed cycle. If you do not notice the reset, paused work sits idle for hours. Codex resets a little afterwards on its own cycle.

A notifier that runs only on a Mac is useless here: the reset that matters most happens overnight, while the Mac is asleep or off. Whatever watches the clock has to be awake when the user is not.

CodexBar already exposes the exact reset timestamps as structured JSON, so no scraping, OCR, screenshots, or AI inference is required — only arithmetic on timestamps.

## Final product decisions

**One combined notification, not three.** Early designs sent a 15-minute warning, a Claude reset alert, and a separate Codex reset alert. Three interruptions per cycle trained the user to ignore all of them. The signal that matters is *"you can work again now"*, and that is the Claude reset. Codex resets shortly after, so its countdown rides along in the same message rather than earning its own ping. The weekly lines answer the natural follow-up question ("how much runway is left?") without a second message.

**Telegram private DM, not a macOS notification.** A macOS notification cannot appear while the Mac is off, which is the only case that matters. A Telegram DM reaches the phone, supports a custom per-chat notification sound so the alert is recognisable without reading it, and needs no inbound port or push infrastructure. macOS notifications were dropped entirely rather than kept as a confusing second channel.

**VPS projection, not a cloud service or a polling phone app.** The VPS is the cheapest way to own an always-on clock. It deliberately holds no Claude or Codex credentials: it receives the last confirmed anchor and the window length, then repeats the cycle. That keeps the trust boundary tight — a compromised VPS leaks reset times and a Telegram token, nothing more — and it means the notifier keeps working through a Mac outage of any length.

**No usage percentages in the message.** Percentages read from CodexBar are correct only at sync time. Projected forward on the VPS they would be stale and misleading, so they are stripped before they ever leave the Mac.

**No hard-coded intervals.** Both providers currently report 300-minute session windows, but the code never assumes it. `windowMinutes` from the provider drives every projection. If a provider stops reporting it and the anchor has passed, the window is declared unavailable rather than guessed.

## Current architecture

- `common.py` holds every rule: config validation, cycle projection, message formatting, atomic JSON writes, Telegram payload construction. Both halves import it, so behavior cannot diverge.
- `monitor.py` runs on the Mac under a LaunchAgent every `mac_sync_interval_seconds` (default 300). It reads CodexBar, reduces each record to `resetsAt` + `windowMinutes`, and ships that over SSH.
- `vps_notifier.py` runs on the VPS under cron every `vps_check_interval_seconds` (default 60). It projects both cycles forward, decides whether a reset just happened, sends at most one Telegram DM, and records the reset.
- Deduplication keys on the ISO timestamp of the trigger provider's last reset. The same reset can never be announced twice.
- `evaluate_reset()` returns one of `send`, `seed`, `duplicate`, `expired`, `unavailable`. It is pure; only the caller writes state.

## Configuration model

Secrets and settings are strictly separated.

- `.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Git-ignored. Mode `600` on both machines. Nothing else belongs here.
- `config.json` — timezone, providers, CodexBar path or discovery, notification mode, VPS host/user/remote dir, both intervals, stale-data threshold. Git-ignored, because it holds a real hostname and path.
- `config.example.json` — the same keys with placeholders. Committed. Must never contain a real host, user, path, or credential.

`providers` is ordered: the first entry triggers the notification, the second is the one counted down to.

Every value passes `common.validate_config()` before any install, deploy, or run, so a typo fails loudly at setup instead of silently at 3 a.m.

## Known limitations

- **Projection drift.** The VPS extrapolates from the last confirmed anchor. If a provider changes its schedule while the Mac is offline, the projection may temporarily drift. The next live Mac sync corrects it. This is the central assumption of the design and is documented in the README.
- **Two-minute grace window.** `RESET_GRACE_SECONDS = 120`. A VPS outage longer than that causes the reset to be recorded silently rather than announced late.
- **Silent first run.** A fresh install adopts the current cycle without notifying, so the user is not messaged about a reset that predates the install.
- **Weekly line requires an interval.** Without `windowMinutes` on the weekly window, the line survives until its anchor passes, then disappears.
- **Cron granularity.** The VPS check interval must be a divisor of 60 minutes, or 60 itself. A `*/7` step would fire at :49, :56, then :00 — an irregular gap every hour. Rejected at validation time.
- **One account per provider.** CodexBar offers no working account selection for Claude or Codex, so each provider's default account is watched. If several records ever come back, the notifier stops and names them rather than guessing. Multi-account support is out of scope.
- **`data/cron.log` is never rotated.** It grows slowly and can be deleted freely.
- **Local-only mode cannot notify while the Mac sleeps.** Documented, not fixable without the VPS.

## Maintenance

- After changing `common.py`, redeploy the VPS: `./scripts/deploy_vps.sh`. The Mac and VPS must run the same projection code.
- After changing `config.json`, re-run `./scripts/deploy_vps.sh` and, if intervals changed, `./scripts/install_vps_cron.sh` and `./scripts/install_mac.sh`.
- If notifications stop, check in this order: `./scripts/test_notification.sh`, the LaunchAgent (`launchctl print`), `data/monitor-error.log`, the VPS cron entry, `vps_notifier.py --status`, `data/cron.log`.
- Telegram notification rules are device-specific. If private-chat notifications are globally muted on the phone, the bot chat must be an explicit exception.
- If a provider's window length changes, no code change is needed — the next Mac sync carries the new `windowMinutes`.

## Review corrections (post-review pass)

An external review found three defects in the first public-release cut. All three are fixed and covered by tests.

1. **Unsafe remote quoting in `install_vps_cron.sh`.** It passed the remote directory and cron schedule as separate `ssh` arguments. `ssh` concatenates its command arguments into one string for the remote login shell, so `/home/deploy/my notifier` split into two words and the schedule's `*` glob-expanded against the remote directory. Every remote argument is now quoted with `shell_quote()`. This was the only affected call site; every other `ssh` invocation already passed a single pre-quoted string.

2. **Cron intervals that do not divide 60.** `*/7` restarts at the top of each hour, firing at :49, :56, then :00. `common.cron_schedule()` now accepts only divisors of 60 (plus 60 itself, rendered `0 * * * *` rather than the never-firing `*/60`), and `validate_config()` calls it so a bad interval fails at setup.

3. **Multiple CodexBar accounts silently reduced to the first.** `fetch_provider()` returned `entries[0]`. It now returns every record, and `common.require_single_record()` returns the sole record or stops and names the accounts it saw. It never guesses.

### Follow-up: account selection does not exist for these providers

Two further review passes drove this to ground.

The first noted that selection was implemented after parsing while CodexBar was invoked with no account flags, so a configured secondary account could never be returned. True — but the obvious fix, always passing `--all-accounts`, is wrong.

The second noted that `--all-accounts` advertised a Codex account as selectable while `--account` rejected it. Also true. Probing CodexBar 0.37.2 directly settled the question:

```
codexbar usage --provider claude ... --all-accounts   -> "No token accounts configured for claude."      exit 1
codexbar usage --provider claude ... --account X      -> "No token accounts configured for claude."      exit 1
codexbar usage --provider claude ... --account-index 1 -> "No token accounts configured for claude."     exit 1
codexbar usage --provider codex  ... --account X      -> "Error: codex does not support token accounts." exit 1
codexbar usage --provider codex  ... --account-index 1 -> "Error: codex does not support token accounts." exit 1
codexbar usage --provider codex  ... --all-accounts   -> one record                                      exit 0
```

`--account`, `--account-index`, and `--all-accounts` address CodexBar **token accounts**, meaning accounts declared in its config file. Claude has none (it signs in through OAuth/cookies). Codex has none and says so explicitly; its `--all-accounts` support is a separate code path that enumerates visible Codex accounts but gives no way to select one.

So account selection is not merely unimplemented here — it is unavailable for both providers this project supports. Discovery that lists an account you cannot then select is worse than no discovery, because it invites a config value that will always fail.

What was done:

- Account selection and discovery were **removed**. `config.json` has no `accounts` key, and there is no `--list-accounts` command.
- `build_codexbar_command()` passes no account flags at all, and names the documented `usage` subcommand. Tests assert the argv element for element, including that no `--account*` or `--all-accounts` flag ever appears.
- `require_single_record()` keeps the safety property that motivated the original finding: if CodexBar ever returns several records, the notifier stops and names them rather than monitoring an arbitrary account.
- Provider errors are **never downgraded**. An earlier draft treated any `--all-accounts` failure as "single account", which would have swallowed an expired-credentials error. `run_codexbar()` now raises `CodexbarError` for every failure, carrying CodexBar's own message, and checks stdout as well as the exit code because CodexBar reports provider errors as JSON on stdout — sometimes while exiting 0.
- Multi-account support is documented as a limitation, to be revisited only if CodexBar gains real selection for Claude and Codex.

Verified against the real binary on macOS: `--status` reads live Claude and Codex resets from a fresh `git archive` extraction, and the record shape (`usage.accountEmail`, `usage.primary.resetsAt`, `windowMinutes`) matches what the code assumes.

## Public-release status

This repository is the cleaned, publishable version. It was built as a fresh export with new Git history; no commits, `.env`, `data/`, logs, or personal configuration were carried over from the private original.

The release candidate is hosted in the private GitHub repository `blizzardbase/codexbar-reset-notifier`. PR #1 (`Codex/public-release-prep` → `main`) is the active release review. Keep the repository private until the VPS deployment script has been exercised against a throwaway host and the release checklist is complete.

Completed for release:

1. The personal VPS alias and remote path are parameterized into `config.json`.
2. A generic VPS deploy script and an idempotent cron installer/uninstaller exist.
3. Local-only and VPS-backed modes are explicit and documented.
4. Tests cover cycle projection, deduplication, stale data, timezone and DST formatting, invalid input, and atomic writes.
5. The repository is free of tokens, chat ids, real hostnames, usernames, and personal paths.
6. The offline-projection assumption is documented in the README, AGENTS.md, and here.

## Handoff instructions

- Read `AGENTS.md` before touching code. It records the invariants (no AI calls, no hard-coded intervals, no secrets outside `.env`, no inbound ports, idempotent scripts).
- Run the full verification set before and after any change:

  ```bash
  python3 -m py_compile common.py monitor.py vps_notifier.py configure_telegram.py
  python3 -m unittest discover -v
  for f in scripts/*.sh; do bash -n "$f"; done
  python3 monitor.py --validate-config --config config.example.json
  git diff --check
  ```

- Tests must never send a real Telegram message or touch the network.
- The current release branch is `Codex/public-release-prep`, tracking `origin/Codex/public-release-prep` in the private GitHub repository. PR #1 is the source of truth until it merges.
- For approved work, push updates to the feature branch automatically, resolve all actionable CodeRabbit comments, and repeat until every required check is green and no actionable review comments remain. Then merge and sync `main` locally.
