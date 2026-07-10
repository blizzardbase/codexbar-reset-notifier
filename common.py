#!/usr/bin/env python3
"""Shared logic for the CodexBar reset notifier.

Deterministic helpers only: configuration loading and validation, reset-cycle
projection, message formatting, atomic state storage, and Telegram payload
construction. No AI, LLM, or paid API calls are made anywhere in this project.

Both ``monitor.py`` (Mac) and ``vps_notifier.py`` (VPS) import this module, so
it must stay dependency-free beyond the Python standard library.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config.json"
ENV_FILE = ROOT / ".env"

TELEGRAM_API_BASE = "https://api.telegram.org"

# How long after a projected reset a notification may still be sent. The VPS
# checks once per minute, so two minutes tolerates one missed run. A reset older
# than this is recorded as seen but never announced late.
RESET_GRACE_SECONDS = 120

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex"}

NOTIFICATION_MODES = ("local", "vps")

# Fields every config.json must define, mapped to their expected Python type.
_REQUIRED_CONFIG = {
    "timezone": str,
    "providers": list,
    "accounts": dict,
    "codexbar_path": (str, type(None)),
    "notification_mode": str,
    "vps_host": str,
    "vps_user": str,
    "vps_remote_dir": str,
    "mac_sync_interval_seconds": int,
    "vps_check_interval_seconds": int,
    "stale_data_minutes": int,
}

_POSITIVE_INTS = (
    "mac_sync_interval_seconds",
    "vps_check_interval_seconds",
    "stale_data_minutes",
)

# cron's `*/N` steps restart each hour, so an N that does not divide 60 leaves an
# irregular gap across every hour boundary (*/7 fires at :56 then :00). Only
# divisors of 60 are accepted, plus 60 itself for an hourly check.
CRON_ALLOWED_MINUTES = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60)

_FRACTION = re.compile(r"\.(\d+)")

# Result of evaluating whether a reset notification is due.
#   send        -> deliver `message`, then record `key`
#   seed        -> first ever run; record `key` without notifying
#   duplicate   -> this reset was already announced
#   expired     -> reset is older than the grace window; record without notifying
#   unavailable -> provider data is missing or unprojectable
ResetDecision = namedtuple("ResetDecision", ("action", "key", "message"))


class ConfigError(RuntimeError):
    """Raised when configuration or credentials are missing or invalid."""


# ---------------------------------------------------------------------------
# Environment and configuration
# ---------------------------------------------------------------------------


def load_env(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Existing environment variables always win, so secrets can be injected by a
    process manager instead of a file. Values are never printed.
    """
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def telegram_credentials() -> Tuple[str, str]:
    """Return (bot token, chat id) from the environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    missing = [
        name
        for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
        if not value
    ]
    if missing:
        raise ConfigError(
            f"{' and '.join(missing)} not set. Add them to .env (see .env.example)."
        )
    return token, chat_id


def load_config(path: Optional[Path] = None) -> dict:
    """Read and validate config.json (or an explicit path)."""
    config_path = Path(path) if path is not None else CONFIG_FILE
    if not config_path.exists():
        raise ConfigError(
            f"{config_path.name} not found. Copy config.example.json to config.json and edit it."
        )
    try:
        raw = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path.name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path.name} must contain a JSON object.")
    return validate_config(raw)


def validate_config(config: dict) -> dict:
    """Validate a config mapping and return it unchanged."""
    for key, expected in _REQUIRED_CONFIG.items():
        if key not in config:
            raise ConfigError(f"Missing required config key: {key}")
        value = config[key]
        # bool is a subclass of int; reject it explicitly for interval fields.
        if expected is int and isinstance(value, bool):
            raise ConfigError(f"Config key {key} must be an integer, not a boolean.")
        if not isinstance(value, expected):
            names = expected if isinstance(expected, tuple) else (expected,)
            allowed = " or ".join(t.__name__ for t in names)
            raise ConfigError(f"Config key {key} must be {allowed}.")

    for key in _POSITIVE_INTS:
        if config[key] <= 0:
            raise ConfigError(f"Config key {key} must be greater than zero.")

    providers = config["providers"]
    if not providers:
        raise ConfigError("Config key providers must list at least one provider.")
    if not all(isinstance(p, str) and p.strip() for p in providers):
        raise ConfigError("Config key providers must contain non-empty strings.")
    if len(set(providers)) != len(providers):
        raise ConfigError("Config key providers must not contain duplicates.")

    accounts = config["accounts"]
    for provider, account in accounts.items():
        if provider not in providers:
            raise ConfigError(
                f"Config key accounts names '{provider}', which is not in providers."
            )
        if not isinstance(account, str) or not account.strip():
            raise ConfigError(f"Config key accounts.{provider} must be a non-empty string.")

    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Config key timezone is not a valid IANA name: {exc}") from exc

    mode = config["notification_mode"]
    if mode not in NOTIFICATION_MODES:
        raise ConfigError(
            f"Config key notification_mode must be one of {', '.join(NOTIFICATION_MODES)}."
        )

    if mode == "vps":
        if not config["vps_host"].strip():
            raise ConfigError("Config key vps_host is required when notification_mode is vps.")
        remote_dir = config["vps_remote_dir"].strip()
        if not remote_dir:
            raise ConfigError(
                "Config key vps_remote_dir is required when notification_mode is vps."
            )
        if not remote_dir.startswith("/"):
            raise ConfigError("Config key vps_remote_dir must be an absolute path.")
        if "%" in remote_dir:
            raise ConfigError("Config key vps_remote_dir must not contain '%'; cron reserves it.")
        # Surfaces a bad interval at setup time rather than at cron-install time.
        cron_schedule(config)

    codexbar_path = config["codexbar_path"]
    if isinstance(codexbar_path, str) and not codexbar_path.strip():
        raise ConfigError("Config key codexbar_path must be a path or null, not an empty string.")

    return config


def config_timezone(config: dict) -> ZoneInfo:
    return ZoneInfo(config["timezone"])


def cron_schedule(config: dict) -> str:
    """Turn vps_check_interval_seconds into a cron schedule that fires evenly.

    A `*/N` step restarts at the top of every hour, so an N that does not divide
    60 produces an irregular gap across the hour boundary. Rather than silently
    accept that, only divisors of 60 are allowed.
    """
    seconds = config["vps_check_interval_seconds"]
    if seconds % 60:
        raise ConfigError(
            "Config key vps_check_interval_seconds must be a whole number of minutes."
        )
    minutes = seconds // 60
    if minutes not in CRON_ALLOWED_MINUTES:
        allowed = ", ".join(str(m) for m in CRON_ALLOWED_MINUTES)
        raise ConfigError(
            f"Config key vps_check_interval_seconds must be one of these minute counts "
            f"(so cron fires evenly across the hour): {allowed}. "
            f"Got {minutes} minutes."
        )
    if minutes == 1:
        return "* * * * *"
    if minutes == 60:
        return "0 * * * *"
    return f"*/{minutes} * * * *"


def select_account_record(entries: Sequence[dict], provider: str, wanted: Optional[str]) -> dict:
    """Choose the CodexBar record for the configured account.

    CodexBar may report several signed-in accounts for one provider. Silently
    taking the first would monitor an arbitrary account, so an explicit choice
    is required whenever there is more than one.
    """
    if not entries:
        raise ConfigError(f"CodexBar returned no data for provider {provider}.")

    available = [account_identifier(entry) for entry in entries]

    if wanted:
        for entry, identifier in zip(entries, available):
            if identifier == wanted:
                return entry
        known = ", ".join(i for i in available if i) or "none reported"
        raise ConfigError(
            f"Config key accounts.{provider} is '{wanted}', but CodexBar reports: {known}."
        )

    if len(entries) == 1:
        return entries[0]

    known = ", ".join(i for i in available if i) or "unnamed accounts"
    raise ConfigError(
        f"CodexBar reports {len(entries)} accounts for provider {provider} ({known}). "
        f'Choose one by setting "accounts": {{"{provider}": "<account>"}} in config.json.'
    )


def account_identifier(entry: dict) -> Optional[str]:
    """The account label CodexBar reports, if any. Never sent to the VPS."""
    if not isinstance(entry, dict):
        return None
    usage = entry.get("usage")
    if isinstance(usage, dict):
        email = usage.get("accountEmail")
        if isinstance(email, str) and email.strip():
            return email.strip()
    account = entry.get("account")
    return account.strip() if isinstance(account, str) and account.strip() else None


def ssh_target(config: dict) -> str:
    """Build the ssh destination, honouring an SSH config alias when no user is set."""
    host = config["vps_host"].strip()
    user = config["vps_user"].strip()
    return f"{user}@{host}" if user else host


def shell_quote(value: str) -> str:
    """Single-quote a string for safe use inside a remote shell command."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


