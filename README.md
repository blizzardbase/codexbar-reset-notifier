# CodexBar Reset Notifier

Get one private Telegram message the moment your Claude session limit resets — even when your Mac is asleep or switched off.

The message also tells you how many minutes until Codex resets, and when both weekly limits roll over.

**No AI tokens are consumed.** This project never calls Claude, Codex, an LLM, or any paid AI API. It is plain Python, SSH, cron, the CodexBar CLI, and the Telegram Bot API.

---

## What you get

```text
Claude session reset has happened. Codex will reset in about 11 minutes.

Claude weekly reset: 7:59 PM, Sun New York time (2 days 11 hours)
Codex weekly reset: 4:00 AM, Fri New York time (6 days 19 hours)
```

Every number is calculated fresh. There is no hard-coded gap between Claude and Codex, no 15-minute warning, no separate Codex message, and no usage percentages. One reset, one message.

---

## Why a VPS is needed

A Mac-only notifier stops the moment the lid closes. Your session still resets while you sleep, but nothing tells you.

So the work is split. Your Mac knows the truth — CodexBar reads the real reset times from the providers. A small always-on server (a VPS) remembers those times and keeps counting forward on its own.

The VPS never talks to Claude or Codex. It receives two facts per provider — *when the window next resets* and *how long the window is* — and repeats the cycle from there. Your Mac corrects it every few minutes whenever it is awake.

You can skip the VPS entirely (see **Local-only mode**), but then notifications only arrive while the Mac is awake.

---

## Architecture

```
┌──────────────────────────┐        SSH (outbound only)       ┌────────────────────────┐
│  Your Mac                │  ──────────────────────────────► │  Your VPS              │
│                          │                                  │                        │
│  CodexBar CLI            │   reset anchors + window lengths │  vps_notifier.py       │
│        ▼                 │   (no credentials, no usage %)   │    projects cycles     │
│  monitor.py              │                                  │    dedupes resets      │
│    every 5 min           │                                  │    every 1 min (cron)  │
│    (LaunchAgent)         │                                  │          │             │
└──────────────────────────┘                                  └──────────┼─────────────┘
                                                                         ▼
                                                                  Telegram DM
```

1. A macOS LaunchAgent runs `monitor.py` every five minutes.
2. `monitor.py` asks the CodexBar CLI for Claude and Codex reset data.
3. It sends only the reset anchors and window lengths to the VPS over SSH.
4. VPS cron runs `vps_notifier.py --check` every minute.
5. At each Claude reset the VPS sends one Telegram DM and records it, so it is never sent twice.
6. The next Mac sync overwrites the anchors with live data, correcting any drift.

---

## Privacy model

- Your Claude and Codex credentials never leave your Mac. The VPS has none.
- The VPS receives only `resetsAt` timestamps and `windowMinutes` integers.
- Usage percentages, account emails, prompts, conversations, and code are never sent anywhere.
- Telegram credentials live only in `.env`, which is git-ignored on both machines.
- The VPS opens no inbound ports. All traffic is outbound: your Mac connects to it over SSH; it connects to Telegram over HTTPS.
- Nothing is sent to an AI provider, and no model tokens are spent.

---

## Requirements

