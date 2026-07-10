#!/usr/bin/env python3
"""Discover the Telegram chat id for your bot and store it in .env.

Send ``/start`` to your bot from the chat you want notifications in, then run
this script. The bot token is read from .env and is never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import common
from common import ConfigError

PLACEHOLDER_TOKEN = "replace_with_botfather_token"


def read_env_pairs(path: Path) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def fetch_updates(token: str) -> list:
    url = f"{common.TELEGRAM_API_BASE}/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read())
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
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Save your Telegram chat id to .env")
    parser.add_argument(
        "--group", action="store_true", help="use the latest group chat instead of a private chat"
    )
    args = parser.parse_args(argv)

    env_path = common.ENV_FILE
    values = read_env_pairs(env_path)
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == PLACEHOLDER_TOKEN:
        raise ConfigError("Add your BotFather token as TELEGRAM_BOT_TOKEN in .env first.")

    chat = select_chat(fetch_updates(token), args.group)
    values["TELEGRAM_CHAT_ID"] = str(chat["id"])
    write_env(env_path, values)

    label = chat.get("title") or chat.get("username") or "private chat"
    print(f"Telegram destination saved: {label} ({chat.get('type')})")
    print("The chat id was written to .env. Nothing was printed to the terminal.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
