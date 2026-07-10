#!/usr/bin/env python3
"""VPS-side notifier for the CodexBar reset notifier.

Receives confirmed reset anchors from the Mac over SSH (``--ingest``), projects
future session and weekly cycles from those anchors (``--check``), and sends one
Telegram DM per trigger reset. It never contacts Claude or Codex and holds no
provider credentials.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import common
from common import ConfigError

SCHEDULE_FILE = common.DATA_DIR / "schedule.json"
STATE_FILE = common.DATA_DIR / "vps-state.json"


def load_schedule() -> dict:
    schedule = common.read_json(SCHEDULE_FILE, {})
    return schedule if isinstance(schedule, dict) else {}


def validate_payload(payload: object) -> dict:
    """Reject anything that is not a well-formed schedule payload."""
    if not isinstance(payload, dict):
        raise ValueError("schedule payload must be a JSON object")
    records = payload.get("records")
    if not isinstance(records, dict) or not records:
        raise ValueError("schedule payload must contain a non-empty records object")
    for provider, entry in records.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("usage"), dict):
            raise ValueError(f"record for {provider} is missing a usage object")
    updated_at = payload.get("updatedAt")
    if not isinstance(updated_at, str):
        raise ValueError("schedule payload must contain an updatedAt timestamp")
    common.parse_timestamp(updated_at)
    return payload


def run_ingest() -> int:
    """Read a schedule payload from stdin and store it atomically."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid schedule JSON: {exc}") from None
    try:
        validated = validate_payload(payload)
    except ValueError as exc:
        raise RuntimeError(f"invalid schedule payload: {exc}") from None
    common.atomic_json_write(SCHEDULE_FILE, validated)
    return 0


def run_check(config: dict) -> int:
    schedule = load_schedule()
    records = schedule.get("records") or {}
    if not records:
        print("WARNING: no schedule has been synced yet", file=sys.stderr)
        return 0

    now = datetime.now(timezone.utc)
    if not common.schedule_is_fresh(schedule, now, config["stale_data_minutes"]):
        # Projection continues: surviving a long Mac outage is the whole point.
        print(
            "WARNING: schedule is older than the stale-data threshold; times are projected",
            file=sys.stderr,
        )

    state = common.read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}

    decision = common.evaluate_reset(
        records, now, state, config["timezone"], config["providers"]
    )
    if decision.action == "send":
        common.notify(decision.message)
    if decision.action in ("send", "seed", "expired"):
        common.atomic_json_write(STATE_FILE, common.mark_sent(state, decision.key))
    return 0


def run_status(config: dict) -> int:
    schedule = load_schedule()
    records = schedule.get("records") or {}
    now = datetime.now(timezone.utc)
    if not records:
        print("No schedule synced yet.")
        return 0
    fresh = common.schedule_is_fresh(schedule, now, config["stale_data_minutes"])
    print(f"Last Mac sync: {schedule.get('updatedAt', 'unknown')} ({'fresh' if fresh else 'stale'})")
    for provider in config["providers"]:
        for slot, label in (("primary", "session"), ("secondary", "weekly")):
            reset_at = common.next_reset(common.get_window(records, provider, slot), now)
            when = (
                reset_at.astimezone(common.config_timezone(config)).isoformat()
                if reset_at
                else "unavailable"
            )
            print(f"{common.provider_label(provider)} {label} next reset: {when}")
    return 0


def run_test(config: dict) -> int:
    """Send the exact production message shape using the stored schedule."""
    schedule = load_schedule()
    records = schedule.get("records") or {}
    if not records:
        raise RuntimeError("no schedule has been synced yet; run the Mac sync first")
    now = datetime.now(timezone.utc)
    preview = common.build_reset_message(records, now, config["timezone"], config["providers"])
    common.notify("VPS delivery test. The next real notification will look like this.\n\n" + preview)
    print("Test message sent. Deduplication state was not modified.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CodexBar reset notifier (VPS side)")
    parser.add_argument("--config", type=Path, default=None, help="path to config.json")
    parser.add_argument("--ingest", action="store_true", help="read a schedule payload from stdin")
    parser.add_argument("--check", action="store_true", help="send a notification if one is due")
    parser.add_argument("--status", action="store_true", help="print freshness and projected resets")
    parser.add_argument("--test", action="store_true", help="send a preview message")
    parser.add_argument("--validate-config", action="store_true", help="validate config and exit")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    common.load_env()

    # Ingest must work before config is ever read so a Mac sync can bootstrap
    # a freshly deployed VPS that has not been configured yet.
    if args.ingest:
        return run_ingest()

    config = common.load_config(args.config)
    if args.validate_config:
        print(f"Configuration is valid ({config['notification_mode']} mode).")
        return 0
    if args.check:
        return run_check(config)
    if args.status:
        return run_status(config)
    if args.test:
        return run_test(config)
    build_parser().error("choose one of --ingest, --check, --status, --test, --validate-config")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
