from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
CHECKER = ROOT / ".github" / "scripts" / "check_sync_candidate.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_sync_candidate_under_test", CHECKER)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load check_sync_candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


class TrustedCandidateCheckerTests(unittest.TestCase):
    def _materialize_candidate(self, root: Path, destination: Path) -> None:
        run_git(root, "worktree", "add", "--detach", "--no-checkout", str(destination), "HEAD")
        run_git(destination, "read-tree", "--reset", "-u", "HEAD")

    def _run_checker(self, candidate_root: Path) -> subprocess.CompletedProcess[str]:
        head_sha = run_git(ROOT, "rev-parse", "HEAD").stdout.strip()
        return subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--repo",
                str(ROOT),
                "--candidate-root",
                str(candidate_root),
                "--base-sha",
                head_sha,
                "--head-sha",
                head_sha,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_base_checker_accepts_current_candidate_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_root = Path(temporary) / "candidate"
            self._materialize_candidate(ROOT, candidate_root)
            try:
                result = self._run_checker(candidate_root)
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                run_git(ROOT, "worktree", "remove", "--force", str(candidate_root), check=False)

    def test_candidate_noop_checker_cannot_bypass_trusted_base_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_root = Path(temporary) / "candidate"
            self._materialize_candidate(ROOT, candidate_root)
            try:
                candidate_checker = candidate_root / ".github" / "scripts" / "check_sync_candidate.py"
                candidate_checker.write_text(
                    "from pathlib import Path\n"
                    "Path('candidate-checker-executed').write_text('executed')\n",
                    encoding="utf-8",
                )
                cli = candidate_root / "cli.py"
                cli.write_text(
                    cli.read_text(encoding="utf-8").replace(
                        '"project":                "project_manager.py"',
                        '"project":                "candidate-no-op.py"',
                        1,
                    ),
                    encoding="utf-8",
                )
                result = self._run_checker(candidate_root)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CLI", result.stderr)
                self.assertFalse((candidate_root / "candidate-checker-executed").exists())
            finally:
                run_git(ROOT, "worktree", "remove", "--force", str(candidate_root), check=False)


    def test_candidate_git_text_paths_pin_utf8_replacement(self) -> None:
        module = load_checker()
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
        with mock.patch.object(module.subprocess, "run", return_value=completed) as mocked:
            module._run_git_text(ROOT, "status")
        self.assertEqual(mocked.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(mocked.call_args.kwargs.get("errors"), "replace")


    def test_candidate_overlay_policy_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_root = Path(temporary) / "candidate"
            self._materialize_candidate(ROOT, candidate_root)
            try:
                policy = candidate_root / ".github" / "upstream-overlay-paths.txt"
                policy.write_text(
                    policy.read_text(encoding="utf-8") + "injected.txt merge unauthorized\n",
                    encoding="utf-8",
                )
                result = self._run_checker(candidate_root)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("overlay", result.stderr)
            finally:
                run_git(ROOT, "worktree", "remove", "--force", str(candidate_root), check=False)

    def test_projects_root_import_accepts_grouped_and_multiline_forms(self) -> None:
        imports = (
            "from project_management.paths import (projects_root)",
            "from project_management.paths import REPO_ROOT, projects_root",
            "from project_management.paths import (  # noqa: E402\n"
            "    REPO_ROOT,\n    projects_root,\n)",
        )
        with tempfile.TemporaryDirectory() as temporary:
            candidate_root = Path(temporary) / "candidate"
            self._materialize_candidate(ROOT, candidate_root)
            try:
                paths = [
                    candidate_root / "skills/ppt-master/scripts/config.py",
                    candidate_root / "skills/ppt-master/scripts/project_manager.py",
                ]
                originals = [path.read_text(encoding="utf-8") for path in paths]
                marker = "from project_management.paths import projects_root"
                for original in originals:
                    self.assertEqual(original.count(marker), 1)
                for statement in imports:
                    with self.subTest(statement=statement):
                        for path, original in zip(paths, originals):
                            path.write_text(original.replace(marker, statement), encoding="utf-8")
                        result = self._run_checker(candidate_root)
                        self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                run_git(ROOT, "worktree", "remove", "--force", str(candidate_root), check=False)

    def test_projects_root_import_rejects_missing_or_invalid_bindings(self) -> None:
        marker = "from project_management.paths import projects_root"
        invalid_imports = (
            "# " + marker,
            repr(marker),
            "from other.paths import projects_root",
            "from .project_management.paths import projects_root",
            marker + " as unused_projects_root",
            "def unused():\n    " + marker,
            "from project_management.paths import (",
        )
        with tempfile.TemporaryDirectory() as temporary:
            candidate_root = Path(temporary) / "candidate"
            self._materialize_candidate(ROOT, candidate_root)
            try:
                for relative in (
                    "skills/ppt-master/scripts/config.py",
                    "skills/ppt-master/scripts/project_manager.py",
                ):
                    path = candidate_root / relative
                    original = path.read_text(encoding="utf-8")
                    self.assertEqual(original.count(marker), 1)
                    for statement in invalid_imports:
                        with self.subTest(path=relative, statement=statement):
                            path.write_text(original.replace(marker, statement), encoding="utf-8")
                            result = self._run_checker(candidate_root)
                            self.assertNotEqual(result.returncode, 0)
                            if statement == "from project_management.paths import (":
                                self.assertTrue(result.stderr)
                            else:
                                self.assertIn(relative, result.stderr)
                    path.write_text(original, encoding="utf-8")
            finally:
                run_git(ROOT, "worktree", "remove", "--force", str(candidate_root), check=False)


if __name__ == "__main__":
    unittest.main()
