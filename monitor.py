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


def build_codexbar_command(
    executable: str,
    provider: str,
    account: Optional[str] = None,
    all_accounts: bool = False,
) -> list:
    """Build the exact CodexBar argument list.

    `--account` and `--all-accounts` address CodexBar's *token accounts*, which
    are the accounts declared in its config file. A provider signed in through
    OAuth or cookies exposes exactly one account and rejects both flags with
    "No token accounts configured". So neither flag is ever passed unless the
    user explicitly asked for a specific account or for discovery — otherwise a
    plain call returns the single default account, which is what almost every
    installation has.

    CodexBar's own help spells the invocation `codexbar usage ...`; `usage` is
    the default subcommand, but naming it keeps us off an implicit default.
    """
    command = [executable, "usage", "--provider", provider, "--format", "json", "--json-only"]
    if account:
        command += ["--account", account]
    elif all_accounts:
        command.append("--all-accounts")
    return command


class CodexbarError(RuntimeError):
    """A CodexBar invocation failed. `detail` is CodexBar's own message."""

    def __init__(self, provider: str, detail: str):
        self.provider = provider
        self.detail = detail
        super().__init__(f"codexbar failed for provider {provider}: {detail}")


def _codexbar_error(stdout: str) -> Optional[str]:
    """CodexBar reports provider failures as JSON on stdout, even when it exits 1."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("error"), dict):
            message = entry["error"].get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return None


def run_codexbar(command: list, provider: str) -> list:
    """Run CodexBar and return its records, raising a useful error on failure."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise CodexbarError(provider, "timed out") from None
    except OSError as exc:
        raise CodexbarError(provider, f"could not run codexbar: {exc}") from None

    problem = _codexbar_error(result.stdout)
    if result.returncode != 0 or problem:
        detail = problem or (result.stderr or "").strip() or f"exit {result.returncode}"
        raise CodexbarError(provider, detail)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CodexbarError(provider, f"returned invalid JSON: {exc}") from None

    entries = payload if isinstance(payload, list) else [payload]
    return [entry for entry in entries if isinstance(entry, dict)]


def fetch_provider(executable: str, provider: str, account: Optional[str] = None) -> list:
    """Return the CodexBar records for a provider, honouring a configured account."""
    try:
        return run_codexbar(build_codexbar_command(executable, provider, account), provider)
    except CodexbarError as exc:
        if account and "token account" in exc.detail.lower():
            raise ConfigError(
                f"CodexBar cannot select an account for provider {provider}: {exc.detail}\n"
                f"Account selection only works for CodexBar token accounts. This provider is "
                f"signed in with a method that exposes a single account, so remove "
                f'"{provider}" from the accounts block in config.json.'
            ) from None
        raise


def list_provider_accounts(executable: str, provider: str) -> list:
    """Every account CodexBar can see for a provider. Used only by --list-accounts."""
    entries = run_codexbar(build_codexbar_command(executable, provider, all_accounts=True), provider)
    return [common.account_identifier(entry) for entry in entries]


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
        wanted = accounts.get(provider)
        entries = fetch_provider(executable, provider, wanted)
        if not entries:
            raise ConfigError(f"CodexBar returned no data for provider {provider}.")
        if len(entries) == 1:
            # Either the sole account, or the one CodexBar's --account filter chose.
            # An --account value is a label, which need not equal accountEmail, so
            # do not second-guess a filter that already returned exactly one record.
            entry = entries[0]
        else:
            # A token provider can still return several records. Never take the first.
            entry = common.select_account_record(entries, provider, wanted)
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


def run_list_accounts(config: dict) -> int:
    """Show which account names may be used in the accounts block of config.json."""
    executable = resolve_codexbar(config)
    for provider in config["providers"]:
        label = common.provider_label(provider)
        try:
            accounts = [name for name in list_provider_accounts(executable, provider) if name]
        except RuntimeError as exc:
            # Expected for OAuth/cookie providers: --all-accounts only covers
            # CodexBar token accounts. Such a provider has one account and needs
            # no entry in the accounts block.
            print(f"{label}: single account (no account selection needed)")
            print(f"  CodexBar said: {exc}")
            continue
        if not accounts:
            print(f"{label}: single unnamed account (no account selection needed)")
            continue
        for name in accounts:
            print(f"{label}: {name}")
    print('\nName one under "accounts" in config.json only if a provider lists more than one.')
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
    parser.add_argument(
        "--list-accounts", action="store_true", help="show account names usable in config.json"
    )
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
    if args.list_accounts:
        return run_list_accounts(config)
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
