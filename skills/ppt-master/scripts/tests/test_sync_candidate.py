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


if __name__ == "__main__":
    unittest.main()
