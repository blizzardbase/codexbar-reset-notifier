#!/usr/bin/env python3
"""Discover Telegram chat ids for your bot and store them in .env.

Send ``/start`` to your bot from the chat you want notifications in, then run
this script. The bot token is read from .env and is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import common
from common import ConfigError

PLACEHOLDER_TOKEN = "replace_with_botfather_token"
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_env_pairs(path: Path) -> dict:
    """Return parsed key/value pairs without exposing values in output."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def fetch_updates(token: str) -> list:
    """Fetch Telegram updates for the configured bot."""
    url = f"{common.TELEGRAM_API_BASE}/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read())
    except json.JSONDecodeError:
        raise RuntimeError("Telegram API returned an invalid JSON response") from None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in .env.") from None
        raise RuntimeError(f"Telegram API returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the Telegram API: {exc.reason}") from None
    if not payload.get("ok"):
        raise RuntimeError("Telegram rejected the getUpdates request")
    return payload.get("result", [])


def select_chat(updates: list, want_group: bool) -> dict:
    """Return the most recent private or group chat from Telegram updates."""
    wanted = {"group", "supergroup"} if want_group else {"private"}
    chats = [
        entry["message"]["chat"]
        for entry in updates
        if isinstance(entry.get("message"), dict)
        and entry["message"].get("chat", {}).get("type") in wanted
    ]
    if not chats:
        destination = "group" if want_group else "private chat"
        raise RuntimeError(
            f"No {destination} message found. Send /start to the bot there, then run this again."
        )
    return chats[-1]


def write_env(path: Path, values: dict) -> None:
    """Update matching keys while preserving comments, blanks, and unrelated lines."""
    original = path.read_text().splitlines(keepends=True) if path.exists() else []
    seen = set()
    rendered = []
    for raw_line in original:
        body = raw_line.rstrip("\r\n")
        newline = raw_line[len(body) :]
        candidate = body.strip()
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if not candidate.startswith("#") and _ENV_KEY.fullmatch(key or "") and key in values:
            rendered.append(f"{key}={values[key]}{newline}")
            seen.add(key)
        else:
            rendered.append(raw_line)

    missing = [(key, value) for key, value in values.items() if key not in seen]
    if missing and rendered and not rendered[-1].endswith(("\n", "\r")):
        rendered[-1] += "\n"
    rendered.extend(f"{key}={value}\n" for key, value in missing)
    content = "".join(rendered)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            os.fchmod(temp_file.fileno(), 0o600)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def main(argv: Optional[list] = None) -> int:
    """Discover a Telegram chat and persist its id in .env."""
    parser = argparse.ArgumentParser(description="Save Telegram destination ids to .env")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--group", action="store_true", help="use the latest group chat instead of a private chat"
    )
    destination.add_argument(
        "--both", action="store_true", help="save the latest private chat and group chat"
    )
    args = parser.parse_args(argv)

    env_path = common.ENV_FILE
    values = read_env_pairs(env_path)
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == PLACEHOLDER_TOKEN:
        raise ConfigError("Add your BotFather token as TELEGRAM_BOT_TOKEN in .env first.")

    updates = fetch_updates(token)
    if args.both:
        chats = [select_chat(updates, False), select_chat(updates, True)]
    else:
        chats = [select_chat(updates, args.group)]
    chat_ids = []
    for chat in chats:
        chat_id = str(chat["id"])
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
    write_env(
        env_path,
        {
            "TELEGRAM_CHAT_ID": chat_ids[0],
            "TELEGRAM_CHAT_IDS": ",".join(chat_ids),
        },
    )

    destinations = ", ".join(
        f"{chat.get('title') or chat.get('username') or 'private chat'} ({chat.get('type')})"
        for chat in chats
    )
    print(f"Telegram destination(s) saved: {destinations}")
    print("The chat ids were written to .env. Nothing sensitive was printed to the terminal.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
