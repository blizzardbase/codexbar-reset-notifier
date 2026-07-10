"""Telegram setup helpers; network access is always mocked."""

import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import configure_telegram


class EnvWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"

    def test_existing_structure_and_unrelated_lines_are_preserved(self):
        original = (
            "# Telegram credentials\n"
            "\n"
            "TELEGRAM_BOT_TOKEN=old-token\n"
            "UNRELATED = keep-this-verbatim\n"
            "TELEGRAM_CHAT_ID=old-chat\n"
        )
        self.path.write_text(original)

        configure_telegram.write_env(
            self.path,
            {"TELEGRAM_BOT_TOKEN": "new-token", "TELEGRAM_CHAT_ID": "new-chat"},
        )

        self.assertEqual(
            self.path.read_text(),
            original.replace("old-token", "new-token").replace("old-chat", "new-chat"),
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_new_keys_are_appended_without_destroying_existing_content(self):
        self.path.write_text("# keep\nOTHER=value")
        configure_telegram.write_env(self.path, {"TELEGRAM_CHAT_ID": "123"})
        self.assertEqual(self.path.read_text(), "# keep\nOTHER=value\nTELEGRAM_CHAT_ID=123\n")

    def test_failed_atomic_replace_leaves_original_untouched(self):
        self.path.write_text("TELEGRAM_CHAT_ID=old\n")
        self.path.chmod(0o640)
        with mock.patch.object(Path, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                configure_telegram.write_env(self.path, {"TELEGRAM_CHAT_ID": "new"})
        self.assertEqual(self.path.read_text(), "TELEGRAM_CHAT_ID=old\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(list(self.path.parent.glob("..env.*")), [])


class TelegramUpdateTests(unittest.TestCase):
    def response(self, payload: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        response.__exit__.return_value = False
        return response

    def test_invalid_json_becomes_a_friendly_runtime_error(self):
        response = self.response(b"not json")
        with mock.patch.object(configure_telegram.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                configure_telegram.fetch_updates("token")
        self.assertEqual(str(ctx.exception), "Telegram API returned an invalid JSON response")

    def test_valid_updates_are_returned(self):
        response = self.response(b'{"ok": true, "result": [{"update_id": 1}]}')
        with mock.patch.object(configure_telegram.urllib.request, "urlopen", return_value=response):
            self.assertEqual(configure_telegram.fetch_updates("token"), [{"update_id": 1}])

    def test_empty_updates_are_returned_as_an_empty_list(self):
        response = self.response(b'{"ok": true, "result": []}')
        with mock.patch.object(configure_telegram.urllib.request, "urlopen", return_value=response):
            self.assertEqual(configure_telegram.fetch_updates("token"), [])

    def test_http_and_transport_errors_are_friendly_and_hide_the_token(self):
        token = "private-test-token"
        failures = (
            urllib.error.HTTPError("https://api.telegram.org", 401, "unauthorized", {}, None),
            urllib.error.URLError("offline"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(
                    configure_telegram.urllib.request, "urlopen", side_effect=failure
                ):
                    with self.assertRaises(RuntimeError) as ctx:
                        configure_telegram.fetch_updates(token)
                self.assertNotIn(token, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
