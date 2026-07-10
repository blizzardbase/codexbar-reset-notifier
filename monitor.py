#!/usr/bin/env python3
"""Mac-side agent for the CodexBar reset notifier.

Reads the authoritative Claude and Codex reset data from the CodexBar CLI. In
``vps`` mode it syncs the reset anchors to the VPS over SSH and sends nothing
itself, so there is exactly one notifier. In ``local`` mode it evaluates the
cycle and sends the Telegram message directly, which only works while the Mac
is awake.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import common
from common import ConfigError

STATE_FILE = common.DATA_DIR / "state.json"

# Searched in order when config.codexbar_path is null.
CODEXBAR_FALLBACKS = ("/opt/homebrew/bin/codexbar", "/usr/local/bin/codexbar")


def resolve_codexbar(config: dict) -> str:
    configured = config.get("codexbar_path")
    if configured:
        if not Path(configured).is_file():
            raise ConfigError(f"codexbar_path does not point to a file: {configured}")
        return configured
    discovered = shutil.which("codexbar")
    if discovered:
        return discovered
    for candidate in CODEXBAR_FALLBACKS:
        if Path(candidate).is_file():
            return candidate
    raise ConfigError(
        "The codexbar CLI was not found. Install CodexBar, or set codexbar_path in config.json."
    )


def fetch_provider(executable: str, provider: str) -> list:
    """Return every CodexBar record for a provider, one per signed-in account."""
    try:
        result = subprocess.run(
            [executable, "--provider", provider, "--format", "json", "--json-only"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"codexbar failed for provider {provider} (exit {exc.returncode})") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"codexbar timed out for provider {provider}") from None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"codexbar returned invalid JSON for provider {provider}: {exc}") from None
    entries = payload if isinstance(payload, list) else [payload]
    return [entry for entry in entries if isinstance(entry, dict)]


def slim_record(entry: dict) -> dict:
    """Keep only reset metadata.

    Usage percentages, account emails, and every other field stay on the Mac.
    The VPS never needs them and they would be stale the moment they arrive.
    """
    usage = entry.get("usage") or {}
    windows = {}
    for slot in ("primary", "secondary"):
        window = usage.get(slot) or {}
        resets_at = window.get("resetsAt")
        if not resets_at:
            continue
        slim = {"resetsAt": resets_at}
        minutes = common.window_minutes(window)
        if minutes is not None:
            slim["windowMinutes"] = minutes
        windows[slot] = slim
    return {"usage": windows}


def collect_records(config: dict) -> dict:
    """Read one record per provider, for the configured account."""
    executable = resolve_codexbar(config)
    accounts = config.get("accounts") or {}
    records = {}
    for provider in config["providers"]:
        entries = fetch_provider(executable, provider)
        entry = common.select_account_record(entries, provider, accounts.get(provider))
        records[provider] = slim_record(entry)
    return records


def sync_to_vps(config: dict, records: dict) -> None:
    """Push reset anchors to the VPS over SSH. Only metadata leaves the Mac."""
    remote_dir = config["vps_remote_dir"].rstrip("/")
    remote_script = f"{remote_dir}/vps_notifier.py"
    command = f"python3 {common.shell_quote(remote_script)} --ingest"
    payload = json.dumps(
        {
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "records": records,
        }
    )
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            common.ssh_target(config),
            command,
        ],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"ssh exit {result.returncode}"
        raise RuntimeError(f"VPS sync failed: {reason}")


def notify_locally(config: dict, records: dict, now: datetime) -> str:
    """Evaluate the cycle and send at most one Telegram message."""
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
    return decision.action


def run_check(config: dict) -> int:
    records = collect_records(config)
    now = datetime.now(timezone.utc)
    if config["notification_mode"] == "vps":
        sync_to_vps(config, records)
        return 0
    action = notify_locally(config, records, now)
    if action == "unavailable":
        print("WARNING: no projectable session data for the trigger provider", file=sys.stderr)
    return 0


def run_status(config: dict) -> int:
    records = collect_records(config)
    now = datetime.now(timezone.utc)
    print(f"Mode: {config['notification_mode']}")
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
    common.notify(
        "CodexBar reset notifier test. "
        "Delivery to this private chat is working; no AI tokens were used."
    )
    print("Test message sent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CodexBar reset notifier (Mac side)")
    parser.add_argument("--config", type=Path, default=None, help="path to config.json")
    parser.add_argument("--validate-config", action="store_true", help="validate config and exit")
    parser.add_argument("--status", action="store_true", help="print projected resets and exit")
    parser.add_argument("--test", action="store_true", help="send a Telegram test message")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    common.load_env()
    config = common.load_config(args.config)

    if args.validate_config:
        print(f"Configuration is valid ({config['notification_mode']} mode).")
        return 0
    if args.test:
        return run_test(config)
    if args.status:
        return run_status(config)
    return run_check(config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
