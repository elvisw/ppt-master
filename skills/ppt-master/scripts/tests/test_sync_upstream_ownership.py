from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
COMMAND = ROOT / ".opencode" / "command" / "sync-upstream.md"
WORKFLOW = ROOT / ".github" / "workflows" / "sync-upstream.yml"


def discover_bash() -> str | None:
    configured = os.environ.get("BASH")
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    discovered = shutil.which("bash")
    if discovered:
        return discovered
    for candidate in (
        Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramW6432", "")) / "Git" / "bin" / "bash.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


BASH = discover_bash()
BASH_AVAILABLE = BASH is not None and Path(BASH).is_file()
MARKER = ".github/upstream-main.sha"
OLD_MARKER = b"old marker\x00\r\n"


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
    )


def run_bash(
    script: str,
    repo: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra_env or {})
    return subprocess.run(
        [BASH],  # type: ignore[list-item]
        cwd=repo,
        env=env,
        input=script,
        text=True,
        capture_output=True,
    )


class SyncUpstreamOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.command_text = COMMAND.read_text(encoding="utf-8")
        cls.workflow_data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        step1_match = re.search(
            r"建立本次同步的 immutable upstream 目标.*?\n\s*```bash\n(.*?)\n\s*```",
            cls.command_text,
            re.DOTALL,
        )
        if step1_match is None:
            raise AssertionError("Step 1 state block not found")
        cls.step1 = textwrap.dedent(step1_match.group(1))

        step2_match = re.search(
            r"### Step 2: 合并上游\n\s*```bash\n(.*?)\n\s*```",
            cls.command_text,
            re.DOTALL,
        )
        if step2_match is None:
            raise AssertionError("Step 2 block not found")
        cls.step2 = textwrap.dedent(step2_match.group(1))

        step4e_section = cls.command_text.split("#### 4e. 提交前门禁", 1)[1]
        step4e_blocks = re.findall(
            r"\n\s*```bash\n(.*?)\n\s*```",
            step4e_section,
            re.DOTALL,
        )
        step4e_block = next(
            (block for block in step4e_blocks if "SYNC_EXPECTED_STATE" in block),
            None,
        )
        if step4e_block is None:
            raise AssertionError("Step 4e state block not found")
        cls.step4e = textwrap.dedent(step4e_block)

        step6_section = cls.command_text.split("### Step 6: 提交、打版本号、推送", 1)[1]
        step6_match = re.search(
            r"\n\s*```bash\n(.*?)\n\s*```",
            step6_section,
            re.DOTALL,
        )
        if step6_match is None:
            raise AssertionError("Step 6 block not found")
        cls.step6 = textwrap.dedent(step6_match.group(1))

        steps = cls.workflow_data["jobs"]["verify-and-open-pr"]["steps"]
        cls.verify = next(
            step["run"]
            for step in steps
            if step.get("name") == "Verify untrusted candidate with trusted helper"
        )

    def _new_repo(
        self,
        root: Path,
        *,
        tracked_marker: bool = True,
        marker_bytes: bytes = OLD_MARKER,
        ignored_marker: bool = False,
    ) -> tuple[Path, str]:
        repo = root / "repo"
        repo.mkdir(parents=True)
        run_git(repo, "init", "-b", "main")
        run_git(repo, "config", "user.name", "Sandbox User")
        run_git(repo, "config", "user.email", "sandbox@example.invalid")
        (repo / ".github").mkdir()
        if tracked_marker:
            (repo / MARKER).write_bytes(marker_bytes)
        if ignored_marker:
            (repo / ".gitignore").write_text(
                ".github/upstream-main.sha\n", encoding="utf-8"
            )
        for relative, content in {
            "cli.py": "# sandbox\n",
            "skills/ppt-master/cli.py": "# sandbox\n",
            "pyproject.toml": "[project]\nname = 'sandbox'\nversion = '0.0.0'\n",
            "skills/ppt-master/pyproject.toml": "[project]\nname = 'sandbox-skill'\nversion = '0.0.0'\n",
        }.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", "base")
        return repo, run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    def _add_target(self, repo: Path, *, base: str, same_as_base: bool = False) -> str:
        if same_as_base:
            target = base
        else:
            run_git(repo, "checkout", "-b", "upstream-branch")
            (repo / "upstream.txt").write_text("upstream\n", encoding="utf-8")
            run_git(repo, "add", "upstream.txt")
            run_git(repo, "commit", "-m", "upstream")
            target = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
            run_git(repo, "checkout", "main")
        run_git(repo, "update-ref", "refs/remotes/upstream/main", target)
        return target

    def _add_conflicting_target(self, repo: Path) -> tuple[str, str]:
        (repo / "shared.txt").write_text("common\n", encoding="utf-8")
        run_git(repo, "add", "shared.txt")
        run_git(repo, "commit", "-m", "common content")
        common = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()

        run_git(repo, "checkout", "-b", "upstream-branch")
        (repo / "shared.txt").write_text("upstream\n", encoding="utf-8")
        run_git(repo, "add", "shared.txt")
        run_git(repo, "commit", "-m", "upstream conflict")
        target = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()

        run_git(repo, "checkout", "-b", "fork-branch", common)
        (repo / "shared.txt").write_text("fork\n", encoding="utf-8")
        run_git(repo, "add", "shared.txt")
        run_git(repo, "commit", "-m", "fork conflict")
        first_parent = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
        run_git(repo, "update-ref", "refs/remotes/upstream/main", target)
        return first_parent, target

    def _state_paths(self, repo: Path) -> dict[str, Path]:
        names = {
            "dir": "ppt-master-sync",
            "expected": "ppt-master-sync/expected-upstream-sha",
            "original": "ppt-master-sync/original-head",
            "marker_state": "ppt-master-sync/marker-original-state",
            "snapshot": "ppt-master-sync/marker-snapshot",
            "started": "ppt-master-sync/merge-started",
            "merge_head": "MERGE_HEAD",
        }
        paths: dict[str, Path] = {}
        for key, name in names.items():
            raw = Path(run_git(repo, "rev-parse", "--git-path", name).stdout.decode().strip())
            paths[key] = raw if raw.is_absolute() else repo / raw
        return paths

    def _run_step1(self, repo: Path, target: str, *, actions: bool = False) -> None:
        env = {"GITHUB_ACTIONS": "true" if actions else "false"}
        if actions:
            env["EXPECTED_UPSTREAM_SHA"] = target
        result = run_bash(self.step1, repo, env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _run_step2(
        self,
        repo: Path,
        *,
        target: str | None = None,
        wrapper: str = "",
        foreign_target: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {"GITHUB_ACTIONS": "false"}
        if target is not None:
            env["EXPECTED_UPSTREAM_SHA"] = target
        if foreign_target is not None:
            env["FOREIGN_TARGET"] = foreign_target
        return run_bash(wrapper + self.step2, repo, env)

    def _assert_state_absent(self, repo: Path) -> None:
        self.assertFalse(self._state_paths(repo)["dir"].exists())

    def _assert_no_merge(self, repo: Path) -> None:
        result = run_git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
        self.assertNotEqual(result.returncode, 0)

    def _assert_clean(self, repo: Path) -> None:
        status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
        self.assertEqual(status.stdout, b"")

    def _marker_bytes(self, repo: Path) -> bytes | None:
        path = repo / MARKER
        return path.read_bytes() if path.exists() else None

    def _index_marker_bytes(self, repo: Path) -> bytes | None:
        result = run_git(repo, "show", f":{MARKER}", check=False)
        return result.stdout if result.returncode == 0 else None

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_successful_two_parent_merge_cleans_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            result = self._run_step2(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_bash(self.step4e, repo, {"GITHUB_ACTIONS": "false"})
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_bash(self.step6, repo, {"GITHUB_ACTIONS": "false"})
            self.assertEqual(result.returncode, 0, result.stderr)
            parents = run_git(repo, "show", "-s", "--format=%P", "HEAD").stdout.decode().split()
            self.assertEqual(len(parents), 2)
            self.assertEqual(parents, [base, target])
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self._assert_clean(repo)
            self.assertEqual(self._marker_bytes(repo), f"{target}\n".encode())

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_owned_conflict_can_be_resolved_into_a_two_parent_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _ = self._new_repo(Path(temporary))
            first_parent, target = self._add_conflicting_target(repo)
            self._run_step1(repo, target)

            result = self._run_step2(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self._state_paths(repo)["merge_head"].exists())
            self.assertNotEqual(run_git(repo, "ls-files", "-u").stdout, b"")
            self.assertEqual(self._marker_bytes(repo), f"{target}\n".encode())

            (repo / "shared.txt").write_text("merged\n", encoding="utf-8")
            run_git(repo, "add", "shared.txt")
            result = run_bash(self.step4e, repo, {"GITHUB_ACTIONS": "false"})
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_bash(self.step6, repo, {"GITHUB_ACTIONS": "false"})
            self.assertEqual(result.returncode, 0, result.stderr)
            parents = run_git(repo, "show", "-s", "--format=%P", "HEAD").stdout.decode().split()
            self.assertEqual(parents, [first_parent, target])
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self._assert_clean(repo)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_unresolved_conflict_is_rejected_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _ = self._new_repo(Path(temporary))
            _, target = self._add_conflicting_target(repo)
            self._run_step1(repo, target)

            result = self._run_step2(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(run_git(repo, "ls-files", "-u").stdout, b"")

            result = run_bash(self.step4e, repo, {"GITHUB_ACTIONS": "false"})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unresolved merge entries", result.stderr)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_non_conflict_merge_error_fails_and_cleans_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            wrapper = """
git() {
  if [ "$1" = "merge" ] && [ "$2" = "--no-ff" ]; then
    return 2
  fi
  command git "$@"
}
"""
            result = self._run_step2(repo, wrapper=wrapper)
            self.assertNotEqual(result.returncode, 0)
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self.assertEqual(self._marker_bytes(repo), OLD_MARKER)
            self._assert_clean(repo)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_already_up_to_date_fails_and_cleans_without_marker_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            self._add_target(repo, base=base, same_as_base=True)
            self._run_step1(repo, base)
            result = self._run_step2(repo, target=base)
            self.assertNotEqual(result.returncode, 0)
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self.assertEqual(self._marker_bytes(repo), OLD_MARKER)
            self._assert_clean(repo)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_foreign_target_merge_is_not_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            foreign = "f" * 40
            self._run_step1(repo, target)
            wrapper = """
git() {
  if [ "$1" = "merge" ] && [ "$2" = "--no-ff" ]; then
    command git "$@"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      path=$(command git rev-parse --git-path MERGE_HEAD)
      command printf '%s\\n' "$FOREIGN_TARGET" > "$path"
    fi
    return "$rc"
  fi
  command git "$@"
}
"""
            result = self._run_step2(
                repo,
                target=target,
                wrapper=wrapper,
                foreign_target=foreign,
            )
            self.assertNotEqual(result.returncode, 0)
            paths = self._state_paths(repo)
            self.assertTrue(paths["dir"].exists())
            self.assertEqual(paths["merge_head"].read_text(encoding="utf-8").strip(), foreign)
            self.assertEqual(self._marker_bytes(repo), OLD_MARKER)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_foreign_original_head_is_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            result = self._run_step2(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = self._state_paths(repo)
            paths["merge_head"].unlink()
            run_git(repo, "add", "-u")
            run_git(repo, "commit", "-m", "foreign commit")
            (repo / MARKER).write_bytes(b"foreign marker\n")
            result = run_bash(self.step4e, repo, {"GITHUB_ACTIONS": "false"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self._marker_bytes(repo), b"foreign marker\n")
            self.assertTrue(paths["dir"].exists())

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_foreign_orig_head_is_not_aborted_and_preserves_scene(self) -> None:
        for step_name, step in (("Step 4e", self.step4e), ("Step 6", self.step6)):
            with self.subTest(step=step_name):
                with tempfile.TemporaryDirectory() as temporary:
                    repo, base = self._new_repo(Path(temporary))
                    run_git(repo, "checkout", "-b", "foreign-branch")
                    (repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")
                    run_git(repo, "add", "foreign.txt")
                    run_git(repo, "commit", "-m", "foreign")
                    foreign = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
                    run_git(repo, "checkout", "main")
                    target = self._add_target(repo, base=base)
                    self._run_step1(repo, target)
                    result = self._run_step2(repo)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    paths = self._state_paths(repo)
                    run_git(repo, "update-ref", "ORIG_HEAD", foreign)

                    result = run_bash(step, repo, {"GITHUB_ACTIONS": "false"})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(paths["dir"].exists())
                    self.assertTrue(paths["merge_head"].exists())
                    self.assertEqual(paths["merge_head"].read_text(encoding="utf-8").strip(), target)
                    self.assertEqual(
                        run_git(repo, "rev-parse", "--verify", "HEAD").stdout.decode().strip(),
                        base,
                    )
                    self.assertEqual(
                        run_git(repo, "rev-parse", "--verify", "ORIG_HEAD").stdout.decode().strip(),
                        foreign,
                    )
                    self.assertEqual(self._marker_bytes(repo), f"{target}\n".encode())

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_missing_merge_head_safely_restores_marker_and_cleans_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            result = self._run_step2(repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = self._state_paths(repo)
            paths["merge_head"].unlink()
            (repo / MARKER).write_bytes(b"unsafe-looking marker\n")
            run_git(repo, "add", MARKER)
            result = run_bash(self.step4e, repo, {"GITHUB_ACTIONS": "false"})
            self.assertNotEqual(result.returncode, 0)
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self.assertEqual(self._marker_bytes(repo), OLD_MARKER)
            self.assertEqual(self._index_marker_bytes(repo), OLD_MARKER)
            self.assertEqual(
                run_git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout,
                b"A  upstream.txt\n",
            )

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_marker_write_failure_restores_tracked_marker_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker_bytes=OLD_MARKER)
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            marker_fault = """
printf() {
  merge_path=$(command git rev-parse --git-path MERGE_HEAD)
  if [ -e "$merge_path" ]; then
    return 1
  fi
  builtin printf "$@"
}
"""
            result = self._run_step2(repo, target=target, wrapper=marker_fault)
            self.assertNotEqual(result.returncode, 0)
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self.assertEqual(self._marker_bytes(repo), OLD_MARKER)
            self.assertEqual(self._index_marker_bytes(repo), OLD_MARKER)
            self._assert_clean(repo)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_git_add_failure_restores_tracked_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            add_fault = """
git() {
  if [ "$1" = "add" ]; then
    return 1
  fi
  command git "$@"
}
"""
            result = self._run_step2(repo, target=target, wrapper=add_fault)
            self.assertNotEqual(result.returncode, 0)
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self.assertEqual(self._marker_bytes(repo), OLD_MARKER)
            self.assertEqual(self._index_marker_bytes(repo), OLD_MARKER)
            self._assert_clean(repo)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_absent_marker_is_restored_to_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), tracked_marker=False)
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            marker_fault = """
printf() {
  merge_path=$(command git rev-parse --git-path MERGE_HEAD)
  if [ -e "$merge_path" ]; then
    return 1
  fi
  builtin printf "$@"
}
"""
            result = self._run_step2(repo, target=target, wrapper=marker_fault)
            self.assertNotEqual(result.returncode, 0)
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self.assertFalse((repo / MARKER).exists())
            self.assertIsNone(self._index_marker_bytes(repo))
            self._assert_clean(repo)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_ignored_preexisting_absent_marker_is_rejected_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary), tracked_marker=False, ignored_marker=True
            )
            target = self._add_target(repo, base=base)
            preexisting = b"user ignored marker\x00\r\n"
            (repo / MARKER).write_bytes(preexisting)
            self._run_step1(repo, target)
            result = self._run_step2(repo, target=target)
            self.assertNotEqual(result.returncode, 0)
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)
            self.assertEqual(self._marker_bytes(repo), preexisting)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_state_lock_rejects_concurrent_step1_without_mutating_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            paths = self._state_paths(repo)
            before = paths["expected"].read_bytes()
            result = run_bash(self.step1, repo, {"GITHUB_ACTIONS": "false"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(paths["expected"].read_bytes(), before)

    @unittest.skipUnless(BASH_AVAILABLE, "Git Bash is required for the ownership sandbox")
    def test_workflow_verify_rejects_untracked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            self.assertEqual(self._run_step2(repo).returncode, 0)
            self.assertEqual(run_bash(self.step4e, repo, {"GITHUB_ACTIONS": "false"}).returncode, 0)
            result = run_bash(self.step6, repo, {"GITHUB_ACTIONS": "false"})
            self.assertEqual(result.returncode, 0, result.stderr)
            (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            result = run_bash(
                self.verify,
                repo,
                {"BASE_SHA": base, "EXPECTED_UPSTREAM_SHA": target},
            )
            self.assertNotEqual(result.returncode, 0)

    def test_static_contract_has_exclusive_state_and_safe_abort_rules(self) -> None:
        self.assertNotIn("PRE_MERGE_HEAD", self.command_text)
        self.assertIn("mkdir -- \"$SYNC_STATE_DIR\"", self.command_text)
        self.assertIn("git rev-parse --git-path ppt-master-sync", self.command_text)
        self.assertIn("if ! git fetch upstream main; then", self.command_text)
        self.assertIn("remote.upstream.url", self.command_text)
        self.assertIn("git log HEAD..upstream/main --oneline", self.command_text)
        self.assertIn("[ -L .github/upstream-main.sha ]", self.command_text)
        self.assertNotIn("git push origin main", self.command_text)
        self.assertNotIn("zero diff", self.command_text.lower())
        self.assertIn("Refusing to abort a foreign MERGE_HEAD target", self.command_text)
        self.assertIn("CURRENT_HEAD_SHA", self.command_text)
        self.assertIn("marker-original-state", self.command_text)
        self.assertIn("marker-snapshot", self.command_text)
        self.assertIn("git merge --abort", self.command_text)
        for block in (self.step2, self.step4e, self.step6):
            self.assertIn("git rev-parse --git-path ppt-master-sync", block)
            self.assertIn("rm -rf -- \"$SYNC_STATE_DIR\"", block)
            self.assertIn("abort_owned_merge", block)

    def test_yaml_parses_and_verify_has_untracked_gate(self) -> None:
        self.assertIn("git status --porcelain=v1 --untracked-files=all", self.verify)
        self.assertIn("check_upstream_ancestry.py", self.verify)

    def test_command_fetches_fail_closed_and_uses_symlink_safe_marker_checks(self) -> None:
        self.assertIn("if ! git fetch upstream main; then", self.command_text)
        self.assertIn("https://github.com/hugohe3/ppt-master.git", self.command_text)
        self.assertIn("[ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ]", self.command_text)
        self.assertNotIn("零改动", self.command_text)
        self.assertNotIn("git push origin main", self.command_text)

    def test_dangling_symlink_has_static_contract_and_unix_runner_path(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertIn("discover_bash", source)
        self.assertIn("shutil.which(\"bash\")", source)
        self.assertIn("marker.symlink_to(\"missing-target\")", source)
        self.assertIn("symlink creation unavailable", source)
        self.assertIn("[ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ]", self.command_text)

    @unittest.skipUnless(BASH_AVAILABLE, "Bash is required for the ownership sandbox")
    def test_dangling_marker_symlink_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), tracked_marker=False)
            marker = repo / MARKER
            try:
                marker.symlink_to("missing-target")
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            target = self._add_target(repo, base=base)
            self._run_step1(repo, target)
            result = self._run_step2(repo, target=target)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(marker.is_symlink())
            self._assert_no_merge(repo)
            self._assert_state_absent(repo)


if __name__ == "__main__":
    unittest.main()
