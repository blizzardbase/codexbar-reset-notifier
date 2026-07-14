"""Notification text, timezone rendering, and Telegram payload construction.

No test in this file performs network access.
"""
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import common

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
PROVIDERS = ("claude", "codex")


def stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


def records(claude_session, codex_session, claude_weekly=None, codex_weekly=None):
    def entry(session, weekly):
        usage = {}
        if session is not None:
            usage["primary"] = {"resetsAt": stamp(session), "windowMinutes": 300}
        if weekly is not None:
            usage["secondary"] = {"resetsAt": stamp(weekly), "windowMinutes": 7 * 24 * 60}
        return {"usage": usage}

    return {
        "claude": entry(claude_session, claude_weekly),
        "codex": entry(codex_session, codex_weekly),
    }


class WeeklyOnlyCodexTests(unittest.TestCase):
    def test_codex_countdown_is_not_sent_even_if_an_old_record_has_one(self):
        payload = records(NOW - timedelta(seconds=30), NOW + timedelta(minutes=11))
        message = common.build_reset_message(payload, NOW, "Asia/Dubai", PROVIDERS)
        self.assertIn("Claude session reset has happened.", message)
        self.assertNotIn("Codex will reset", message)
        self.assertNotIn("minute", message)

    def test_codex_can_report_weekly_only(self):
        payload = records(
            NOW,
            None,
            claude_weekly=NOW + timedelta(days=2),
            codex_weekly=NOW + timedelta(days=6),
        )
        message = common.build_reset_message(payload, NOW, "UTC", PROVIDERS)
        self.assertIn("Claude weekly reset:", message)
        self.assertIn("Codex weekly reset:", message)
        self.assertNotIn("Codex session", message)


class WeeklyLineTests(unittest.TestCase):
    def test_full_production_template(self):
        payload = records(
            NOW - timedelta(seconds=30),
            NOW + timedelta(minutes=11),
            claude_weekly=NOW + timedelta(days=2, hours=11),
            codex_weekly=NOW + timedelta(days=6, hours=19),
        )
        message = common.build_reset_message(payload, NOW, "Asia/Dubai", PROVIDERS)
        lines = message.split("\n")
        self.assertEqual(
            lines[0],
            "Claude session reset has happened.",
        )
        self.assertEqual(lines[1], "")
        self.assertTrue(lines[2].startswith("Claude weekly reset: "))
        self.assertTrue(lines[2].endswith("Dubai time (2 days 11 hours)"))
        self.assertTrue(lines[3].startswith("Codex weekly reset: "))
        self.assertTrue(lines[3].endswith("Dubai time (6 days 19 hours)"))
        self.assertEqual(len(lines), 4)

    def test_no_usage_percentages_leak_into_the_message(self):
        payload = records(NOW, NOW + timedelta(minutes=5), NOW + timedelta(days=1))
        payload["claude"]["usage"]["primary"]["usedPercent"] = 73
        message = common.build_reset_message(payload, NOW, "UTC", PROVIDERS)
        self.assertNotIn("%", message)
        self.assertNotIn("73", message)

    def test_weekly_line_omitted_when_the_provider_reports_nothing(self):
        payload = records(NOW, NOW + timedelta(minutes=5), claude_weekly=NOW + timedelta(days=1))
        message = common.build_reset_message(payload, NOW, "UTC", PROVIDERS)
        self.assertIn("Claude weekly reset:", message)
        self.assertNotIn("Codex weekly reset:", message)


