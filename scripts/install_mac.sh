#!/usr/bin/env bash
# Install the per-user macOS LaunchAgents for schedule sync and /usage replies.
# Idempotent: re-running replaces the existing agents. No sudo, no system files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_LABEL="local.codexbar-reset-notifier"
USAGE_LABEL="local.codexbar-reset-usage-bot"
MONITOR_PLIST="$HOME/Library/LaunchAgents/$MONITOR_LABEL.plist"
USAGE_PLIST="$HOME/Library/LaunchAgents/$USAGE_LABEL.plist"

cd "$ROOT"

if [ ! -f "$ROOT/config.json" ]; then
  echo "ERROR: config.json not found. Copy config.example.json to config.json first." >&2
  exit 1
fi

python3 "$ROOT/monitor.py" --validate-config

PYTHON_BIN="$(command -v python3)"
INTERVAL="$(python3 "$ROOT/common.py" mac_sync_interval_seconds)"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"

python3 - "$ROOT/launchagent.plist.template" "$MONITOR_PLIST" "$ROOT" "$MONITOR_LABEL" "$PYTHON_BIN" "$INTERVAL" <<'PY'
import sys
template, target, root, label, python_bin, interval = sys.argv[1:7]
text = open(template).read()
for token, value in (
    ("__ROOT__", root),
    ("__LABEL__", label),
    ("__PYTHON__", python_bin),
    ("__INTERVAL__", interval),
):
    text = text.replace(token, value)
open(target, "w").write(text)
PY

python3 - "$ROOT/usage-bot-launchagent.plist.template" "$USAGE_PLIST" "$ROOT" "$USAGE_LABEL" "$PYTHON_BIN" <<'PY'
import sys
template, target, root, label, python_bin = sys.argv[1:6]
text = open(template).read()
for token, value in (
    ("__ROOT__", root),
    ("__LABEL__", label),
    ("__PYTHON__", python_bin),
):
    text = text.replace(token, value)
open(target, "w").write(text)
PY

launchctl bootout "gui/$UID/$MONITOR_LABEL" 2>/dev/null || true
launchctl bootout "gui/$UID/$USAGE_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$MONITOR_PLIST"
launchctl bootstrap "gui/$UID" "$USAGE_PLIST"
launchctl kickstart -k "gui/$UID/$MONITOR_LABEL"
launchctl kickstart -k "gui/$UID/$USAGE_LABEL"

echo "Installed and started $MONITOR_LABEL (every ${INTERVAL}s)."
echo "Installed and started $USAGE_LABEL (Telegram /usage listener)."
echo "Logs: $ROOT/data/monitor.log and $ROOT/data/monitor-error.log"
echo "Usage logs: $ROOT/data/usage-bot.log and $ROOT/data/usage-bot-error.log"
