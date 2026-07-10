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
