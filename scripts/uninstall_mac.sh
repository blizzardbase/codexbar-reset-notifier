#!/usr/bin/env bash
# Remove this project's macOS LaunchAgent. Touches nothing else.
set -euo pipefail

LABEL="local.codexbar-reset-notifier"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Uninstalled $LABEL."
echo "Local data/ and .env were left in place; delete them yourself if you want."
