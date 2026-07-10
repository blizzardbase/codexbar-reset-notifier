#!/usr/bin/env bash
# Run one monitor cycle by hand: read CodexBar, then sync (vps mode) or
# evaluate and notify (local mode). Useful for verifying a fresh install.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

exec python3 "$ROOT/monitor.py" "$@"
