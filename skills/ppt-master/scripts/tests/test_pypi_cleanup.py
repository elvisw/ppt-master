#!/usr/bin/env python3
"""Test the repository-only PyPI cleanup policy and report CLI."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError


SCRIPT = Path(__file__).resolve().parents[4] / ".github/maintenance/pypi_cleanup.py"
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
OLD = "2026-01-01T00:00:00Z"


def metadata(count=12):
    return {
        "info": {"name": "ppt-master", "version": f"1.{count}"},
        "releases": {
            f"1.{number}": [{"size": number, "upload_time_iso_8601": OLD}]
            for number in range(1, count + 1)
        },
    }


class PyPICleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("pypi_cleanup", SCRIPT)
        cls.cleanup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cleanup)

    def test_keeps_ten_largest_pep440_versions_not_lexical_order(self):
        report = self.cleanup.build_report(metadata(), now=NOW)
        self.assertEqual([row["version"] for row in report["keep"]],
                         ["1.12", "1.11", "1.10", "1.9", "1.8", "1.7", "1.6", "1.5", "1.4", "1.3"])
        self.assertEqual([row["version"] for row in report["delete"]], ["1.2", "1.1"])
        self.assertEqual(report["total_bytes"], 78)
        self.assertEqual(report["reclaim_bytes"], 3)

    def test_latest_file_upload_protects_an_old_version(self):
        data = metadata()
        data["releases"]["1.1"].append(
            {"size": 100, "upload_time_iso_8601": "2026-10-18T14:00:00+02:00"}
        )
        report = self.cleanup.build_report(data, now=NOW)
        self.assertEqual(report["keep"][-1], {
            "version": "1.1", "size_bytes": 101, "latest_upload": "2026-10-18T12:00:00Z",
        })
        self.assertEqual(report["reclaim_bytes"], 2)

    def test_cutoff_is_inclusive(self):
        for offset, expected in [(0, ["1.2"]), (-1, ["1.2", "1.1"])]:
            with self.subTest(offset=offset):
                data = metadata()
                stamp = NOW - timedelta(days=30) + timedelta(microseconds=offset)
                data["releases"]["1.1"][0]["upload_time_iso_8601"] = stamp.isoformat()
                report = self.cleanup.build_report(data, now=NOW)
                self.assertEqual([row["version"] for row in report["delete"]], expected)

    def test_fewer_than_ten_versions_are_all_kept(self):
        report = self.cleanup.build_report(metadata(3), now=NOW)
        self.assertEqual(len(report["keep"]), 3)
        self.assertEqual(report["delete"], [])

    def test_empty_releases_do_not_take_latest_slots(self):
        data = metadata()
        data["releases"]["2.0"] = []
        report = self.cleanup.build_report(data, now=NOW)
        self.assertEqual(len(report["keep"]), 10)
        self.assertNotIn("2.0", [row["version"] for row in report["delete"]])
        self.assertEqual(report["reclaim_bytes"], 3)

    def test_default_version_is_kept_even_when_old(self):
        data = metadata()
        data["info"]["version"] = "1.1"
        report = self.cleanup.build_report(data, now=NOW)
        self.assertEqual([row["version"] for row in report["delete"]], ["1.2"])

    def test_pre_release_and_post_release_follow_pep440(self):
        data = metadata(0)
        versions = ["1.0.dev1", "1.0a1", "1.0b1", "1.0rc1", "1.0", "1.0.post1"]
        data["info"]["version"] = "1.0"
        data["releases"] = {version: [{"size": 0, "upload_time_iso_8601": OLD}]
                            for version in versions}
        report = self.cleanup.build_report(data, now=NOW)
        self.assertEqual([row["version"] for row in report["keep"]], list(reversed(versions)))

    def test_report_contract_and_pure_input(self):
        data = metadata()
        original = copy.deepcopy(data)
        report = self.cleanup.build_report(data, now=NOW)
        self.assertEqual(set(report), {"project", "generated_at", "keep_latest", "keep_days",
                                      "total_bytes", "reclaim_bytes", "keep", "delete"})
        self.assertEqual(report["project"], "ppt-master")
        self.assertEqual(report["generated_at"], "2026-10-18T12:00:00Z")
        self.assertEqual((report["keep_latest"], report["keep_days"]), (10, 30))
        self.assertEqual(set(report["delete"][0]), {"version", "size_bytes", "latest_upload"})
        self.assertEqual(data, original)

    def test_invalid_metadata_fails_closed(self):
        cases = []
        for version in ["not-a-version", ""]:
            data = metadata()
            data["releases"][version] = []
            cases.append(data)
        for stamp in [None, "", "2026-01-01T00:00:00", "invalid"]:
            data = metadata()
            data["releases"]["1.1"][0]["upload_time_iso_8601"] = stamp
            cases.append(data)
        for size in [-1, True, 1.5, "10", None]:
            data = metadata()
            data["releases"]["1.1"][0]["size"] = size
            cases.append(data)
        data = metadata()
        del data["releases"]["1.1"][0]["upload_time_iso_8601"]
        cases.append(data)
        for field, value in [("name", "another-project"), ("version", "invalid")]:
            data = metadata()
            data["info"][field] = value
            cases.append(data)
        cases.extend([{}, {"info": {}}, {"info": metadata()["info"], "releases": []}])
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.cleanup.build_report(data, now=NOW)

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            self.cleanup.build_report(metadata(), now=NOW.replace(tzinfo=None))

    def test_fetch_uses_only_public_project_endpoint_with_timeout(self):
        def open_public(url, *, timeout):
            self.assertEqual(url, "https://pypi.org/pypi/ppt-master/json")
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 60)
            return io.BytesIO(json.dumps(metadata()).encode("utf-8"))

        with patch.object(self.cleanup, "urlopen", side_effect=open_public):
            self.assertEqual(self.cleanup.fetch_metadata(), metadata())

    def test_cli_writes_both_report_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "report.json"
            markdown_path = Path(directory) / "report.md"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(self.cleanup, "fetch_metadata", return_value=metadata()), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = self.cleanup.main([
                    "report", "--json", str(json_path), "--markdown", str(markdown_path),
                ])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["total_bytes"], 78)
            text = markdown_path.read_text(encoding="utf-8")
            self.assertIn("ppt-master", text)
            self.assertIn("1.12", text)
            self.assertIn("1.1", text)
            self.assertIn("78", text)
            self.assertEqual(stdout.getvalue(), "")

    def test_cli_accepts_each_output_independently(self):
        for option, filename in [("--json", "report.json"), ("--markdown", "report.md")]:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / filename
                with patch.object(self.cleanup, "fetch_metadata", return_value=metadata()), \
                        redirect_stdout(io.StringIO()):
                    result = self.cleanup.main(["report", option, str(path)])
                self.assertEqual(result, 0)
                self.assertTrue(path.is_file())

    def test_cli_defaults_to_markdown_stdout(self):
        stdout = io.StringIO()
        with patch.object(self.cleanup, "fetch_metadata", return_value=metadata()), \
                redirect_stdout(stdout):
            result = self.cleanup.main(["report"])
        self.assertEqual(result, 0)
        self.assertTrue(stdout.getvalue().startswith("# "))
        self.assertIn("ppt-master", stdout.getvalue())
        self.assertIn("Delete", stdout.getvalue())

    def test_network_or_json_failure_produces_no_success_report(self):
        errors = [
            HTTPError("https://pypi.org/pypi/ppt-master/json", 503, "unavailable", {}, None),
            URLError("offline"),
            TimeoutError("timed out"),
        ]
        for error in errors + [None]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                json_path = Path(directory) / "report.json"
                markdown_path = Path(directory) / "report.md"
                stdout, stderr = io.StringIO(), io.StringIO()
                kwargs = ({"side_effect": error} if error else
                          {"return_value": io.BytesIO(b"not json")})
                with patch.object(self.cleanup, "urlopen", **kwargs), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    result = self.cleanup.main([
                        "report", "--json", str(json_path), "--markdown", str(markdown_path),
                    ])
                self.assertNotEqual(result, 0)
                self.assertFalse(json_path.exists())
                self.assertFalse(markdown_path.exists())
                self.assertEqual(stdout.getvalue(), "")
                self.assertTrue(stderr.getvalue())

    def test_invalid_metadata_cli_does_not_write_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(self.cleanup, "fetch_metadata", return_value={}), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = self.cleanup.main(["report", "--json", str(path)])
            self.assertNotEqual(result, 0)
            self.assertFalse(path.exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue())

    def test_help_has_no_network_or_file_writes(self):
        for argv in [["--help"], ["report", "--help"]]:
            with self.subTest(argv=argv), \
                    patch.object(self.cleanup, "urlopen", side_effect=AssertionError("network")), \
                    patch.object(Path, "write_text", side_effect=AssertionError("write")), \
                    redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.cleanup.main(argv)
            self.assertEqual(raised.exception.code, 0)

    def test_invalid_arguments_do_not_fetch(self):
        for argv in [[], ["apply"], ["report", "--unknown"], ["report", "--json"]]:
            with self.subTest(argv=argv), \
                    patch.object(self.cleanup, "urlopen", side_effect=AssertionError("network")), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.cleanup.main(argv)
            self.assertNotEqual(raised.exception.code, 0)


def metadata_without(count, *removed):
    """Return the standard fixture with the named releases removed."""
    data = metadata(count)
    for version in removed:
        del data["releases"][version]
    return data


def metadata_with_new_candidate():
    """Return the standard fixture plus a new, old-upload candidate (1.0)."""
    data = metadata()
    data["releases"]["1.0"] = [{"size": 1, "upload_time_iso_8601": OLD}]
    return data


def protected_metadata():
    """Return a fixture where candidate 1.1 gains a fresh upload and 1.0 appears."""
    data = metadata_with_new_candidate()
    data["releases"]["1.1"].append(
        {"size": 1, "upload_time_iso_8601": datetime.now(timezone.utc).isoformat()}
    )
    return data


class _FakeStdin:
    """Minimal stdin stand-in whose isatty() is controlled by the test."""

    def __init__(self, interactive):
        self._interactive = interactive

    def isatty(self):
        return self._interactive


class PyPICleanupApplyTests(unittest.TestCase):
    """Cover the local interactive apply entry; every destructive step is mocked."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("pypi_cleanup", SCRIPT)
        cls.cleanup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cleanup)

    def invoke_apply(self, fetch, *, answer="DELETE ppt-master", returncode=0,
                     interactive=True, environ=None, argv=None):
        """Run the apply CLI with mocked network, subprocess, clock and confirmation."""
        stdout, stderr = io.StringIO(), io.StringIO()
        run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=returncode))
        simulated = {key: value for key, value in os.environ.items()
                     if key != self.cleanup.ACTION_ENV}
        simulated.update(environ or {})
        with patch.object(self.cleanup.sys, "stdin", _FakeStdin(interactive)), \
                patch.object(self.cleanup, "fetch_metadata", side_effect=fetch) as fetch_mock, \
                patch.object(self.cleanup.subprocess, "run", run), \
                patch.object(self.cleanup.time, "sleep") as sleep, \
                patch("builtins.input", return_value=answer), \
                patch.dict(os.environ, simulated, clear=True), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = self.cleanup.main(argv or ["apply", "--username", "alice"])
        return {
            "result": result, "fetch": fetch_mock, "run": run, "sleep": sleep,
            "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
        }

    def test_build_cleanup_command_is_exact_anchored_and_escaped(self):
        command = self.cleanup.build_cleanup_command(["1.2", "1.0rc1", "1.0+local"], "alice")
        self.assertEqual(command, [
            "uvx", "--from", "pypi-cleanup==0.1.12", "pypi-cleanup",
            "--package", "ppt-master",
            "--username", "alice",
            "--version-regex", r"^(?:1\.0\+local|1\.0rc1|1\.2)$",
            "--days", "30",
            "--yes",
            "--do-it",
        ])
        self.assertNotIn("--delete-project", command)
        self.assertNotIn("--debug", command)
        pattern = command[command.index("--version-regex") + 1]
        self.assertNotIn(".*", pattern)
        for version in ["1.2", "1.0rc1", "1.0+local"]:
            with self.subTest(version=version):
                match = re.match(pattern, version)
                self.assertIsNotNone(match)
                self.assertEqual(match.end(), len(version))
        for other in ["1.20", "1.2.0", "1.0rc10", "21.2", "1.0+locality"]:
            with self.subTest(other=other):
                match = re.match(pattern, other)
                self.assertTrue(match is None or match.end() != len(other))

    def test_build_cleanup_command_rejects_empty_or_blank_inputs(self):
        for versions, username in [([], "alice"), (["1.2"], ""), (["1.2"], "   "), ([""], "alice")]:
            with self.subTest(versions=versions, username=username), self.assertRaises(ValueError):
                self.cleanup.build_cleanup_command(versions, username)

    def test_verify_cleanup_reports_deleted_remaining_and_missing_kept(self):
        before = self.cleanup.build_report(metadata(), now=NOW)
        same = self.cleanup.verify_cleanup(before, before)
        self.assertEqual(same, {"deleted": [], "remaining": ["1.2", "1.1"], "missing_kept": []})
        after = self.cleanup.build_report(metadata_without(12, "1.1", "1.12"), now=NOW)
        result = self.cleanup.verify_cleanup(before, after)
        self.assertEqual(result["deleted"], ["1.1"])
        self.assertEqual(result["remaining"], ["1.2"])
        self.assertEqual(result["missing_kept"], ["1.12"])

    def test_apply_refuses_in_action_or_non_interactive_terminal(self):
        for label, kwargs in [("non-interactive", {"interactive": False}),
                              ("action", {"environ": {"GITHUB_ACTIONS": "true"}})]:
            with self.subTest(label=label):
                outcome = self.invoke_apply([metadata()], **kwargs)
                self.assertNotEqual(outcome["result"], 0)
                outcome["fetch"].assert_not_called()
                outcome["run"].assert_not_called()
                outcome["sleep"].assert_not_called()
                self.assertTrue(outcome["stderr"])

    def test_outer_ci_environment_does_not_contaminate_a_simulated_human_apply(self):
        """An inherited GITHUB_ACTIONS must not turn a simulated local run into a refusal."""
        with patch.dict(os.environ, {self.cleanup.ACTION_ENV: "true"}, clear=False):
            self.assertTrue(os.environ.get(self.cleanup.ACTION_ENV))
            outcome = self.invoke_apply(
                [metadata(), metadata(), metadata_without(12, "1.1", "1.2")]
            )
        self.assertEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        self.assertNotIn(self.cleanup.ACTION_ENV, outcome["run"].call_args.kwargs["env"])
        self.assertIn("Verified deletion", outcome["stdout"])

    def test_apply_refuses_empty_candidate_list_without_subprocess(self):
        outcome = self.invoke_apply([metadata(3)])
        self.assertNotEqual(outcome["result"], 0)
        self.assertEqual(outcome["fetch"].call_count, 1)
        outcome["run"].assert_not_called()
        self.assertIn("No delete candidates", outcome["stderr"])

    def test_apply_stops_on_wrong_confirmation_without_subprocess(self):
        for answer in ["delete ppt-master", "DELETE ppt-master ", "DELETE  ppt-master", ""]:
            with self.subTest(answer=answer):
                outcome = self.invoke_apply([metadata()], answer=answer)
                self.assertNotEqual(outcome["result"], 0)
                outcome["run"].assert_not_called()
                self.assertIn("Confirmation did not match", outcome["stderr"])

    def test_apply_shows_candidates_and_capacity_before_confirming(self):
        outcome = self.invoke_apply([metadata()], answer="wrong")
        self.assertIn("1.1", outcome["stdout"])
        self.assertIn("1.2", outcome["stdout"])
        self.assertIn("Reclaimable: 3 bytes of 78 total nonempty-version bytes.", outcome["stdout"])

    def test_apply_runs_the_fixed_command_and_verifies_success(self):
        outcome = self.invoke_apply(
            [metadata(), metadata(), metadata_without(12, "1.1", "1.2")],
            environ={"PYPI_CLEANUP_PASSWORD": "secret"},
        )
        self.assertEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        call = outcome["run"].call_args
        self.assertEqual(call.args[0], [
            "uvx", "--from", "pypi-cleanup==0.1.12", "pypi-cleanup",
            "--package", "ppt-master",
            "--username", "alice",
            "--version-regex", r"^(?:1\.1|1\.2)$",
            "--days", "30", "--yes", "--do-it",
        ])
        self.assertNotIn("PYPI_CLEANUP_PASSWORD", call.kwargs["env"])
        self.assertNotIn("shell", call.kwargs)
        self.assertNotIn("capture_output", call.kwargs)
        self.assertNotIn("stdout", call.kwargs)
        self.assertEqual(outcome["fetch"].call_count, 3)
        self.assertEqual(outcome["sleep"].call_count, 1)
        outcome["sleep"].assert_called_with(self.cleanup.VERIFY_INTERVAL_SECONDS)
        self.assertIn("Verified deletion", outcome["stdout"])

    def test_apply_retries_only_get_until_candidates_disappear(self):
        outcome = self.invoke_apply([
            metadata(), metadata(),
            metadata_without(12, "1.1"), metadata_without(12, "1.1", "1.2"),
        ])
        self.assertEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        self.assertEqual(outcome["fetch"].call_count, 4)
        self.assertEqual(outcome["sleep"].call_count, 2)

    def test_apply_reports_candidates_not_fully_deleted(self):
        outcome = self.invoke_apply(
            [metadata(), metadata()] + [metadata_without(12, "1.1")] * 6
        )
        self.assertNotEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        self.assertEqual(outcome["sleep"].call_count, self.cleanup.VERIFY_ATTEMPTS)
        self.assertIn("not fully deleted", outcome["stderr"])
        self.assertIn("1.2", outcome["stderr"])

    def test_apply_stops_when_candidate_becomes_protected_after_deletion(self):
        outcome = self.invoke_apply([metadata(), metadata(), protected_metadata()])
        self.assertNotEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        self.assertEqual(outcome["fetch"].call_count, 3)
        self.assertEqual(outcome["sleep"].call_count, 1)
        self.assertIn("protected", outcome["stderr"])
        self.assertIn("1.1", outcome["stderr"])
        self.assertIn("NOT CONFIRMED", outcome["stderr"])
        self.assertIn("not individually verified", outcome["stderr"])
        self.assertNotIn("1.0", outcome["stderr"])

    def test_apply_aborts_before_subprocess_when_candidate_becomes_protected(self):
        outcome = self.invoke_apply([metadata(), protected_metadata()])
        self.assertNotEqual(outcome["result"], 0)
        self.assertEqual(outcome["run"].call_count, 0)
        self.assertEqual(outcome["fetch"].call_count, 2)
        outcome["sleep"].assert_not_called()
        self.assertIn("no longer deletable", outcome["stderr"])
        self.assertIn("1.1", outcome["stderr"])
        self.assertNotIn("1.0", outcome["stderr"])

    def test_apply_aborts_before_subprocess_when_recheck_fails_or_is_invalid(self):
        for label, second in [("network", URLError("offline")), ("invalid", {})]:
            with self.subTest(label=label):
                outcome = self.invoke_apply([metadata(), second])
                self.assertNotEqual(outcome["result"], 0)
                self.assertEqual(outcome["run"].call_count, 0)
                self.assertEqual(outcome["fetch"].call_count, 2)
                outcome["sleep"].assert_not_called()
                self.assertIn("Pre-deletion re-check failed", outcome["stderr"])
                self.assertIn("No deletion was attempted", outcome["stderr"])

    def test_apply_never_adopts_a_new_candidate_before_deletion(self):
        after = metadata_with_new_candidate()
        del after["releases"]["1.1"]
        del after["releases"]["1.2"]
        outcome = self.invoke_apply([metadata(), metadata_with_new_candidate(), after])
        self.assertEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        command = outcome["run"].call_args.args[0]
        self.assertNotIn("1.0", command)
        self.assertEqual(command[command.index("--version-regex") + 1], r"^(?:1\.1|1\.2)$")
        self.assertEqual(outcome["fetch"].call_count, 3)

    def test_apply_reports_verification_query_failure(self):
        outcome = self.invoke_apply([metadata(), metadata(), URLError("offline")])
        self.assertNotEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        self.assertEqual(outcome["sleep"].call_count, 1)
        self.assertIn("verification query failed", outcome["stderr"])
        self.assertIn("NOT CONFIRMED", outcome["stderr"])
        self.assertNotIn("Unfinished candidates", outcome["stderr"])
        self.assertIn("1.1", outcome["stderr"])
        self.assertIn("1.2", outcome["stderr"])

    def test_apply_reports_tool_nonzero_exit_without_verification(self):
        outcome = self.invoke_apply([metadata(), metadata()], returncode=1)
        self.assertNotEqual(outcome["result"], 0)
        outcome["run"].assert_called_once()
        self.assertEqual(outcome["fetch"].call_count, 2)
        outcome["sleep"].assert_not_called()
        self.assertIn("exited with 1", outcome["stderr"])
        self.assertIn("NOT CONFIRMED", outcome["stderr"])
        self.assertIn("not individually verified", outcome["stderr"])
        self.assertNotIn("Unfinished candidates", outcome["stderr"])
        self.assertIn("1.1", outcome["stderr"])
        self.assertIn("1.2", outcome["stderr"])

    def test_apply_help_is_offline_and_write_free(self):
        with patch.object(self.cleanup, "urlopen", side_effect=AssertionError("network")), \
                patch.object(self.cleanup.subprocess, "run", side_effect=AssertionError("run")), \
                patch.object(Path, "write_text", side_effect=AssertionError("write")), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.cleanup.main(["apply", "--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_apply_rejects_password_flag_and_missing_username(self):
        for argv in [["apply"], ["apply", "--username"], ["apply", "--username", "u", "--password", "x"]]:
            with self.subTest(argv=argv), \
                    patch.object(self.cleanup, "urlopen", side_effect=AssertionError("network")), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.cleanup.main(argv)
            self.assertNotEqual(raised.exception.code, 0)
