"""The exact CodexBar subprocess invocation, and how its output is interpreted.

No test here executes the real CodexBar binary; subprocess.run is patched.
"""
import json
import subprocess
import unittest
from unittest import mock

import common
import monitor
from common import ConfigError

CB = "/opt/homebrew/bin/codexbar"

# The invocation CodexBar 0.37.2 documents: `codexbar usage --provider <p> ...`
BASE = [CB, "usage", "--provider", "claude", "--format", "json", "--json-only"]


def completed(stdout="[]", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def record(email=None, resets_at="2026-07-10T05:00:00Z", window=300):
    usage = {"primary": {"resetsAt": resets_at, "windowMinutes": window, "usedPercent": 74}}
    if email:
        usage["accountEmail"] = email
    return {"provider": "claude", "usage": usage}


class CommandConstructionTests(unittest.TestCase):
    """Assert the exact argument list, element for element."""

    def test_default_invocation_passes_no_account_flags(self):
        self.assertEqual(monitor.build_codexbar_command(CB, "claude"), BASE)

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

    def test_configured_account_appends_account_flag(self):
        self.assertEqual(
            monitor.build_codexbar_command(CB, "claude", account="work@example.com"),
            BASE + ["--account", "work@example.com"],
        )

    def test_all_accounts_appends_all_accounts_flag(self):
        self.assertEqual(
            monitor.build_codexbar_command(CB, "claude", all_accounts=True),
            BASE + ["--all-accounts"],
        )

    def test_account_wins_over_all_accounts(self):
        # CodexBar: "Account selection requires a single provider." Passing both
        # is meaningless; --account is the more specific request.
        command = monitor.build_codexbar_command(CB, "claude", account="a@b.c", all_accounts=True)
        self.assertEqual(command, BASE + ["--account", "a@b.c"])
        self.assertNotIn("--all-accounts", command)

    def test_no_account_flag_leaks_into_the_default_call(self):
        # Passing --all-accounts unconditionally breaks OAuth/cookie providers,
        # which reject it with "No token accounts configured".
        command = monitor.build_codexbar_command(CB, "claude")
        self.assertNotIn("--all-accounts", command)
        self.assertNotIn("--account", command)

    def test_empty_account_string_is_treated_as_unset(self):
        self.assertEqual(monitor.build_codexbar_command(CB, "claude", account=""), BASE)


class SubprocessArgumentTests(unittest.TestCase):
    """The argv actually handed to subprocess.run."""

    def test_fetch_provider_invokes_the_expected_argv(self):
        with mock.patch.object(subprocess, "run", return_value=completed(json.dumps([record()]))) as run:
            monitor.fetch_provider(CB, "claude")
        self.assertEqual(run.call_args.args[0], BASE)

    def test_fetch_provider_with_account_invokes_the_expected_argv(self):
        payload = json.dumps([record("work@example.com")])
        with mock.patch.object(subprocess, "run", return_value=completed(payload)) as run:
            monitor.fetch_provider(CB, "claude", "work@example.com")
        self.assertEqual(run.call_args.args[0], BASE + ["--account", "work@example.com"])

    def test_list_provider_accounts_invokes_all_accounts(self):
        payload = json.dumps([record("a@example.com"), record("b@example.com")])
        with mock.patch.object(subprocess, "run", return_value=completed(payload)) as run:
            names = monitor.list_provider_accounts(CB, "claude")
        self.assertEqual(run.call_args.args[0], BASE + ["--all-accounts"])
        self.assertEqual(names, ["a@example.com", "b@example.com"])

    def test_collect_records_calls_codexbar_once_per_provider(self):
        config = {
            "providers": ["claude", "codex"],
            "accounts": {},
            "codexbar_path": CB,
        }
        payload = json.dumps([record()])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)) as run:
                records = monitor.collect_records(config)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][3], "claude")
        self.assertEqual(run.call_args_list[1].args[0][3], "codex")
        self.assertEqual(sorted(records), ["claude", "codex"])

    def test_collect_records_passes_the_configured_account_only_for_that_provider(self):
        config = {
            "providers": ["claude", "codex"],
            "accounts": {"codex": "work@example.com"},
            "codexbar_path": CB,
        }
        payload = json.dumps([record()])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)) as run:
                monitor.collect_records(config)
        claude_argv, codex_argv = (c.args[0] for c in run.call_args_list)
        self.assertNotIn("--account", claude_argv)
        self.assertEqual(codex_argv[-2:], ["--account", "work@example.com"])