- **macOS** with [CodexBar](https://github.com/steipete/CodexBar) installed, including its command-line tool
- Claude and/or Codex signed in inside CodexBar
- **Python 3.9 or newer** on the Mac and on the VPS (no third-party packages)
- For VPS-backed mode: any always-on Linux server you can reach with `ssh` using key authentication
- A **Telegram bot** and a private chat with it

On a minimal Linux VPS, make sure the system `tzdata` package is present — Python's `zoneinfo` needs it.

---

## Installing CodexBar

Install CodexBar and open it once so Claude and Codex sign in. Then check the CLI answers:

```bash
codexbar --provider claude --format json --json-only
```

If that prints JSON containing `resetsAt`, you are ready. If `codexbar` is not on your `PATH`, note its full path — you will put it in `config.json` as `codexbar_path`.

---

## Creating a Telegram bot

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot` and follow the prompts (a name, then a username ending in `bot`).
3. BotFather replies with a **token** that looks like `123456789:AA-example-token-replace-me`.
4. Keep it private. Anyone with the token controls the bot.

### Start a private chat with the bot

Telegram bots cannot message you first. Open your new bot's chat and press **Start** (or send `/start`). Without this, delivery will fail.

---

## Setup

Clone the repository, then create your two configuration files:

```bash
cp .env.example .env
cp config.example.json config.json
```

Open `.env` and paste your BotFather token:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA-example-token-replace-me
TELEGRAM_CHAT_ID=
```

Now let the tool find your chat id. Make sure you pressed **Start** in the bot chat first:

```bash
python3 configure_telegram.py
```

It writes `TELEGRAM_CHAT_ID` into `.env` for you and prints nothing sensitive.

Finally, open `config.json` and set at least your `timezone` (an [IANA name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) such as `America/New_York` or `Europe/London`).

Check the configuration is valid at any time:

```bash
python3 monitor.py --validate-config
```

---

### Local-only mode

Everything runs on the Mac. Simple, but **notifications stop whenever the Mac is asleep or off** — which is exactly when a reset is most likely to go unnoticed.

In `config.json`:

```json
"notification_mode": "local"
```

Then:

```bash
./scripts/test_notification.sh   # confirm Telegram delivery works
./scripts/install_mac.sh         # run every 5 minutes in the background
```

Because macOS wakes only briefly, a reset that happens overnight is announced when the Mac next wakes — or missed entirely if more than two minutes have passed. Use VPS-backed mode if that matters to you.

---

### VPS-backed mode

Notifications continue while the Mac is off. This is the recommended setup.

**1. Confirm passwordless SSH works.** Either use a host alias from `~/.ssh/config`, or a plain hostname:

```bash
ssh your-vps-host 'echo ok'
```

If that prints `ok` without asking for a password, you are set. Otherwise configure SSH key authentication first.

**2. Fill in `config.json`:**

```json
{
  "timezone": "America/New_York",
  "providers": ["claude", "codex"],
  "codexbar_path": null,
  "notification_mode": "vps",
  "vps_host": "your-vps-host",
  "vps_user": "your-vps-user",
  "vps_remote_dir": "/home/your-vps-user/codexbar-reset-notifier",
  "mac_sync_interval_seconds": 300,
  "vps_check_interval_seconds": 60,
  "stale_data_minutes": 30
}
```

Leave `vps_user` as `""` if `vps_host` is an SSH config alias that already specifies the user.

**3. Deploy and schedule:**

```bash
./scripts/deploy_vps.sh          # copy the VPS half over SSH
./scripts/run_once.sh            # push the first real schedule
./scripts/install_vps_cron.sh    # check every minute on the VPS
./scripts/test_notification.sh   # confirm the VPS can reach Telegram
./scripts/install_mac.sh         # keep the schedule fresh from the Mac
```

`deploy_vps.sh` copies `.env` to the VPS (it needs the Telegram token) and sets it to mode `600`. It creates nothing outside `vps_remote_dir`, needs no root, and opens no ports.

---

## Configuration reference

Secrets live in `.env`. Everything else lives in `config.json`.

### `.env`

| Key | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | The token from BotFather. |
| `TELEGRAM_CHAT_ID` | Your private chat id. Filled in by `configure_telegram.py`. |

### `config.json`

| Key | Meaning |
| --- | --- |
| `timezone` | IANA timezone used to render reset times, e.g. `America/New_York`. |
| `providers` | Ordered list. The **first** provider triggers the notification; the **second** is the one counted down to. |
| `codexbar_path` | Full path to the `codexbar` binary, or `null` to search `PATH` and the usual Homebrew locations. |
| `notification_mode` | `"local"` or `"vps"`. |
| `vps_host` | SSH host alias or hostname. Required in `vps` mode. |
| `vps_user` | SSH username, or `""` when the alias supplies it. |
| `vps_remote_dir` | Absolute path on the VPS. Everything is confined here. |
| `mac_sync_interval_seconds` | How often the Mac reads CodexBar and syncs. Default `300`. |
| `vps_check_interval_seconds` | How often the VPS checks for a due reset. Must be a whole number of minutes that divides 60 evenly (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30) or exactly 60. Default `60`. |
| `stale_data_minutes` | How old a synced schedule may be before it is reported as stale. Default `30`. |

---

## Testing

Send a real message through whichever component will deliver the real ones:

```bash
./scripts/test_notification.sh
```

In `vps` mode this sends from the VPS using the stored schedule, so it proves the whole chain. It never touches the deduplication state, so it cannot cause a missed or duplicate real notification.

Inspect what the system currently believes:

```bash
python3 monitor.py --status              # what CodexBar reports right now
ssh your-vps-host 'python3 /home/your-vps-user/codexbar-reset-notifier/vps_notifier.py --status'
```

Run the automated test suite (no network access, no messages sent):

```bash
python3 -m unittest discover -v
```

---

## How offline projection works

Each provider reports two things per window: when it next resets (`resetsAt`, the *anchor*) and how long the window lasts (`windowMinutes`).

Given those, the next reset after any moment is pure arithmetic — repeatedly add the window length to the anchor until you land in the future. The VPS does exactly that, once a minute, for both the session window and the weekly window. No interval is ever assumed; if a provider stops reporting `windowMinutes`, that line is dropped rather than guessed.

**The assumption you are relying on:** the VPS projects future resets from the last confirmed provider anchor and window length. If the provider changes its schedule while your Mac is offline, the projection may temporarily drift. The next live Mac sync corrects it.

In practice the Mac syncs every five minutes whenever it is awake, so drift is bounded by how long your Mac stays off.

---

## Known limitations

- **Projection drift.** While the Mac is offline the VPS cannot learn about a provider-side schedule change. It self-corrects on the next sync.
- **Weekly line needs an interval.** If a provider reports a weekly `resetsAt` without `windowMinutes`, the weekly line survives until that timestamp passes, then disappears rather than being guessed.
- **Two-minute grace window.** A reset is announced only if the VPS notices it within two minutes. If the VPS is down longer than that, the reset is recorded silently and the next one is announced normally. No late or duplicate alerts.
- **First run is silent.** On a brand-new install the current cycle is adopted without notifying, so you are not messaged about a reset that happened before you installed anything.
- **Cron granularity.** cron's `*/N` step restarts every hour, so an N that does not divide 60 (seven minutes, say) leaves an irregular gap across each hour boundary. Only divisors of 60 are accepted, plus 60 itself for an hourly check. Anything else is rejected at validation time rather than silently misbehaving.
- **No `%` in `vps_remote_dir`.** cron reserves it. Rejected at validation time.
- **One account per provider.** CodexBar exposes a single account for Claude and for Codex, and offers no working way to pick among several: `--account`, `--account-index`, and `--all-accounts` all address CodexBar *token accounts*, which neither provider has. Claude answers `No token accounts configured for claude.` and Codex answers `codex does not support token accounts.` So this release watches each provider's default account. If CodexBar ever returns more than one record, the notifier stops and names them rather than monitoring an arbitrary one. Multi-account support is out of scope until CodexBar offers selection for these providers.
- **`data/cron.log` grows slowly** and is never rotated. It is tiny, but delete it if you like.
- **Local-only mode cannot notify while the Mac sleeps.** This is the whole reason VPS mode exists.

---

## Troubleshooting

**No message ever arrives.**
Run `./scripts/test_notification.sh`. If it fails, the problem is Telegram configuration, not scheduling. Confirm you pressed **Start** in the bot chat and that `.env` has both values.

**`codexbar CLI was not found`.**
Set `codexbar_path` in `config.json` to the binary's full path. Under a LaunchAgent the `PATH` is minimal, which is why `/opt/homebrew/bin` and `/usr/local/bin` are searched explicitly.

**The LaunchAgent is not running.**
```bash
launchctl print "gui/$UID/local.codexbar-reset-notifier" | grep -E 'state|last exit'
tail -n 20 data/monitor-error.log
```
If `python3` on your Mac comes from a version manager (pyenv, asdf), its path can change. Re-run `./scripts/install_mac.sh` after switching Python versions.

**`VPS sync failed`.**
Check `ssh your-vps-host 'echo ok'` still works from a plain shell. LaunchAgents do not load your interactive shell profile, so the SSH key must be usable without an agent prompt.

**The VPS is silent.**
```bash
ssh your-vps-host 'crontab -l | grep codexbar'
ssh your-vps-host 'python3 /home/your-vps-user/codexbar-reset-notifier/vps_notifier.py --status'
ssh your-vps-host 'tail -n 20 /home/your-vps-user/codexbar-reset-notifier/data/cron.log'
```
`--status` tells you when the Mac last synced and what the VPS expects next.

**`CodexBar reports N accounts for provider ...`.**
CodexBar returned more than one account and this release cannot choose between them, so it stopped rather than watch an arbitrary one. Sign out of the extra accounts in CodexBar. See **Known limitations**.

**`codexbar failed for provider ...`.**
The message after the colon is CodexBar's own. Run the same command by hand to see it in context:
```bash
codexbar usage --provider claude --format json --json-only
```
Expired credentials usually show as an authorization error; open CodexBar and sign in again.

**Times are in the wrong timezone.**
`timezone` in `config.json` controls only how times are *displayed*. Change it and redeploy.

---

## Updating

```bash
git pull
./scripts/deploy_vps.sh          # push the new VPS half
./scripts/install_mac.sh         # re-render and restart the LaunchAgent
```

Both scripts are idempotent. Your `.env`, `config.json`, and `data/` are untouched by `git pull` because they are ignored.

---

## Uninstalling

```bash
./scripts/uninstall_vps_cron.sh   # removes only this project's cron entry
./scripts/uninstall_mac.sh        # removes only this project's LaunchAgent
```

Neither script touches unrelated cron entries, other LaunchAgents, or system services. To finish, delete `data/`, `.env`, and the remote directory yourself.

---

## Security guidance

- Never commit `.env`. It is git-ignored; keep it that way.
- Treat the bot token like a password. If it leaks, revoke it with `/revoke` in BotFather.
- Use SSH key authentication and `BatchMode` — every script here already does.
- Give the VPS a dedicated non-root user. Nothing in this project needs `sudo`, writes outside `vps_remote_dir`, or modifies `/etc`, firewall rules, or system services.
- `.env` is written with mode `600` on both machines.
- Errors never print your token or chat id.

---

## No AI tokens are used

Worth repeating, because the project name mentions Codex and Claude: this tool **reads reset timestamps and does arithmetic on them.** It sends no prompts, spawns no agents, and calls no model API. Running it costs you nothing from any AI plan.

---

## License

MIT © Blizzardbase. See [LICENSE](LICENSE).
