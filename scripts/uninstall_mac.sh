#!/usr/bin/env bash
# Remove this project's macOS LaunchAgents. Touches nothing else.
set -euo pipefail

for LABEL in local.codexbar-reset-notifier local.codexbar-reset-usage-bot; do
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled $LABEL."
done
echo "Local data/ and .env were left in place; delete them yourself if you want."
