"""Storage, deduplication, staleness, and the VPS check path.

Telegram delivery is patched out; no test here touches the network.
"""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import common
import vps_notifier

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
PROVIDERS = ("claude", "codex")


def stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


def sample_records(claude_session=None, codex_session=None):
    claude_session = claude_session or NOW - timedelta(seconds=30)
    codex_session = codex_session or NOW + timedelta(minutes=11)
    return {
        "claude": {
            "usage": {
                "primary": {"resetsAt": stamp(claude_session), "windowMinutes": 300},
                "secondary": {
                    "resetsAt": stamp(NOW + timedelta(days=2, hours=11)),
                    "windowMinutes": 10080,
                },
            }
        },
        "codex": {
            "usage": {
                "primary": {"resetsAt": stamp(codex_session), "windowMinutes": 300},
                "secondary": {
                    "resetsAt": stamp(NOW + timedelta(days=6, hours=19)),
                    "windowMinutes": 10080,
                },
            }
        },
    }


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_write_creates_parent_directories(self):
        target = self.root / "data" / "nested" / "state.json"
        common.atomic_json_write(target, {"a": 1})
        self.assertEqual(json.loads(target.read_text()), {"a": 1})

    def test_no_temporary_file_is_left_behind(self):
        target = self.root / "state.json"
        common.atomic_json_write(target, {"a": 1})
        self.assertEqual([p.name for p in self.root.iterdir()], ["state.json"])

    def test_existing_file_is_replaced_atomically(self):
        target = self.root / "state.json"
        common.atomic_json_write(target, {"version": 1})
        common.atomic_json_write(target, {"version": 2})
        self.assertEqual(json.loads(target.read_text()), {"version": 2})
        self.assertFalse((self.root / "state.json.tmp").exists())

    def test_dotted_filenames_keep_their_suffix(self):
        # with_suffix() would have turned vps-state.json into vps-state.tmp.
        target = self.root / "vps-state.json"
        common.atomic_json_write(target, {"ok": True})
        self.assertTrue(target.exists())

    def test_read_json_falls_back_on_missing_file(self):
        self.assertEqual(common.read_json(self.root / "absent.json", {"fallback": True}), {"fallback": True})

    def test_read_json_falls_back_on_invalid_json(self):
        broken = self.root / "broken.json"
        broken.write_text("{ this is not json")
        self.assertEqual(common.read_json(broken, {}), {})


class DeduplicationTests(unittest.TestCase):
    def test_first_ever_run_seeds_without_notifying(self):
        decision = common.evaluate_reset(sample_records(), NOW, {}, "UTC", PROVIDERS)
        self.assertEqual(decision.action, "seed")
        self.assertIsNone(decision.message)

    def test_a_new_reset_is_announced_once(self):
        state = {"resetsSent": {"trigger": "2026-07-10T07:00:00+00:00"}}
        first = common.evaluate_reset(sample_records(), NOW, state, "UTC", PROVIDERS)
        self.assertEqual(first.action, "send")
        self.assertIn("Claude session reset has happened.", first.message)

        common.mark_sent(state, first.key)
        second = common.evaluate_reset(sample_records(), NOW, state, "UTC", PROVIDERS)
        self.assertEqual(second.action, "duplicate")
        self.assertIsNone(second.message)

    def test_duplicate_persists_across_the_whole_cycle(self):
        state = {"resetsSent": {"trigger": "2026-07-10T07:00:00+00:00"}}
        decision = common.evaluate_reset(sample_records(), NOW, state, "UTC", PROVIDERS)
        common.mark_sent(state, decision.key)
        later = NOW + timedelta(hours=4)
        self.assertEqual(
            common.evaluate_reset(sample_records(), later, state, "UTC", PROVIDERS).action,
            "duplicate",
        )

    def test_a_long_missed_reset_is_recorded_but_not_announced_late(self):
        state = {"resetsSent": {"trigger": "older-key"}}
        records = sample_records(claude_session=NOW - timedelta(minutes=90))
        decision = common.evaluate_reset(records, NOW, state, "UTC", PROVIDERS)
        self.assertEqual(decision.action, "expired")
        self.assertIsNone(decision.message)

    def test_reset_within_the_grace_window_is_announced(self):
        state = {"resetsSent": {"trigger": "older-key"}}
        records = sample_records(claude_session=NOW - timedelta(seconds=110))
        self.assertEqual(common.evaluate_reset(records, NOW, state, "UTC", PROVIDERS).action, "send")

    def test_unprojectable_data_produces_no_decision(self):
        self.assertEqual(common.evaluate_reset({}, NOW, {}, "UTC", PROVIDERS).action, "unavailable")
        self.assertEqual(common.evaluate_reset(sample_records(), NOW, {}, "UTC", ()).action, "unavailable")

    def test_evaluate_reset_does_not_mutate_state(self):
        state = {"resetsSent": {"trigger": "older-key"}}
        common.evaluate_reset(sample_records(), NOW, state, "UTC", PROVIDERS)
        self.assertEqual(state, {"resetsSent": {"trigger": "older-key"}})

    def test_next_cycle_produces_a_different_key(self):
        state = {"resetsSent": {"trigger": "older-key"}}
        first = common.evaluate_reset(sample_records(), NOW, state, "UTC", PROVIDERS)
        later = NOW + timedelta(minutes=300)
        second = common.evaluate_reset(sample_records(), later, state, "UTC", PROVIDERS)
        self.assertNotEqual(first.key, second.key)


