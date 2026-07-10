#!/usr/bin/env bash
# Install the per-user macOS LaunchAgent that runs monitor.py on a schedule.
# Idempotent: re-running replaces the existing agent. No sudo, no system files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="local.codexbar-reset-notifier"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$ROOT"

if [ ! -f "$ROOT/config.json" ]; then
  echo "ERROR: config.json not found. Copy config.example.json to config.json first." >&2
  exit 1
fi

python3 "$ROOT/monitor.py" --validate-config

PYTHON_BIN="$(command -v python3)"
INTERVAL="$(python3 "$ROOT/common.py" mac_sync_interval_seconds)"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"

python3 - "$ROOT/launchagent.plist.template" "$PLIST" "$ROOT" "$LABEL" "$PYTHON_BIN" "$INTERVAL" <<'PY'
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

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "Installed and started $LABEL (every ${INTERVAL}s)."
echo "Logs: $ROOT/data/monitor.log and $ROOT/data/monitor-error.log"
