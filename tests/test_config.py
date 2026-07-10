"""Configuration loading and validation, including the shipped example file."""
import json
import tempfile
import unittest
from pathlib import Path

import common
from common import ConfigError

EXAMPLE = common.ROOT / "config.example.json"


def base_config(**overrides):
    config = json.loads(EXAMPLE.read_text())
    config.update(overrides)
    return config


class ExampleConfigTests(unittest.TestCase):
    def test_shipped_example_is_valid(self):
        self.assertEqual(common.validate_config(base_config())["notification_mode"], "vps")

    def test_example_carries_no_real_credentials(self):
        text = EXAMPLE.read_text().lower()
        for forbidden in ("token", "password", "secret", "api_key"):
            self.assertNotIn(forbidden, text)

    def test_example_loads_from_an_explicit_path(self):
        self.assertEqual(common.load_config(EXAMPLE)["timezone"], "UTC")


class RequiredKeyTests(unittest.TestCase):
    def test_every_key_is_required(self):
        for key in ("timezone", "providers", "notification_mode", "vps_host", "stale_data_minutes"):
            config = base_config()
            del config[key]
            with self.assertRaises(ConfigError, msg=key) as ctx:
                common.validate_config(config)
            self.assertIn(key, str(ctx.exception))

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(timezone=42))
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(providers="claude"))

    def test_codexbar_path_accepts_null_or_a_path(self):
        self.assertIsNone(common.validate_config(base_config(codexbar_path=None))["codexbar_path"])
        self.assertEqual(
            common.validate_config(base_config(codexbar_path="/opt/homebrew/bin/codexbar"))[
                "codexbar_path"
            ],
            "/opt/homebrew/bin/codexbar",
        )

    def test_codexbar_path_rejects_an_empty_string(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(codexbar_path="   "))


class IntervalTests(unittest.TestCase):
    def test_intervals_must_be_positive(self):
        for key in ("mac_sync_interval_seconds", "vps_check_interval_seconds", "stale_data_minutes"):
            with self.assertRaises(ConfigError, msg=key):
                common.validate_config(base_config(**{key: 0}))
            with self.assertRaises(ConfigError, msg=key):
                common.validate_config(base_config(**{key: -60}))

    def test_booleans_are_not_accepted_as_integers(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(vps_check_interval_seconds=True))


class ProviderTests(unittest.TestCase):
    def test_at_least_one_provider_is_required(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(providers=[]))

    def test_providers_must_be_non_empty_strings(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(providers=["claude", ""]))
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(providers=["claude", 7]))

    def test_duplicate_providers_are_rejected(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(providers=["claude", "claude"]))


class TimezoneTests(unittest.TestCase):
    def test_unknown_timezone_is_rejected(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(timezone="Mars/Olympus_Mons"))

    def test_real_timezones_are_accepted(self):
        for name in ("UTC", "Asia/Dubai", "America/New_York", "Europe/London"):
            self.assertEqual(common.validate_config(base_config(timezone=name))["timezone"], name)


class NotificationModeTests(unittest.TestCase):
    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(notification_mode="carrier-pigeon"))

    def test_vps_mode_requires_a_host(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(notification_mode="vps", vps_host="  "))

    def test_vps_mode_requires_an_absolute_remote_directory(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(notification_mode="vps", vps_remote_dir="relative/path"))
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(notification_mode="vps", vps_remote_dir=""))

    def test_local_mode_ignores_empty_vps_fields(self):
        config = common.validate_config(
            base_config(notification_mode="local", vps_host="", vps_user="", vps_remote_dir="")
        )
        self.assertEqual(config["notification_mode"], "local")


class CronScheduleTests(unittest.TestCase):
    """cron's */N restarts each hour, so N must divide 60 to fire evenly."""

    def schedule(self, minutes):
        return common.cron_schedule(base_config(vps_check_interval_seconds=minutes * 60))

    def test_divisors_of_sixty_are_accepted(self):
        self.assertEqual(self.schedule(1), "* * * * *")
        self.assertEqual(self.schedule(5), "*/5 * * * *")
        self.assertEqual(self.schedule(15), "*/15 * * * *")
        self.assertEqual(self.schedule(30), "*/30 * * * *")

    def test_sixty_minutes_becomes_an_hourly_schedule_not_a_step(self):
        # "*/60 * * * *" would never fire; cron minutes only reach 59.
        self.assertEqual(self.schedule(60), "0 * * * *")

    def test_non_divisors_are_rejected(self):
        for minutes in (7, 8, 9, 11, 13, 14, 25, 45, 59):
            with self.assertRaises(ConfigError, msg=f"{minutes} minutes"):
                self.schedule(minutes)

    def test_rejection_names_the_allowed_values(self):
        with self.assertRaises(ConfigError) as ctx:
            self.schedule(7)
        self.assertIn("vps_check_interval_seconds", str(ctx.exception))
        self.assertIn("30", str(ctx.exception))

    def test_partial_minutes_are_rejected(self):
        with self.assertRaises(ConfigError):
            common.cron_schedule(base_config(vps_check_interval_seconds=90))

    def test_every_allowed_minute_yields_an_evenly_dividing_schedule(self):
        for minutes in common.CRON_ALLOWED_MINUTES:
            self.assertEqual(60 % minutes, 0, f"{minutes} does not divide 60")
            self.schedule(minutes)

    def test_validation_rejects_a_bad_interval_in_vps_mode(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(notification_mode="vps", vps_check_interval_seconds=420))


