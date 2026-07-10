#!/usr/bin/env bash
# Remove only this project's crontab entry from the VPS.
# Unrelated crontab entries are preserved. Files under vps_remote_dir are left
# alone; delete them yourself if you also want the code gone.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 "$ROOT/monitor.py" --validate-config >/dev/null
TARGET="$(python3 "$ROOT/common.py" ssh_target)"

ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
MARKER="# codexbar-reset-notifier"

EXISTING="$(crontab -l 2>/dev/null || true)"
if ! printf '%s\n' "$EXISTING" | grep -qF "$MARKER"; then
  echo "No codexbar-reset-notifier cron entry was present."
  exit 0
fi

KEPT="$(printf '%s\n' "$EXISTING" | grep -vF "$MARKER" || true)"
if [ -n "$KEPT" ]; then
  printf '%s\n' "$KEPT" | crontab -
else
  crontab -r 2>/dev/null || true
fi

echo "Removed the codexbar-reset-notifier cron entry."
REMOTE

echo "Done. Remote files under vps_remote_dir were not deleted."
