#!/usr/bin/env python3
"""Test the repository-only PyPI cleanup policy and report CLI."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError


SCRIPT = Path(__file__).resolve().parents[4] / ".github/scripts/pypi_cleanup.py"
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
