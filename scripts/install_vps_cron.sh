#!/usr/bin/env bash
# Install the VPS crontab entry that checks for a due reset.
# Idempotent and non-destructive: only lines carrying this project's marker are
# rewritten, so unrelated crontab entries are preserved exactly. No sudo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 "$ROOT/monitor.py" --validate-config >/dev/null

TARGET="$(python3 "$ROOT/common.py" ssh_target)"
REMOTE_DIR="$(python3 "$ROOT/common.py" vps_remote_dir)"
CHECK_SECONDS="$(python3 "$ROOT/common.py" vps_check_interval_seconds)"

if [ $((CHECK_SECONDS % 60)) -ne 0 ] || [ "$CHECK_SECONDS" -lt 60 ] || [ "$CHECK_SECONDS" -gt 3540 ]; then
  echo "ERROR: vps_check_interval_seconds must be a whole number of minutes between 60 and 3540." >&2
  exit 1
fi

MINUTES=$((CHECK_SECONDS / 60))
if [ "$MINUTES" -eq 1 ]; then SCHEDULE="* * * * *"; else SCHEDULE="*/$MINUTES * * * *"; fi

ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" bash -s -- "$REMOTE_DIR" "$SCHEDULE" <<'REMOTE'
set -euo pipefail
REMOTE_DIR="$1"
SCHEDULE="$2"
MARKER="# codexbar-reset-notifier"

PYTHON_BIN="$(command -v python3)"
mkdir -p "$REMOTE_DIR/data"

LINE="$SCHEDULE cd \"$REMOTE_DIR\" && \"$PYTHON_BIN\" \"$REMOTE_DIR/vps_notifier.py\" --check >> \"$REMOTE_DIR/data/cron.log\" 2>&1 $MARKER"

EXISTING="$(crontab -l 2>/dev/null || true)"
# Drop only our own marked lines, keep every other entry byte for byte.
KEPT="$(printf '%s\n' "$EXISTING" | grep -vF "$MARKER" || true)"

{
  if [ -n "$KEPT" ]; then printf '%s\n' "$KEPT"; fi
  printf '%s\n' "$LINE"
} | crontab -

echo "Cron entry installed on the VPS:"
crontab -l | grep -F "$MARKER"
REMOTE

echo "Done. The VPS now checks for a due reset on schedule: $SCHEDULE"
