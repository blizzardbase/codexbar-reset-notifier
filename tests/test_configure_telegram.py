"""Telegram setup helpers; network access is always mocked."""

import stat
import tempfile
import unittest
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


class TelegramUpdateTests(unittest.TestCase):
    def test_invalid_json_becomes_a_friendly_runtime_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        with mock.patch.object(configure_telegram.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                configure_telegram.fetch_updates("token")
        self.assertEqual(str(ctx.exception), "Telegram API returned an invalid JSON response")


if __name__ == "__main__":
    unittest.main()