class RemoteDirTests(unittest.TestCase):
    def test_percent_is_rejected_because_cron_reserves_it(self):
        with self.assertRaises(ConfigError):
            common.validate_config(base_config(vps_remote_dir="/home/u/50%off"))

    def test_spaces_are_allowed_and_quoted_for_the_remote_shell(self):
        config = common.validate_config(base_config(vps_remote_dir="/home/u/my notifier"))
        self.assertEqual(common.shell_quote(config["vps_remote_dir"]), "'/home/u/my notifier'")

    def test_shell_and_cron_metacharacters_are_rejected(self):
        for suffix in ("$(touch pwned)", "`id`", "'quoted'", '"quoted"', "line\nbreak", "50%off"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ConfigError):
                    common.validate_config(base_config(vps_remote_dir=f"/home/u/{suffix}"))


class SingleRecordTests(unittest.TestCase):
    """CodexBar offers no working account selection for Claude or Codex."""

    def setUp(self):
        self.work = {"usage": {"accountEmail": "work@example.com", "primary": {}}}
        self.personal = {"usage": {"accountEmail": "personal@example.com", "primary": {}}}
        self.unnamed = {"usage": {"primary": {}}}

    def test_sole_record_is_returned(self):
        self.assertIs(common.require_single_record([self.work], "claude"), self.work)

    def test_sole_unnamed_record_is_returned(self):
        self.assertIs(common.require_single_record([self.unnamed], "claude"), self.unnamed)

    def test_several_records_are_never_reduced_to_the_first(self):
        with self.assertRaises(ConfigError) as ctx:
            common.require_single_record([self.work, self.personal], "claude")
        message = str(ctx.exception)
        self.assertNotIn("work@example.com", message)
        self.assertNotIn("personal@example.com", message)
        self.assertIn("2 accounts", message)
        self.assertIn("cannot choose between them", message)

    def test_several_unnamed_records_still_refuse_to_guess(self):
        with self.assertRaises(ConfigError):
            common.require_single_record([self.unnamed, self.unnamed], "codex")

    def test_no_records_is_rejected(self):
        with self.assertRaises(ConfigError):
            common.require_single_record([], "claude")

class NoAccountsKeyTests(unittest.TestCase):
    def test_accounts_is_not_a_config_key(self):
        self.assertNotIn("accounts", json.loads(EXAMPLE.read_text()))

    def test_an_unknown_extra_key_is_ignored_rather_than_fatal(self):
        # Users upgrading from a build that had "accounts" keep working.
        config = common.validate_config(base_config(accounts={"claude": "x@y.z"}))
        self.assertEqual(config["notification_mode"], "vps")


class SshTargetTests(unittest.TestCase):
    def test_user_and_host_are_combined(self):
        self.assertEqual(common.ssh_target(base_config(vps_user="deploy", vps_host="example.net")), "deploy@example.net")

    def test_empty_user_yields_a_bare_ssh_alias(self):
        self.assertEqual(common.ssh_target(base_config(vps_user="", vps_host="my-alias")), "my-alias")


class ShellQuoteTests(unittest.TestCase):
    def test_paths_with_spaces_are_quoted(self):
        self.assertEqual(common.shell_quote("/srv/my notifier"), "'/srv/my notifier'")

    def test_embedded_single_quotes_are_escaped(self):
        self.assertEqual(common.shell_quote("it's"), "'it'\"'\"'s'")


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_missing_file_raises_a_helpful_error(self):
        with self.assertRaises(ConfigError) as ctx:
            common.load_config(self.root / "config.json")
        self.assertIn("config.example.json", str(ctx.exception))

    def test_invalid_json_raises_config_error(self):
        path = self.root / "config.json"
        path.write_text("{ not valid json")
        with self.assertRaises(ConfigError) as ctx:
            common.load_config(path)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_non_object_json_raises_config_error(self):
        path = self.root / "config.json"
        path.write_text("[1, 2, 3]")
        with self.assertRaises(ConfigError):
            common.load_config(path)


class TelegramCredentialTests(unittest.TestCase):
    def test_missing_credentials_raise_without_echoing_values(self):
        import os

        saved = {k: os.environ.pop(k, None) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        self.addCleanup(lambda: os.environ.update({k: v for k, v in saved.items() if v is not None}))
        with self.assertRaises(ConfigError) as ctx:
            common.telegram_credentials()
        message = str(ctx.exception)
        self.assertIn("TELEGRAM_BOT_TOKEN", message)
        self.assertIn(".env", message)


if __name__ == "__main__":
    unittest.main()