class ErrorReportingTests(unittest.TestCase):
    """CodexBar reports provider failures as JSON on stdout, even when exiting 1."""

    ERROR_PAYLOAD = json.dumps(
        [{"provider": "claude", "error": {"code": 1, "kind": "provider",
                                          "message": "No token accounts configured for claude."}}]
    )

    def test_nonzero_exit_surfaces_the_json_error_message(self):
        with mock.patch.object(subprocess, "run", return_value=completed(self.ERROR_PAYLOAD, 1)):
            with self.assertRaises(RuntimeError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("No token accounts configured", str(ctx.exception))

    def test_account_selection_on_an_oauth_provider_explains_the_fix(self):
        # The real failure mode: --account only works for CodexBar token accounts.
        with mock.patch.object(subprocess, "run", return_value=completed(self.ERROR_PAYLOAD, 1)):
            with self.assertRaises(ConfigError) as ctx:
                monitor.fetch_provider(CB, "claude", "work@example.com")
        message = str(ctx.exception)
        self.assertIn("accounts", message)
        self.assertIn("config.json", message)

    def test_zero_exit_with_an_embedded_error_is_still_a_failure(self):
        with mock.patch.object(subprocess, "run", return_value=completed(self.ERROR_PAYLOAD, 0)):
            with self.assertRaises(RuntimeError):
                monitor.fetch_provider(CB, "claude")

    def test_invalid_json_is_reported_clearly(self):
        with mock.patch.object(subprocess, "run", return_value=completed("not json")):
            with self.assertRaises(RuntimeError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_timeout_is_reported_clearly(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(BASE, 60)):
            with self.assertRaises(RuntimeError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("timed out", str(ctx.exception))

    def test_stderr_is_used_when_stdout_carries_no_json_error(self):
        with mock.patch.object(subprocess, "run", return_value=completed("", 2, "boom")):
            with self.assertRaises(RuntimeError) as ctx:
                monitor.fetch_provider(CB, "claude")
        self.assertIn("boom", str(ctx.exception))


class RecordShapeTests(unittest.TestCase):
    """Matches CodexBar 0.37.2: usage.accountEmail, primary.resetsAt, windowMinutes."""

    def test_single_record_is_used_without_configuration(self):
        config = {"providers": ["claude"], "accounts": {}, "codexbar_path": CB}
        payload = json.dumps([record("solo@example.com")])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)):
                records = monitor.collect_records(config)
        self.assertEqual(
            records["claude"]["usage"]["primary"],
            {"resetsAt": "2026-07-10T05:00:00Z", "windowMinutes": 300},
        )

    def test_a_filtered_single_record_is_trusted_even_if_the_label_differs(self):
        # --account takes a label, which need not equal accountEmail. When
        # CodexBar has already narrowed to one record, do not re-match on email.
        config = {"providers": ["claude"], "accounts": {"claude": "work-label"}, "codexbar_path": CB}
        payload = json.dumps([record("work@example.com")])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)):
                records = monitor.collect_records(config)
        self.assertIn("primary", records["claude"]["usage"])

    def test_multiple_records_without_configuration_are_rejected(self):
        config = {"providers": ["claude"], "accounts": {}, "codexbar_path": CB}
        payload = json.dumps([record("a@example.com"), record("b@example.com")])
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)):
                with self.assertRaises(ConfigError) as ctx:
                    monitor.collect_records(config)
        self.assertIn("a@example.com", str(ctx.exception))
        self.assertIn("b@example.com", str(ctx.exception))

    def test_multiple_records_select_the_configured_account(self):
        config = {
            "providers": ["claude"],
            "accounts": {"claude": "b@example.com"},
            "codexbar_path": CB,
        }
        payload = json.dumps(
            [record("a@example.com", "2026-07-10T05:00:00Z"), record("b@example.com", "2026-07-10T09:00:00Z")]
        )
        with mock.patch.object(monitor.Path, "is_file", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=completed(payload)):
                records = monitor.collect_records(config)
        self.assertEqual(records["claude"]["usage"]["primary"]["resetsAt"], "2026-07-10T09:00:00Z")

    def test_no_records_is_rejected(self):
        config = {"providers": ["claude"], "accounts": {}, "codexbar_path": CB}
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


if __name__ == "__main__":
    unittest.main()
