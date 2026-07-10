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
# Rejects intervals that would not fire evenly across the hour boundary.
SCHEDULE="$(python3 "$ROOT/common.py" cron_schedule)"

# ssh concatenates its command arguments into a single string and hands it to the
# remote login shell, which then word-splits and glob-expands it. Quote each
# argument for that remote shell, or a path with spaces and a schedule
# containing '*' will arrive mangled.
REMOTE_ARGS="$(python3 -c \
  'import sys, common; print(" ".join(common.shell_quote(a) for a in sys.argv[1:]))' \
  "$REMOTE_DIR" "$SCHEDULE")"

ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "bash -s -- $REMOTE_ARGS" <<'REMOTE'
set -euo pipefail
REMOTE_DIR="$1"
SCHEDULE="$2"
MARKER="# codexbar-reset-notifier"

PYTHON_BIN="$(command -v python3)"
mkdir -p "$REMOTE_DIR/data"

QUOTED_DIR="$(printf '%q' "$REMOTE_DIR")"
QUOTED_PYTHON="$(printf '%q' "$PYTHON_BIN")"
LINE="$SCHEDULE cd $QUOTED_DIR && $QUOTED_PYTHON $QUOTED_DIR/vps_notifier.py --check >> $QUOTED_DIR/data/cron.log 2>&1 $MARKER"

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
