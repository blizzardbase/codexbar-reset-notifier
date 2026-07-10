#!/usr/bin/env bash
# Send one test Telegram message through whichever component will deliver the
# real notifications. Deduplication state is never modified.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 "$ROOT/monitor.py" --validate-config >/dev/null
MODE="$(python3 "$ROOT/common.py" notification_mode)"

if [ "$MODE" = "local" ]; then
  echo "Local-only mode: sending from this Mac ..."
  exec python3 "$ROOT/monitor.py" --test
fi

TARGET="$(python3 "$ROOT/common.py" ssh_target)"
REMOTE_DIR="$(python3 "$ROOT/common.py" vps_remote_dir)"
REMOTE_Q="$(python3 -c 'import sys, common; print(common.shell_quote(sys.argv[1]))' "$REMOTE_DIR")"

echo "VPS-backed mode: sending from $TARGET ..."
ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "python3 $REMOTE_Q/vps_notifier.py --test"
