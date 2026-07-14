#!/usr/bin/env python3
"""Serve live CodexBar usage through an authorized Telegram /usage command."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import common
import monitor
from common import ConfigError

OFFSET_FILE = common.DATA_DIR / "telegram-usage-offset.json"


def telegram_request(token: str, method: str, fields: dict, timeout: int = 30) -> dict:
    """Call one Telegram Bot API method without exposing the token in errors."""
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        f"{common.TELEGRAM_API_BASE}/bot{token}/{method}", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram API returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the Telegram API: {exc.reason}") from None
    except json.JSONDecodeError:
        raise RuntimeError("Telegram API returned invalid JSON") from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected {method}")
    return payload


def get_updates(token: str, offset: int, timeout: int = 25) -> list:
    """Long-poll Telegram for new message updates."""
    payload = telegram_request(
        token,
        "getUpdates",
        {
            "offset": str(offset),
            "limit": "100",
            "timeout": str(timeout),
            "allowed_updates": json.dumps(["message"]),
        },
        timeout=timeout + 5,
    )
    result = payload.get("result", [])
    return result if isinstance(result, list) else []


def read_offset() -> Optional[int]:
    """Return the next Telegram update id, or None on first run."""
    payload = common.read_json(OFFSET_FILE, {})
    value = payload.get("offset") if isinstance(payload, dict) else None
    return value if isinstance(value, int) and value >= 0 else None


def write_offset(offset: int) -> None:
    """Persist the next update id atomically."""
    common.atomic_json_write(OFFSET_FILE, {"offset": offset})


def prime_offset(token: str) -> int:
    """Discard pre-install messages so old commands are never replayed."""
    payload = telegram_request(
        token,
        "getUpdates",
        {
            "offset": "-1",
            "limit": "1",
            "timeout": "0",
            "allowed_updates": json.dumps(["message"]),
        },
    )
    updates = payload.get("result", [])
    if not isinstance(updates, list):
        return 0
    valid_ids = [item.get("update_id") for item in updates if isinstance(item, dict)]
    valid_ids = [value for value in valid_ids if isinstance(value, int)]
    return max(valid_ids) + 1 if valid_ids else 0


def send_message(
    token: str, chat_id: str, message: str, reply_to: Optional[int] = None
) -> None:
    """Send a response to the chat that issued the command."""
    fields = {"chat_id": chat_id, "text": message}
    if reply_to is not None:
        fields["reply_parameters"] = json.dumps({"message_id": reply_to})
    telegram_request(token, "sendMessage", fields)


def is_usage_command(text: object) -> bool:
    """Recognize /usage, /usage@botname, and /usage followed by arguments."""
    if not isinstance(text, str) or not text.strip():
        return False
    command = text.strip().split(maxsplit=1)[0].lower()
    return command.split("@", 1)[0] == "/usage"


def _left_percent(window: dict) -> Optional[int]:
    """Return the rounded percentage remaining, when CodexBar reports it."""
    used = window.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return max(0, min(100, 100 - round(float(used))))


def _session_countdown(seconds: float) -> str:
    """Format a session duration as hours and minutes."""
    total_minutes = max(0, round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def format_usage_window(
    provider: str,
    name: str,
    window: dict,
    now: datetime,
    timezone_name: str,
    weekly: bool = False,
) -> str:
    """Format one live session or weekly usage line."""
    label = f"{common.provider_label(provider)} {name}"
    left = _left_percent(window)
    prefix = f"{left}% left" if left is not None else "usage unavailable"
    reset_at = common.next_reset(window, now)
    if reset_at is None:
        return f"{label}: {prefix}; reset time unavailable"
    local = reset_at.astimezone(ZoneInfo(timezone_name))
    countdown = (
        common.format_days_hours((reset_at - now).total_seconds())
        if weekly
        else _session_countdown((reset_at - now).total_seconds())
    )
    return f"{label}: {prefix}; resets {common.format_clock(local)} ({countdown})"


def build_usage_message(config: dict) -> str:
    """Read CodexBar now and format current usage for configured providers."""
    executable = monitor.resolve_codexbar(config)
    records = {}
    for provider in config["providers"]:
        entries = monitor.fetch_provider(executable, provider)
        records[provider] = common.require_single_record(entries, provider)

    now = datetime.now(timezone.utc)
    local = now.astimezone(common.config_timezone(config))
    timezone_name = config["timezone"]
    heading = (
        f"Live CodexBar usage — {common.format_clock(local)} "
        f"{common.timezone_label(timezone_name)} time"
    )
    sections = []
    for provider in config["providers"]:
        raw_usage = records[provider].get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        lines = []
        primary = usage.get("primary")
        if isinstance(primary, dict) and primary:
            lines.append(format_usage_window(provider, "session", primary, now, timezone_name))
        secondary = usage.get("secondary")
        if isinstance(secondary, dict) and secondary:
            lines.append(
                format_usage_window(provider, "weekly", secondary, now, timezone_name, weekly=True)
            )
        sections.append("\n".join(lines) if lines else f"{common.provider_label(provider)}: usage unavailable")
    return heading + "\n\n" + "\n\n".join(sections)


def process_update(
    update: dict, token: str, allowed_chat_ids: Sequence[str], config: dict
) -> None:
    """Handle one authorized /usage message and ignore everything else."""
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict) or not is_usage_command(message.get("text")):
        return
    chat = message.get("chat")
    chat_id = str(chat.get("id")) if isinstance(chat, dict) and chat.get("id") is not None else ""
    if chat_id not in allowed_chat_ids:
        return
    try:
        response = build_usage_message(config)
    except (ConfigError, monitor.CodexbarError, RuntimeError) as exc:
        print(f"ERROR: live CodexBar usage failed: {exc}", file=sys.stderr)
        response = "I could not read live CodexBar usage from the Mac. Please try again shortly."
    message_id = message.get("message_id")
    send_message(
        token,
        chat_id,
        response,
        reply_to=message_id if isinstance(message_id, int) else None,
    )


def poll_once(
    token: str, offset: int, allowed_chat_ids: Sequence[str], config: dict, timeout: int = 25
) -> int:
    """Process one long-poll batch and return the next update offset."""
    next_offset = offset
    for update in get_updates(token, offset, timeout=timeout):
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if not isinstance(update_id, int):
            continue
        process_update(update, token, allowed_chat_ids, config)
        next_offset = max(next_offset, update_id + 1)
        write_offset(next_offset)
    return next_offset


def run(config: dict, once: bool = False) -> int:
    """Run the command listener until launchd stops it."""
    token, allowed_chat_ids = common.telegram_credentials()
    offset = read_offset()
    if offset is None:
        offset = prime_offset(token)
        write_offset(offset)
    if once:
        poll_once(token, offset, allowed_chat_ids, config, timeout=0)
        return 0
    while True:
        try:
            offset = poll_once(token, offset, allowed_chat_ids, config)
        except Exception as exc:
            print(f"ERROR: usage bot poll failed: {exc}", file=sys.stderr)
            time.sleep(5)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-service parser."""
    parser = argparse.ArgumentParser(description="Telegram /usage command service")
    parser.add_argument("--config", type=Path, default=None, help="path to config.json")
    parser.add_argument("--once", action="store_true", help="poll once without waiting")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Load configuration and run the Telegram listener."""
    args = build_parser().parse_args(argv)
    common.load_env()
    return run(common.load_config(args.config), once=args.once)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
