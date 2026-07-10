#!/usr/bin/env python3
"""VPS-side notifier for the CodexBar reset notifier.

Receives confirmed reset anchors from the Mac over SSH (``--ingest``), projects
future session and weekly cycles from those anchors (``--check``), and sends one
Telegram DM per trigger reset. It never contacts Claude or Codex and holds no
provider credentials.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import common
from common import ConfigError

SCHEDULE_FILE = common.DATA_DIR / "schedule.json"
STATE_FILE = common.DATA_DIR / "vps-state.json"
LOCK_FILE = common.DATA_DIR / "vps-check.lock"


def load_schedule() -> dict:
    """Return the last schedule synced from the Mac."""
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
        usage = entry["usage"]
        if not isinstance(usage.get("primary"), dict):
            raise ValueError(f"record for {provider} is missing a primary reset window")
        for slot in ("primary", "secondary"):
            if slot not in usage:
                continue
            window = usage[slot]
            if not isinstance(window, dict):
                raise ValueError(f"{slot} window for {provider} must be an object")
            try:
                common.parse_timestamp(window.get("resetsAt"))
            except ValueError:
                raise ValueError(f"{slot} window for {provider} has an invalid resetsAt") from None
            if "windowMinutes" in window and common.window_minutes(window) is None:
                raise ValueError(f"{slot} window for {provider} has invalid windowMinutes")
    updated_at = payload.get("updatedAt")
    if not isinstance(updated_at, str):
        raise ValueError("schedule payload must contain an updatedAt timestamp")
    common.parse_timestamp(updated_at)
    return payload


@contextmanager
def check_lock() -> Iterator[None]:
    """Serialize VPS check-and-send transactions across cron processes."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
    """Project the trigger cycle and send at most one notification."""
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

    with check_lock():
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
        elif decision.action == "unavailable":
            trigger = config["providers"][0] if config["providers"] else "trigger provider"
            print(
                f"WARNING: no projectable session schedule for {common.provider_label(trigger)}",
                file=sys.stderr,
            )
    return 0


def run_status(config: dict) -> int:
    """Print schedule freshness and projected provider resets."""
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
    """Build the VPS command-line parser with one required action."""
    parser = argparse.ArgumentParser(description="CodexBar reset notifier (VPS side)")
    parser.add_argument("--config", type=Path, default=None, help="path to config.json")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--ingest", action="store_true", help="read a schedule payload from stdin")
    action.add_argument("--check", action="store_true", help="send a notification if one is due")
    action.add_argument("--status", action="store_true", help="print freshness and projected resets")
    action.add_argument("--test", action="store_true", help="send a preview message")
    action.add_argument("--validate-config", action="store_true", help="validate config and exit")
    return parser


def main(argv: Optional[list] = None) -> int:
    """Dispatch exactly one VPS action."""
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
    raise AssertionError("argparse accepted no VPS action")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