class FormattingTests(unittest.TestCase):
    def test_days_and_hours(self):
        hour = 3600
        self.assertEqual(common.format_days_hours(2 * 24 * hour + 11 * hour), "2 days 11 hours")
        self.assertEqual(common.format_days_hours(24 * hour + 1 * hour), "1 day 1 hour")
        self.assertEqual(common.format_days_hours(48 * hour), "2 days")
        self.assertEqual(common.format_days_hours(5 * hour), "5 hours")
        self.assertEqual(common.format_days_hours(1 * hour), "1 hour")
        self.assertEqual(common.format_days_hours(59 * 60), "less than an hour")
        self.assertEqual(common.format_days_hours(-500), "less than an hour")

    def test_clock_formatting_across_the_meridiem(self):
        def clock(hour, minute):
            return common.format_clock(datetime(2026, 7, 12, hour, minute, tzinfo=timezone.utc))

        self.assertEqual(clock(19, 59), "7:59 PM, Sun")
        self.assertEqual(clock(0, 5), "12:05 AM, Sun")
        self.assertEqual(clock(12, 0), "12:00 PM, Sun")
        self.assertEqual(clock(4, 0), "4:00 AM, Sun")

    def test_timezone_label(self):
        self.assertEqual(common.timezone_label("Asia/Dubai"), "Dubai")
        self.assertEqual(common.timezone_label("America/New_York"), "New York")
        self.assertEqual(common.timezone_label("UTC"), "UTC")

    def test_provider_label(self):
        self.assertEqual(common.provider_label("claude"), "Claude")
        self.assertEqual(common.provider_label("codex"), "Codex")
        self.assertEqual(common.provider_label("some-provider"), "Some Provider")


class TimezoneTests(unittest.TestCase):
    def test_same_instant_renders_differently_per_timezone(self):
        moment = datetime(2026, 7, 12, 19, 59, tzinfo=timezone.utc)
        self.assertEqual(common.format_clock(moment.astimezone(ZoneInfo("UTC"))), "7:59 PM, Sun")
        self.assertEqual(
            common.format_clock(moment.astimezone(ZoneInfo("Asia/Dubai"))), "11:59 PM, Sun"
        )

    def test_dubai_offset_can_roll_the_weekday_forward(self):
        moment = datetime(2026, 7, 12, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(
            common.format_clock(moment.astimezone(ZoneInfo("Asia/Dubai"))), "2:00 AM, Mon"
        )

    def test_daylight_saving_shifts_the_rendered_hour(self):
        new_york = ZoneInfo("America/New_York")
        winter = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc).astimezone(new_york)
        summer = datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc).astimezone(new_york)
        # Same UTC clock time, but EST is UTC-5 and EDT is UTC-4.
        self.assertEqual(common.format_clock(winter), "12:00 PM, Thu")
        self.assertEqual(common.format_clock(summer), "1:00 PM, Wed")
        self.assertEqual(winter.utcoffset(), timedelta(hours=-5))
        self.assertEqual(summer.utcoffset(), timedelta(hours=-4))

    def test_weekly_line_uses_the_configured_timezone(self):
        payload = records(NOW, NOW + timedelta(minutes=5), claude_weekly=NOW + timedelta(days=2))
        dubai = common.build_reset_message(payload, NOW, "Asia/Dubai", PROVIDERS)
        utc = common.build_reset_message(payload, NOW, "UTC", PROVIDERS)
        self.assertIn("Dubai time", dubai)
        self.assertIn("UTC time", utc)
        self.assertNotEqual(dubai, utc)


class TelegramPayloadTests(unittest.TestCase):
    """Payload construction only. build_telegram_request performs no network access."""

    def test_url_and_body_are_built_correctly(self):
        url, body = common.build_telegram_request("123:ABC", "-100999", "hello\nworld")
        self.assertEqual(url, "https://api.telegram.org/bot123:ABC/sendMessage")
        parsed = urllib.parse.parse_qs(body.decode("utf-8"))
        self.assertEqual(parsed["chat_id"], ["-100999"])
        self.assertEqual(parsed["text"], ["hello\nworld"])

    def test_unicode_message_is_encoded_as_utf8(self):
        _, body = common.build_telegram_request("t", "1", "reset ✅")
        self.assertIsInstance(body, bytes)
        self.assertEqual(urllib.parse.parse_qs(body.decode("utf-8"))["text"], ["reset ✅"])

    def test_missing_credentials_are_rejected_before_any_request(self):
        with self.assertRaises(common.ConfigError):
            common.build_telegram_request("", "1", "hi")
        with self.assertRaises(common.ConfigError):
            common.build_telegram_request("token", "", "hi")


if __name__ == "__main__":
    unittest.main()