# ---------------------------------------------------------------------------
# JSON storage
# ---------------------------------------------------------------------------


def read_json(path: Path, fallback: Any) -> Any:
    """Read JSON, returning `fallback` when the file is absent or corrupt."""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        return fallback


def atomic_json_write(path: Path, payload: Any) -> None:
    """Write JSON via a temporary file and an atomic rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


# ---------------------------------------------------------------------------
# Time parsing, projection, and formatting
# ---------------------------------------------------------------------------


def parse_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    match = _FRACTION.search(text)
    if match and len(match.group(1)) > 6:
        # datetime.fromisoformat accepts at most microsecond precision.
        text = text[: match.start(1) + 6] + text[match.end(1) :]
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def window_minutes(window: Any) -> Optional[int]:
    """Return the provider-reported window length in minutes, or None."""
    if not isinstance(window, dict):
        return None
    value = window.get("windowMinutes")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    minutes = int(value)
    return minutes if minutes > 0 else None


def cycle_bounds(window: Any, now: datetime) -> Optional[Tuple[Optional[datetime], datetime]]:
    """Project the (last, next) reset for a repeating window.

    The provider's own ``windowMinutes`` drives the repetition; no interval is
    ever assumed. Returns None when the window cannot be projected, and a
    ``last`` of None when the anchor lies ahead and no interval was reported.
    """
    if not isinstance(window, dict):
        return None
    raw_anchor = window.get("resetsAt")
    if not raw_anchor:
        return None
    try:
        anchor = parse_timestamp(raw_anchor)
    except ValueError:
        return None

    minutes = window_minutes(window)
    if now < anchor:
        previous = anchor - timedelta(minutes=minutes) if minutes else None
        return previous, anchor

    if minutes is None:
        # The anchor has passed and the provider reported no interval, so the
        # next reset is unknowable. Refuse to guess.
        return None

    span = timedelta(minutes=minutes)
    completed = int((now - anchor) // span)
    last = anchor + completed * span
    return last, last + span


def next_reset(window: Any, now: datetime) -> Optional[datetime]:
    bounds = cycle_bounds(window, now)
    return bounds[1] if bounds else None


def last_reset(window: Any, now: datetime) -> Optional[datetime]:
    bounds = cycle_bounds(window, now)
    return bounds[0] if bounds else None


def get_window(records: dict, provider: str, slot: str) -> dict:
    """Fetch a provider's ``primary`` (session) or ``secondary`` (weekly) window."""
    entry = records.get(provider) if isinstance(records, dict) else None
    usage = (entry or {}).get("usage") if isinstance(entry, dict) else None
    window = (usage or {}).get(slot) if isinstance(usage, dict) else None
    return window if isinstance(window, dict) else {}


