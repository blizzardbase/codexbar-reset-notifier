"""Reset-cycle projection: the logic that keeps the VPS correct while the Mac is off."""
import unittest
from datetime import datetime, timedelta, timezone

import common

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def window(resets_at, minutes=None):
    payload = {"resetsAt": resets_at.isoformat().replace("+00:00", "Z")}
    if minutes is not None:
        payload["windowMinutes"] = minutes
    return payload


class SessionCycleTests(unittest.TestCase):
    def test_claude_session_anchor_in_the_future(self):
        anchor = NOW + timedelta(minutes=40)
        last, upcoming = common.cycle_bounds(window(anchor, 300), NOW)
        self.assertEqual(upcoming, anchor)
        self.assertEqual(last, anchor - timedelta(minutes=300))

    def test_codex_session_anchor_in_the_past_advances_whole_cycles(self):
        anchor = NOW - timedelta(minutes=700)
        last, upcoming = common.cycle_bounds(window(anchor, 300), NOW)
        # 700 minutes is two complete 300-minute cycles plus 100 minutes.
        self.assertEqual(last, anchor + timedelta(minutes=600))
        self.assertEqual(upcoming, anchor + timedelta(minutes=900))
        self.assertGreater(upcoming, NOW)

    def test_providers_may_report_different_window_lengths(self):
        anchor = NOW - timedelta(minutes=60)
        five_hour = common.next_reset(window(anchor, 300), NOW)
        three_hour = common.next_reset(window(anchor, 180), NOW)
        self.assertEqual(five_hour, anchor + timedelta(minutes=300))
        self.assertEqual(three_hour, anchor + timedelta(minutes=180))
        self.assertNotEqual(five_hour, three_hour)

    def test_window_length_is_never_assumed(self):
        # No windowMinutes and a passed anchor means the next reset is unknowable.
        self.assertIsNone(common.cycle_bounds(window(NOW - timedelta(minutes=1)), NOW))

    def test_exact_anchor_moment_counts_as_a_reset(self):
        last, upcoming = common.cycle_bounds(window(NOW, 300), NOW)
        self.assertEqual(last, NOW)
        self.assertEqual(upcoming, NOW + timedelta(minutes=300))

    def test_mac_offline_for_three_days_still_projects_forward(self):
        anchor = NOW - timedelta(days=3)
        last, upcoming = common.cycle_bounds(window(anchor, 300), NOW)
        self.assertGreater(upcoming, NOW)
        self.assertLessEqual(upcoming - NOW, timedelta(minutes=300))
        self.assertEqual((last - anchor).total_seconds() % (300 * 60), 0)

    def test_mac_online_sync_corrects_a_drifted_anchor(self):
        stale = common.next_reset(window(NOW - timedelta(minutes=700), 300), NOW)
        corrected = common.next_reset(window(NOW + timedelta(minutes=17), 300), NOW)
        self.assertNotEqual(stale, corrected)
        self.assertEqual(corrected, NOW + timedelta(minutes=17))


class WeeklyCycleTests(unittest.TestCase):
    def test_weekly_cycle_advances_by_the_reported_interval(self):
        anchor = NOW - timedelta(days=8)
        upcoming = common.next_reset(window(anchor, 7 * 24 * 60), NOW)
        # One week past the anchor is still behind `now`, so two cycles elapse.
        self.assertEqual(upcoming, anchor + timedelta(days=14))
        self.assertGreater(upcoming, NOW)

    def test_weekly_cycle_lands_within_one_interval_of_now(self):
        anchor = NOW - timedelta(days=200)
        upcoming = common.next_reset(window(anchor, 7 * 24 * 60), NOW)
        self.assertGreater(upcoming, NOW)
        self.assertLessEqual(upcoming - NOW, timedelta(days=7))

    def test_future_weekly_anchor_is_used_verbatim(self):
        anchor = NOW + timedelta(days=2, hours=11)
        self.assertEqual(common.next_reset(window(anchor, 7 * 24 * 60), NOW), anchor)

    def test_future_weekly_anchor_works_without_a_reported_interval(self):
        anchor = NOW + timedelta(days=2)
        self.assertEqual(common.next_reset(window(anchor), NOW), anchor)


class MissingAndInvalidDataTests(unittest.TestCase):
    def test_missing_provider_data_yields_no_projection(self):
        self.assertIsNone(common.cycle_bounds({}, NOW))
        self.assertIsNone(common.cycle_bounds(None, NOW))
        self.assertIsNone(common.cycle_bounds({"windowMinutes": 300}, NOW))

    def test_invalid_timestamp_yields_no_projection(self):
        self.assertIsNone(common.cycle_bounds({"resetsAt": "not-a-date", "windowMinutes": 300}, NOW))

    def test_zero_or_boolean_window_minutes_is_ignored(self):
        self.assertIsNone(common.window_minutes({"windowMinutes": 0}))
        self.assertIsNone(common.window_minutes({"windowMinutes": -5}))
        self.assertIsNone(common.window_minutes({"windowMinutes": True}))
        self.assertEqual(common.window_minutes({"windowMinutes": 300.0}), 300)

    def test_get_window_survives_malformed_records(self):
        self.assertEqual(common.get_window({"claude": None}, "claude", "primary"), {})
        self.assertEqual(common.get_window({"claude": {"usage": []}}, "claude", "primary"), {})
        self.assertEqual(common.get_window({}, "claude", "primary"), {})


class TimestampParsingTests(unittest.TestCase):
    def test_zulu_suffix_is_understood(self):
        self.assertEqual(
            common.parse_timestamp("2026-07-10T12:00:00Z"),
            datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        )

    def test_offset_is_normalised_to_utc(self):
        self.assertEqual(
            common.parse_timestamp("2026-07-10T16:00:00+04:00"),
            datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        )

    def test_naive_timestamp_is_treated_as_utc(self):
        self.assertEqual(
            common.parse_timestamp("2026-07-10T12:00:00"),
            datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        )

    def test_nanosecond_precision_is_truncated_not_rejected(self):
        parsed = common.parse_timestamp("2026-07-10T12:00:00.123456789Z")
        self.assertEqual(parsed.microsecond, 123456)

    def test_empty_and_non_string_values_are_rejected(self):
        for bad in ("", "   ", None, 12345, []):
            with self.assertRaises(ValueError):
                common.parse_timestamp(bad)


if __name__ == "__main__":
    unittest.main()
