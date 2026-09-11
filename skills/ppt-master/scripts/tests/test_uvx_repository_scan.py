from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "skills" / "ppt-master" / "scripts" / "check_uvx_repository.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_uvx_repository_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load check_uvx_repository.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")


class RepositoryUvxScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def _new_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        run_git(repo, "init", "-b", "main")
        run_git(repo, "config", "user.name", "Scanner Test")
        run_git(repo, "config", "user.email", "scanner@example.invalid")
        cli = "COMMANDS = {'project': 'project_manager.py'}\nALIASES = {}\n"
        (repo / "cli.py").write_text(cli, encoding="utf-8")
        (repo / "skills" / "ppt-master").mkdir(parents=True)
        (repo / "skills" / "ppt-master" / "cli.py").write_text(cli, encoding="utf-8")
        return repo

    def _commit(self, repo: Path) -> None:
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", "scanner fixture")

    def test_scans_root_examples_hidden_workflow_prompt_and_skill_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._new_repo(Path(temporary))
            command = "python skills/ppt-master/scripts/project_manager.py\n"
            files = {
                "README.md": command,
                "examples/guide.md": command,
                ".github/workflows/hidden-gate.yml": f"run: {command.strip()}\n",
                ".opencode/command/release.md": command,
                "skills/ppt-master/README.md": command,
            }
            for relative, content in files.items():
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            self._commit(repo)

            violations = self.module.scan_repository(repo)

            self.assertEqual({violation.path for violation in violations}, set(files))
            self.assertTrue(all(violation.line == 1 for violation in violations))
            self.assertTrue(all(violation.rule == "python-script" for violation in violations))

    def test_exact_allowlist_is_path_and_rule_based_not_content_keyword_based(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._new_repo(Path(temporary))
            command = "upstream-sync python skills/ppt-master/scripts/project_manager.py\n"
            allowed = repo / "docs" / "zh" / "upstream-sync.md"
            unrelated = repo / "docs" / "other.md"
            allowed.parent.mkdir(parents=True)
            allowed.write_text(command, encoding="utf-8")
            unrelated.write_text(command, encoding="utf-8")
            self._commit(repo)
            before = {path: path.read_bytes() for path in (allowed, unrelated)}

            violations = self.module.scan_repository(repo)

            self.assertEqual({violation.path for violation in violations}, {"docs/other.md"})
            self.assertEqual(before[allowed], allowed.read_bytes())
            self.assertEqual(before[unrelated], unrelated.read_bytes())

    def test_untracked_files_are_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._new_repo(Path(temporary))
            self._commit(repo)
            untracked = repo / "README.md"
            untracked.write_text("python skills/ppt-master/scripts/project_manager.py\n", encoding="utf-8")

            self.assertEqual(self.module.scan_repository(repo), [])

    def test_allowlist_is_scoped_to_the_exact_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._new_repo(Path(temporary))
            path = repo / "docs" / "superpowers" / "plans" / "2026-09-10-sync-upstream-ancestry.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "python skills/ppt-master/scripts/project_manager.py\n"
                "uv run skills/ppt-master/scripts/project_manager.py\n",
                encoding="utf-8",
            )
            self._commit(repo)

            violations = self.module.scan_repository(repo)

            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0].rule, "uv-run-script")

    def test_tracked_symlink_document_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._new_repo(Path(temporary))
            target = repo / "outside.md"
            target.write_text("python skills/ppt-master/scripts/project_manager.py\n", encoding="utf-8")
            link = repo / "README.md"
            try:
                os.symlink(target, link)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            self._commit(repo)

            with self.assertRaises(self.module.ScannerError):
                self.module.scan_repository(repo)


if __name__ == "__main__":
    unittest.main()