def _plural(word: str, count: int) -> str:
    return word if count == 1 else word + "s"


def format_clock(moment: datetime) -> str:
    """Format an already-localised datetime as ``7:59 PM, Sun``."""
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {meridiem}, {DAY_NAMES[moment.weekday()]}"


def format_days_hours(seconds: float) -> str:
    """Format a duration as ``2 days 11 hours``, degrading gracefully."""
    total_hours = max(0, int(seconds // 3600))
    days, hours = divmod(total_hours, 24)
    if days and hours:
        return f"{days} {_plural('day', days)} {hours} {_plural('hour', hours)}"
    if days:
        return f"{days} {_plural('day', days)}"
    if hours:
        return f"{hours} {_plural('hour', hours)}"
    return "less than an hour"


def timezone_label(timezone_name: str) -> str:
    """Turn ``Asia/Dubai`` into ``Dubai`` and ``America/New_York`` into ``New York``."""
    return timezone_name.rsplit("/", 1)[-1].replace("_", " ")


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider.replace("-", " ").replace("_", " ").title())


def weekly_lines(records: dict, now: datetime, timezone_name: str, providers: Sequence[str]) -> list:
    """One ``<Provider> weekly reset: ...`` line per provider with usable data."""
    tzinfo = ZoneInfo(timezone_name)
    label = timezone_label(timezone_name)
    lines = []
    for provider in providers:
        reset_at = next_reset(get_window(records, provider, "secondary"), now)
        if reset_at is None:
            continue
        local = reset_at.astimezone(tzinfo)
        countdown = format_days_hours((reset_at - now).total_seconds())
        lines.append(
            f"{provider_label(provider)} weekly reset: {format_clock(local)} {label} time ({countdown})"
        )
    return lines


def build_reset_message(
    records: dict, now: datetime, timezone_name: str, providers: Sequence[str]
) -> str:
    """Build the single production notification sent at each trigger reset.

    The first entry of ``providers`` is the trigger (Claude). The second, when
    present, is the companion whose countdown is calculated dynamically.
    """
    if not providers:
        raise ValueError("at least one provider is required")

    trigger = providers[0]
    sentences = [f"{provider_label(trigger)} session reset has happened."]

    if len(providers) > 1:
        companion = providers[1]
        companion_reset = next_reset(get_window(records, companion, "primary"), now)
        if companion_reset is not None:
            minutes = max(1, round((companion_reset - now).total_seconds() / 60))
            sentences.append(
                f"{provider_label(companion)} will reset in about {minutes} {_plural('minute', minutes)}."
            )

    headline = " ".join(sentences)
    weekly = weekly_lines(records, now, timezone_name, providers)
    return headline + "\n\n" + "\n".join(weekly) if weekly else headline


def evaluate_reset(
    records: dict,
    now: datetime,
    state: dict,
    timezone_name: str,
    providers: Sequence[str],
    grace_seconds: int = RESET_GRACE_SECONDS,
) -> ResetDecision:
    """Decide whether a notification is due, without sending or mutating state."""
    if not providers:
        return ResetDecision("unavailable", None, None)

    bounds = cycle_bounds(get_window(records, providers[0], "primary"), now)
    if bounds is None or bounds[0] is None:
        return ResetDecision("unavailable", None, None)

    reset_at = bounds[0]
    key = reset_at.isoformat()
    previous = (state.get("resetsSent") or {}).get("trigger")

    if previous == key:
        return ResetDecision("duplicate", key, None)
    if previous is None:
        # First run: adopt the current cycle silently rather than announcing a
        # reset that may have happened hours before installation.
        return ResetDecision("seed", key, None)
    if (now - reset_at).total_seconds() > grace_seconds:
        return ResetDecision("expired", key, None)

    return ResetDecision("send", key, build_reset_message(records, now, timezone_name, providers))


def mark_sent(state: dict, key: str) -> dict:
    state.setdefault("resetsSent", {})["trigger"] = key
    return state


def schedule_is_fresh(schedule: dict, now: datetime, stale_data_minutes: int) -> bool:
    """True when the Mac synced within the stale-data threshold.

    A stale schedule is still projected from; freshness only affects warnings
    and status output, because offline continuation is the point of the VPS.
    """
    updated_at = (schedule or {}).get("updatedAt")
    if not updated_at:
        return False
    try:
        synced = parse_timestamp(updated_at)
    except ValueError:
        return False
    return (now - synced).total_seconds() / 60 <= stale_data_minutes


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def build_telegram_request(token: str, chat_id: str, message: str) -> Tuple[str, bytes]:
    """Build the (url, body) pair for sendMessage. Performs no network access."""
    if not token or not chat_id:
        raise ConfigError("Telegram token and chat id are required.")
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    return url, body


def send_telegram(token: str, chat_id: str, message: str) -> None:
    """Deliver a Telegram message. Never echoes the token in errors."""
    url, body = build_telegram_request(token, chat_id, message)
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram API returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the Telegram API: {exc.reason}") from None
    if not payload.get("ok"):
        raise RuntimeError("Telegram rejected the notification")


def notify(message: str) -> None:
    token, chat_id = telegram_credentials()
    send_telegram(token, chat_id, message)


# ---------------------------------------------------------------------------
# Shell helper: `python3 common.py <key>` keeps config parsing in one place
# ---------------------------------------------------------------------------

_SHELL_KEYS = (
    "ssh_target",
    "cron_schedule",
    "vps_remote_dir",
    "notification_mode",
    "mac_sync_interval_seconds",
    "vps_check_interval_seconds",
    "timezone",
)

_SHELL_COMPUTED = {"ssh_target": ssh_target, "cron_schedule": cron_schedule}


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1 or argv[0] not in _SHELL_KEYS:
        print(f"usage: python3 common.py [{'|'.join(_SHELL_KEYS)}]", file=sys.stderr)
        return 2
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    key = argv[0]
    computed = _SHELL_COMPUTED.get(key)
    print(computed(config) if computed else config[key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