class StalenessTests(unittest.TestCase):
    def test_recent_sync_is_fresh(self):
        schedule = {"updatedAt": stamp(NOW - timedelta(minutes=4))}
        self.assertTrue(common.schedule_is_fresh(schedule, NOW, 30))

    def test_old_sync_is_stale(self):
        schedule = {"updatedAt": stamp(NOW - timedelta(hours=9))}
        self.assertFalse(common.schedule_is_fresh(schedule, NOW, 30))

    def test_threshold_boundary_is_inclusive(self):
        schedule = {"updatedAt": stamp(NOW - timedelta(minutes=30))}
        self.assertTrue(common.schedule_is_fresh(schedule, NOW, 30))

    def test_missing_or_invalid_timestamp_is_stale(self):
        self.assertFalse(common.schedule_is_fresh({}, NOW, 30))
        self.assertFalse(common.schedule_is_fresh({"updatedAt": "nonsense"}, NOW, 30))

    def test_stale_schedule_still_projects_and_notifies(self):
        # Offline continuation is the entire point of the VPS: staleness warns,
        # it never silences.
        state = {"resetsSent": {"trigger": "older-key"}}
        records = sample_records()
        decision = common.evaluate_reset(records, NOW, state, "UTC", PROVIDERS)
        self.assertEqual(decision.action, "send")


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.schedule = Path(self.tmp.name) / "schedule.json"
        patcher = mock.patch.object(vps_notifier, "SCHEDULE_FILE", self.schedule)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ingest(self, text):
        with mock.patch("sys.stdin", new=io.StringIO(text)):
            return vps_notifier.run_ingest()

    def test_valid_payload_is_stored(self):
        payload = json.dumps({"updatedAt": stamp(NOW), "records": sample_records()})
        self.assertEqual(self._ingest(payload), 0)
        stored = json.loads(self.schedule.read_text())
        self.assertIn("claude", stored["records"])

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._ingest("{ this is not json")
        self.assertFalse(self.schedule.exists())

    def test_invalid_updated_at_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._ingest(json.dumps({"updatedAt": "nonsense", "records": sample_records()}))

    def test_payload_without_records_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._ingest(json.dumps({"updatedAt": stamp(NOW)}))
        self.assertFalse(self.schedule.exists())

    def test_payload_without_updated_at_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._ingest(json.dumps({"records": sample_records()}))

    def test_payload_with_malformed_record_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._ingest(json.dumps({"updatedAt": stamp(NOW), "records": {"claude": "nope"}}))

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._ingest(json.dumps([1, 2, 3]))


class VpsCheckTests(unittest.TestCase):
    """End-to-end --check behaviour with Telegram delivery patched out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.schedule = root / "schedule.json"
        self.state = root / "vps-state.json"
        for name, value in (("SCHEDULE_FILE", self.schedule), ("STATE_FILE", self.state)):
            patcher = mock.patch.object(vps_notifier, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []
        patcher = mock.patch.object(common, "notify", side_effect=self.sent.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {
            "timezone": "Asia/Dubai",
            "providers": ["claude", "codex"],
            "stale_data_minutes": 30,
        }

    def write_schedule(self, updated_at=None):
        common.atomic_json_write(
            self.schedule,
            {"updatedAt": stamp(updated_at or datetime.now(timezone.utc)), "records": live_records()},
        )

    def test_no_schedule_sends_nothing(self):
        self.assertEqual(vps_notifier.run_check(self.config), 0)
        self.assertEqual(self.sent, [])

    def test_first_check_seeds_without_sending(self):
        self.write_schedule()
        vps_notifier.run_check(self.config)
        self.assertEqual(self.sent, [])
        self.assertTrue(self.state.exists())

    def test_second_check_after_a_reset_sends_exactly_one_message(self):
        self.write_schedule()
        common.atomic_json_write(self.state, {"resetsSent": {"trigger": "older-key"}})
        vps_notifier.run_check(self.config)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Claude session reset has happened.", self.sent[0])

        vps_notifier.run_check(self.config)
        self.assertEqual(len(self.sent), 1, "the same reset must never be announced twice")

    def test_stale_schedule_still_sends(self):
        self.write_schedule(updated_at=datetime.now(timezone.utc) - timedelta(days=2))
        common.atomic_json_write(self.state, {"resetsSent": {"trigger": "older-key"}})
        vps_notifier.run_check(self.config)
        self.assertEqual(len(self.sent), 1)

    def test_corrupt_state_file_does_not_crash_the_check(self):
        self.write_schedule()
        self.state.write_text("{ broken")
        self.assertEqual(vps_notifier.run_check(self.config), 0)


def live_records():
    """Records anchored to the real current time so run_check sees a fresh reset."""
    now = datetime.now(timezone.utc)
    return {
        "claude": {
            "usage": {
                "primary": {"resetsAt": stamp(now - timedelta(seconds=20)), "windowMinutes": 300},
                "secondary": {"resetsAt": stamp(now + timedelta(days=2)), "windowMinutes": 10080},
            }
        },
        "codex": {
            "usage": {
                "primary": {"resetsAt": stamp(now + timedelta(minutes=11)), "windowMinutes": 300},
                "secondary": {"resetsAt": stamp(now + timedelta(days=6)), "windowMinutes": 10080},
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
