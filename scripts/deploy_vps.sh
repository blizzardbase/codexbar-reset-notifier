#!/usr/bin/env bash
# Copy the VPS half of the project to the remote host over SSH.
# Idempotent: re-running overwrites the same files. No sudo, no system changes,
# no inbound ports. Everything lands inside config.vps_remote_dir.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f "$ROOT/config.json" ]; then
  echo "ERROR: config.json not found. Copy config.example.json to config.json first." >&2
  exit 1
fi
if [ ! -f "$ROOT/.env" ]; then
  echo "ERROR: .env not found. The VPS needs the Telegram credentials to send messages." >&2
  exit 1
fi

python3 "$ROOT/monitor.py" --validate-config >/dev/null

MODE="$(python3 "$ROOT/common.py" notification_mode)"
if [ "$MODE" != "vps" ]; then
  echo "ERROR: notification_mode is '$MODE'. Set it to 'vps' before deploying." >&2
  exit 1
fi

TARGET="$(python3 "$ROOT/common.py" ssh_target)"
REMOTE_DIR="$(python3 "$ROOT/common.py" vps_remote_dir)"
# Quote the remote path once, using the same helper the Python code uses.
REMOTE_Q="$(python3 -c 'import sys, common; print(common.shell_quote(sys.argv[1]))' "$REMOTE_DIR")"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

echo "Checking python3 on $TARGET ..."
ssh "${SSH_OPTS[@]}" "$TARGET" 'command -v python3 >/dev/null' \
  || { echo "ERROR: python3 was not found on the VPS." >&2; exit 1; }

echo "Creating $REMOTE_DIR ..."
# REMOTE_Q is deliberately expanded locally after common.shell_quote().
# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p $REMOTE_Q $REMOTE_Q/data && chmod 700 $REMOTE_Q/data"

echo "Copying files ..."
# shellcheck disable=SC2029
tar -cf - -C "$ROOT" vps_notifier.py common.py config.json requirements.txt .env \
  | ssh "${SSH_OPTS[@]}" "$TARGET" "tar -xf - -C $REMOTE_Q"

# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "$TARGET" "chmod 600 $REMOTE_Q/.env"

echo "Validating the remote configuration ..."
# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "$TARGET" "python3 $REMOTE_Q/vps_notifier.py --validate-config"

echo "Deployed to $TARGET:$REMOTE_DIR"
echo "Next: ./scripts/install_vps_cron.sh"
