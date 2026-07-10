"""Tests for the Mac-side Telegram /usage command service."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import usage_bot


CONFIG = {
    "timezone": "Asia/Dubai",
    "providers": ["claude", "codex"],
    "codexbar_path": None,
    "notification_mode": "local",
    "vps_host": "",
    "vps_user": "",
    "vps_remote_dir": "",
    "mac_sync_interval_seconds": 300,
    "vps_check_interval_seconds": 60,
    "stale_data_minutes": 30,
}


class CommandTests(unittest.TestCase):
    def test_supported_command_shapes(self):
        for text in ("/usage", "/USAGE", "/usage bot", "/usage@example_bot"):
            with self.subTest(text=text):
                self.assertTrue(usage_bot.is_usage_command(text))

    def test_unrelated_messages_are_ignored(self):
        for text in ("usage", "/status", "hello", "", None):
            with self.subTest(text=text):
                self.assertFalse(usage_bot.is_usage_command(text))


class FormattingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)

    def test_session_line_uses_hours_and_minutes(self):
        window = {
            "usedPercent": 67.4,
            "resetsAt": "2026-07-10T11:18:00Z",
            "windowMinutes": 300,
        }
        line = usage_bot.format_usage_window(
            "claude", "session", window, self.now, "Asia/Dubai"
        )
        self.assertEqual(
            line, "Claude session: 33% left; resets 3:18 PM, Fri (3h 18m)"
        )

    def test_weekly_line_uses_days_and_hours(self):
        window = {
            "usedPercent": 19,
            "resetsAt": "2026-07-12T16:00:00Z",
            "windowMinutes": 10080,
        }
        line = usage_bot.format_usage_window(
            "claude", "weekly", window, self.now, "Asia/Dubai", weekly=True
        )
        self.assertEqual(
            line, "Claude weekly: 81% left; resets 8:00 PM, Sun (2 days 8 hours)"
        )

    def test_missing_values_degrade_without_guessing(self):
        line = usage_bot.format_usage_window(
            "codex", "session", {}, self.now, "Asia/Dubai"
        )
        self.assertEqual(line, "Codex session: usage unavailable; reset time unavailable")

    def test_spring_forward_uses_new_local_offset_without_changing_countdown(self):
        now = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
        window = {
            "usedPercent": 50,
            "resetsAt": "2026-03-08T07:30:00Z",
            "windowMinutes": 300,
        }
        line = usage_bot.format_usage_window(
            "claude", "session", window, now, "America/New_York"
        )
        self.assertEqual(
            line, "Claude session: 50% left; resets 3:30 AM, Sun (1h 0m)"
        )

    def test_fall_back_uses_repeated_local_hour_with_absolute_countdown(self):
        now = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        window = {
            "usedPercent": 50,
            "resetsAt": "2026-11-01T06:30:00Z",
            "windowMinutes": 300,
        }
        line = usage_bot.format_usage_window(
            "claude", "session", window, now, "America/New_York"
        )
        self.assertEqual(
            line, "Claude session: 50% left; resets 1:30 AM, Sun (1h 0m)"
        )

    def test_full_message_reads_each_provider_once(self):
        records = {
            "claude": {
                "usage": {
                    "primary": {"usedPercent": 10, "resetsAt": "2026-07-10T12:00:00Z"},
                    "secondary": {"usedPercent": 20, "resetsAt": "2026-07-12T16:00:00Z"},
                }
            },
            "codex": {
                "usage": {
                    "primary": {"usedPercent": 30, "resetsAt": "2026-07-10T12:10:00Z"},
                    "secondary": {"usedPercent": 40, "resetsAt": "2026-07-16T00:00:00Z"},
                }
            },
        }

        def fetch(_executable, provider):
            return [records[provider]]

        with mock.patch.object(usage_bot.monitor, "resolve_codexbar", return_value="codexbar"):
            with mock.patch.object(usage_bot.monitor, "fetch_provider", side_effect=fetch) as called:
                with mock.patch.object(usage_bot, "datetime") as clock:
                    clock.now.return_value = self.now
                    message = usage_bot.build_usage_message(CONFIG)
        self.assertEqual(called.call_count, 2)
        self.assertIn("Live CodexBar usage — 12:00 PM, Fri Dubai time", message)
        self.assertIn("Claude session: 90% left", message)
        self.assertIn("Codex weekly: 60% left", message)
        self.assertNotIn("account", message.lower())


class UpdateTests(unittest.TestCase):
    @staticmethod
    def update(chat_id="123", text="/usage"):
        return {
            "update_id": 10,
            "message": {"message_id": 7, "chat": {"id": chat_id}, "text": text},
        }

    def test_authorized_command_replies_only_to_origin(self):
        with mock.patch.object(usage_bot, "build_usage_message", return_value="live usage"):
            with mock.patch.object(usage_bot, "send_message") as send:
                usage_bot.process_update(self.update(), "token", "123", CONFIG)
        send.assert_called_once_with("token", "123", "live usage", reply_to=7)

    def test_unconfigured_chat_is_ignored(self):
        with mock.patch.object(usage_bot, "send_message") as send:
            usage_bot.process_update(self.update("999"), "token", "123", CONFIG)
        send.assert_not_called()

    def test_codexbar_failure_gets_a_friendly_reply(self):
        with mock.patch.object(
            usage_bot, "build_usage_message", side_effect=RuntimeError("provider failed")
        ):
            with mock.patch.object(usage_bot, "send_message") as send:
                with mock.patch.object(usage_bot.sys, "stderr"):
                    usage_bot.process_update(self.update(), "token", "123", CONFIG)
        self.assertIn("could not read live CodexBar usage", send.call_args.args[2])


class OffsetTests(unittest.TestCase):
    def test_offset_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "offset.json"
            with mock.patch.object(usage_bot, "OFFSET_FILE", path):
                usage_bot.write_offset(42)
                self.assertEqual(json.loads(path.read_text()), {"offset": 42})
                self.assertFalse(path.with_name("offset.json.tmp").exists())

    def test_poll_advances_past_every_received_update(self):
        updates = [
            {"update_id": 11, "message": {"chat": {"id": "123"}, "text": "hello"}},
            {"update_id": 14, "message": {"chat": {"id": "123"}, "text": "/usage"}},
        ]
        with mock.patch.object(usage_bot, "get_updates", return_value=updates):
            with mock.patch.object(usage_bot, "process_update") as process:
                with mock.patch.object(usage_bot, "write_offset") as write:
                    result = usage_bot.poll_once("token", 10, "123", CONFIG, timeout=0)
        self.assertEqual(result, 15)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(write.call_args_list, [mock.call(12), mock.call(15)])


class ListenerRecoveryTests(unittest.TestCase):
    def test_unexpected_poll_error_retries_without_exiting(self):
        with mock.patch.object(
            usage_bot.common, "telegram_credentials", return_value=("token", "123")
        ):
            with mock.patch.object(usage_bot, "read_offset", return_value=10):
                with mock.patch.object(
                    usage_bot, "poll_once", side_effect=(ValueError("bad update"), KeyboardInterrupt)
                ) as poll:
                    with mock.patch.object(usage_bot.time, "sleep") as sleep:
                        with mock.patch.object(usage_bot.sys, "stderr"):
                            with self.assertRaises(KeyboardInterrupt):
                                usage_bot.run(CONFIG)
        self.assertEqual(poll.call_count, 2)
        sleep.assert_called_once_with(5)


class TelegramRequestTests(unittest.TestCase):
    def test_request_contains_no_network_in_unit_tests(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": true, "result": []}'
        response.__enter__.return_value = response
        with mock.patch.object(usage_bot.urllib.request, "urlopen", return_value=response) as opened:
            result = usage_bot.get_updates("not-a-real-token", 12, timeout=0)
        self.assertEqual(result, [])
        request = opened.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertIn(b"offset=12", request.data)

    def test_non_object_response_is_rejected(self):
        response = mock.MagicMock()
        response.read.return_value = b"[]"
        response.__enter__.return_value = response
        with mock.patch.object(usage_bot.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Telegram rejected getUpdates"):
                usage_bot.get_updates("not-a-real-token", 0, timeout=0)


if __name__ == "__main__":
    unittest.main()
