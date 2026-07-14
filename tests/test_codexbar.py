"""The exact CodexBar subprocess invocation, and how its output is interpreted.

No test here executes the real CodexBar binary; subprocess.run is patched.
"""
import json
import io
import subprocess
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import monitor
from common import ConfigError

CB = "/opt/homebrew/bin/codexbar"

# The invocation CodexBar 0.37.2 documents: `codexbar usage --provider <p> ...`
EXPECTED = [CB, "usage", "--provider", "claude", "--format", "json", "--json-only"]


def completed(stdout="[]", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def record(email=None, resets_at="2026-07-10T05:00:00Z", window=300):
    usage = {"primary": {"resetsAt": resets_at, "windowMinutes": window, "usedPercent": 74}}
    if email:
        usage["accountEmail"] = email
    return {"provider": "claude", "usage": usage}


class CommandConstructionTests(unittest.TestCase):
    """Assert the exact argument list, element for element."""

    def test_exact_argv(self):
        self.assertEqual(monitor.build_codexbar_command(CB, "claude"), EXPECTED)

    def test_usage_subcommand_is_explicit(self):
        self.assertEqual(monitor.build_codexbar_command(CB, "claude")[1], "usage")

    def test_json_only_is_requested_so_stdout_is_pure_json(self):
        command = monitor.build_codexbar_command(CB, "codex")
        self.assertIn("--json-only", command)
        self.assertEqual(command[command.index("--format") + 1], "json")

    def test_provider_is_passed_through(self):
        self.assertEqual(
            monitor.build_codexbar_command(CB, "codex"),
            [CB, "usage", "--provider", "codex", "--format", "json", "--json-only"],
        )

    def test_no_account_flag_is_ever_passed(self):
        # Verified against CodexBar 0.37.2: every one of these flags fails for
        # both providers this project supports.
        #   claude --account/--account-index/--all-accounts
        #     -> "No token accounts configured for claude."
        #   codex  --account/--account-index
        #     -> "codex does not support token accounts."
        # Passing any of them would replace every notification with an error.
        for provider in ("claude", "codex"):
            command = monitor.build_codexbar_command(CB, provider)
            self.assertNotIn("--account", command)
            self.assertNotIn("--account-index", command)
            self.assertNotIn("--all-accounts", command)

    def test_command_takes_no_account_argument(self):
        with self.assertRaises(TypeError):
            monitor.build_codexbar_command(CB, "claude", account="work@example.com")


class SubprocessArgumentTests(unittest.TestCase):
    """The argv actually handed to subprocess.run."""

    def test_fetch_provider_invokes_the_expected_argv(self):
        with mock.patch.object(subprocess, "run", return_value=completed(json.dumps([record()]))) as run:
            monitor.fetch_provider(CB, "claude")
        self.assertEqual(run.call_args.args[0], EXPECTED)

    def test_collect_records_calls_codexbar_once_per_provider(self):
        config = {"providers": ["claude", "codex"], "codexbar_path": CB}
        payload = json.dumps([record()])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)) as run:
                records = monitor.collect_records(config)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], EXPECTED)
        self.assertEqual(run.call_args_list[1].args[0][3], "codex")
        self.assertEqual(sorted(records), ["claude", "codex"])

    def test_no_call_carries_an_account_flag(self):
        config = {"providers": ["claude", "codex"], "codexbar_path": CB}
        payload = json.dumps([record()])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)) as run:
                monitor.collect_records(config)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertFalse(any(a.startswith("--account") or a == "--all-accounts" for a in argv))


