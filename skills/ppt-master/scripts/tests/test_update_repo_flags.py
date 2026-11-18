#!/usr/bin/env python3
"""Regression tests for the update-repo flag compatibility contract."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import update_repo  # noqa: E402


class UpdateRepoFlagTests(unittest.TestCase):
    def test_skip_deps_flag_disables_dependency_sync(self) -> None:
        self.assertTrue(update_repo.parse_args(["--skip-deps"]).skip_deps)

    def test_upstream_skip_pip_alias_maps_to_skip_deps(self) -> None:
        self.assertTrue(update_repo.parse_args(["--skip-pip"]).skip_deps)

    def test_dependency_sync_is_enabled_by_default(self) -> None:
        self.assertFalse(update_repo.parse_args([]).skip_deps)


if __name__ == "__main__":
    unittest.main()