class ErrorReportingTests(unittest.TestCase):
    """CodexBar reports provider failures as JSON on stdout, even when exiting 0."""

    NO_TOKEN_ACCOUNTS = json.dumps(
        [{"provider": "claude", "error": {"code": 1, "kind": "provider",
                                          "message": "No token accounts configured for claude."}}]
    )
    UNSUPPORTED = json.dumps(
        [{"provider": "codex", "error": {"message": "Error: codex does not support token accounts."}}]
    )
    AUTH_FAILURE = json.dumps(
        [{"provider": "claude", "error": {"message": "Unauthorized: cookies expired."}}]
    )

    def test_nonzero_exit_surfaces_the_json_error_message(self):
        with mock.patch.object(subprocess, "run", return_value=completed(self.NO_TOKEN_ACCOUNTS, 1)):
            with self.assertRaises(monitor.CodexbarError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("No token accounts configured", str(ctx.exception))

    def test_zero_exit_with_an_embedded_error_is_still_a_failure(self):
        with mock.patch.object(subprocess, "run", return_value=completed(self.UNSUPPORTED, 0)):
            with self.assertRaises(monitor.CodexbarError):
                monitor.fetch_provider(CB, "codex")

    def test_every_provider_error_surfaces_and_none_is_downgraded(self):
        # A real failure (expired auth) must never be swallowed or reinterpreted
        # as "this provider has a single account".
        for payload in (self.NO_TOKEN_ACCOUNTS, self.UNSUPPORTED, self.AUTH_FAILURE):
            with mock.patch.object(subprocess, "run", return_value=completed(payload, 1)):
                with self.assertRaises(monitor.CodexbarError):
                    monitor.fetch_provider(CB, "claude")

    def test_auth_failure_message_is_preserved_verbatim(self):
        with mock.patch.object(subprocess, "run", return_value=completed(self.AUTH_FAILURE, 1)):
            with self.assertRaises(monitor.CodexbarError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertEqual(ctx.exception.detail, "Unauthorized: cookies expired.")

    def test_invalid_json_is_reported_clearly(self):
        with mock.patch.object(subprocess, "run", return_value=completed("not json")):
            with self.assertRaises(monitor.CodexbarError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_timeout_is_reported_clearly(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(EXPECTED, 60)):
            with self.assertRaises(monitor.CodexbarError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("timed out", str(ctx.exception))

    def test_stderr_is_used_when_stdout_carries_no_json_error(self):
        with mock.patch.object(subprocess, "run", return_value=completed("", 2, "boom")):
            with self.assertRaises(monitor.CodexbarError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("boom", str(ctx.exception))

    def test_codexbar_error_is_a_runtime_error(self):
        self.assertTrue(issubclass(monitor.CodexbarError, RuntimeError))


class RecordShapeTests(unittest.TestCase):
    """Matches CodexBar 0.37.2: usage.accountEmail, primary.resetsAt, windowMinutes."""

    def test_single_record_is_used(self):
        config = {"providers": ["claude"], "codexbar_path": CB}
        payload = json.dumps([record("solo@example.com")])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)):
                records = monitor.collect_records(config)
        self.assertEqual(
            records["claude"]["usage"]["primary"],
            {"resetsAt": "2026-07-10T05:00:00Z", "windowMinutes": 300},
        )

    def test_multiple_records_are_never_silently_reduced_to_the_first(self):
        config = {"providers": ["claude"], "codexbar_path": CB}
        payload = json.dumps([record("a@example.com"), record("b@example.com")])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)):
                with self.assertRaises(ConfigError) as ctx:
                    monitor.collect_records(config)
        message = str(ctx.exception)
        self.assertNotIn("a@example.com", message)
        self.assertNotIn("b@example.com", message)
        self.assertIn("2 accounts", message)
        self.assertIn("cannot choose between them", message)

    def test_no_records_is_rejected(self):
        config = {"providers": ["claude"], "codexbar_path": CB}
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed("[]")):
                with self.assertRaises(ConfigError):
                    monitor.collect_records(config)

    def test_account_email_and_used_percent_never_reach_the_vps_payload(self):
        slim = monitor.slim_record(record("secret@example.com"))
        text = json.dumps(slim)
        self.assertNotIn("secret@example.com", text)
        self.assertNotIn("usedPercent", text)
        self.assertNotIn("accountEmail", text)

    def test_codex_weekly_only_record_is_kept_without_an_invented_session(self):
        weekly_only = {
            "provider": "codex",
            "usage": {
                "primary": None,
                "secondary": {
                    "resetsAt": "2026-07-16T00:00:00Z",
                    "windowMinutes": 10080,
                    "usedPercent": 40,
                },
            },
        }
        slim = monitor.slim_record(weekly_only)
        self.assertEqual(
            slim,
            {
                "usage": {
                    "secondary": {"resetsAt": "2026-07-16T00:00:00Z", "windowMinutes": 10080}
                }
            },
        )


class CodexBarAvailabilityTests(unittest.TestCase):
    def test_app_bundle_helper_is_a_discovery_fallback(self):
        helper = "/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI"
        with mock.patch.object(monitor.shutil, "which", return_value=None):
            with mock.patch.object(monitor, "CODEXBAR_FALLBACKS", (helper,)):
                with mock.patch.object(monitor.Path, "is_file", return_value=True):
                    self.assertEqual(monitor.resolve_codexbar({"codexbar_path": None}), helper)

    def test_status_omits_a_missing_codex_session_window(self):
        records = {
            "claude": {"usage": {"primary": {"resetsAt": "2026-07-10T12:00:00Z"}}},
            "codex": {"usage": {"secondary": {"resetsAt": "2026-07-16T00:00:00Z"}}},
        }
        config = {"notification_mode": "vps", "providers": ["claude", "codex"], "timezone": "UTC"}
        output = io.StringIO()
        with mock.patch.object(monitor, "collect_records", return_value=records):
            with mock.patch.object(monitor, "datetime") as clock:
                clock.now.return_value = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
                with redirect_stdout(output):
                    monitor.run_status(config)
        self.assertIn("Codex weekly next reset:", output.getvalue())
        self.assertNotIn("Codex session next reset:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
