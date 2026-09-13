from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[4]
HELPER = ROOT / ".github" / "scripts" / "check_upstream_ancestry.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check-upstream-ancestry.yml"
MARKER = ".github/upstream-main.sha"
OVERLAY_POLICY = ".github/upstream-overlay-paths.txt"
OLD_TARGET = "1" * 40


def load_helper_module():
    spec = importlib.util.spec_from_file_location("ancestry_under_test", HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load check_upstream_ancestry.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
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


def git_output(repo: Path, *args: str) -> str:
    return run_git(repo, *args).stdout.strip()


class UpstreamAncestryHelperTests(unittest.TestCase):
    def _new_repo(
        self,
        root: Path,
        *,
        marker: bytes | None = None,
        extra_files: dict[str, bytes] | None = None,
        overlay_policy: str | None = None,
        include_policy: bool = True,
    ) -> tuple[Path, str]:
        repo = root / "repo"
        repo.mkdir()
        run_git(repo, "init", "-b", "main")
        run_git(repo, "config", "user.name", "Sandbox User")
        run_git(repo, "config", "user.email", "sandbox@example.invalid")
        run_git(repo, "config", "core.fileMode", "false")
        (repo / ".github").mkdir()
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        if include_policy:
            policy_text = "# synthetic protected overlay policy\n" if overlay_policy is None else overlay_policy
            (repo / OVERLAY_POLICY).write_text(policy_text, encoding="utf-8")
        if marker is not None:
            (repo / MARKER).write_bytes(marker)
        for relative, content in (extra_files or {}).items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", "base")
        return repo, git_output(repo, "rev-parse", "HEAD")

    def _build_sync_merge(
        self,
        repo: Path,
        base: str,
        target: str,
        *,
        strategy_ours: bool = False,
        resolve: Callable[[], None] | None = None,
        message: str = "sync merge",
    ) -> str:
        run_git(repo, "checkout", "--", ".")
        run_git(repo, "checkout", "-b", "sync", base)
        merge_args = ["merge", "--no-ff", "--no-commit"]
        if strategy_ours:
            merge_args.extend(["-s", "ours"])
        merge_args.append(target)
        result = run_git(repo, *merge_args, check=False)
        if resolve is None:
            if result.returncode != 0:
                raise AssertionError(f"git {' '.join(merge_args)} failed: {result.stderr}")
        elif result.returncode not in (0, 1):
            raise AssertionError(f"git {' '.join(merge_args)} failed: {result.stderr}")
        else:
            if resolve is not None:
                resolve()
            unresolved = run_git(repo, "ls-files", "-u").stdout.strip()
            if unresolved:
                raise AssertionError(f"sync merge resolution left unresolved entries: {unresolved}")
        (repo / MARKER).write_text(f"{target}\n", encoding="ascii")
        run_git(repo, "add", MARKER)
        run_git(repo, "commit", "-m", message)
        return git_output(repo, "rev-parse", "HEAD")

    def _commit_file(self, repo: Path, relative: str, content: str, message: str) -> str:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        run_git(repo, "add", relative)
        run_git(repo, "commit", "-m", message)
        return git_output(repo, "rev-parse", "HEAD")

    def _set_upstream(self, repo: Path, sha: str) -> None:
        run_git(repo, "update-ref", "refs/remotes/upstream/main", sha)

    def _run_check(
        self,
        repo: Path,
        *,
        base: str | None,
        head: str,
        cwd: Path | None = None,
        expected_target: str | None = None,
        require_version_bump: bool = False,
        allow_protected_changes: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        args = [
            sys.executable,
            str(HELPER),
            "--repo",
            str(repo),
            "--head-sha",
            head,
            "--upstream-ref",
            "upstream/main",
        ]
        if base is not None:
            args.extend(["--base-sha", base])
        if expected_target is not None:
            args.extend(["--expected-target-sha", expected_target])
        if require_version_bump:
            args.append("--require-version-bump")
        if allow_protected_changes:
            args.append("--allow-protected-changes")
        return subprocess.run(args, cwd=cwd or repo, text=True, capture_output=True, check=False)

    def _set_index_mode(self, repo: Path, mode: str, content: bytes) -> None:
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=content,
            capture_output=True,
            check=True,
        ).stdout.decode().strip()
        run_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{MARKER}")

    def test_both_absent_marker_skips_as_ordinary_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            head = self._commit_file(repo, "ordinary.txt", "ordinary\n", "ordinary change")
            result = self._run_check(repo, base=base, head=head)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skipped", result.stdout)

    def test_sync_mode_rejects_missing_marker_instead_of_ordinary_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            head = self._commit_file(repo, "ordinary.txt", "ordinary\n", "ordinary change")
            result = self._run_check(
                repo,
                base=base,
                head=head,
                expected_target="a" * 40,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("skipped", result.stdout)

    def test_sync_mode_rejects_unchanged_marker_instead_of_ordinary_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=b"not a target\n")
            head = self._commit_file(repo, "ordinary.txt", "ordinary\n", "ordinary change")
            result = self._run_check(
                repo,
                base=base,
                head=head,
                require_version_bump=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("unchanged", result.stdout)

    def test_added_empty_marker_fails_instead_of_skipping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            (repo / MARKER).write_bytes(b"")
            run_git(repo, "add", MARKER)
            run_git(repo, "commit", "-m", "empty marker")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("marker content", result.stderr)

    def test_explicit_empty_base_sha_fails_instead_of_downgrading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _ = self._new_repo(Path(temporary))
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base="", head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("base-sha", result.stderr)

    def test_symlink_marker_fails_without_echoing_link_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            secret_link_target = "secret-marker-target"
            self._set_index_mode(repo, "120000", secret_link_target.encode())
            run_git(repo, "commit", "-m", "symlink marker")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("regular 100644 blob", result.stderr)
            self.assertNotIn(secret_link_target, result.stdout + result.stderr)

    def test_deleted_head_marker_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "rm", MARKER)
            run_git(repo, "commit", "-m", "delete marker")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("deleted", result.stderr)

    def test_present_base_marker_must_also_be_a_regular_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, _ = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            self._set_index_mode(repo, "100755", f"{OLD_TARGET}\n".encode())
            run_git(repo, "commit", "-m", "executable base marker")
            base = git_output(repo, "rev-parse", "HEAD")
            run_git(repo, "rm", MARKER)
            run_git(repo, "commit", "-m", "delete executable marker")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("regular 100644 blob", result.stderr)

    def test_unchanged_regular_blob_skips_without_validating_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=b"not a target\n")
            head = self._commit_file(repo, "ordinary.txt", "ordinary\n", "ordinary change")
            result = self._run_check(repo, base=base, head=head)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("unchanged", result.stdout)

    def test_marker_requires_exact_lowercase_sha_with_one_lf(self) -> None:
        invalid_contents = (
            b"1" * 40,
            b"1" * 40 + b"\r\n",
            b"1" * 40 + b"\nextra\n",
            b"",
        )
        for content in invalid_contents:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as temporary:
                    repo, base = self._new_repo(Path(temporary))
                    self._set_index_mode(repo, "100644", content)
                    run_git(repo, "commit", "-m", "invalid marker")
                    head = git_output(repo, "rev-parse", "HEAD")
                    result = self._run_check(repo, base=base, head=head)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("marker content", result.stderr)
                    if content:
                        self.assertNotIn(content.decode("ascii", errors="ignore"), result.stderr)

    def test_candidate_changes_to_protected_gate_files_fail_closed(self) -> None:
        protected_files = (
            ".github/scripts/check_upstream_ancestry.py",
            ".github/scripts/check_sync_candidate.py",
            ".github/scripts/yaml.py",
            ".github/upstream-overlay-paths.txt",
            ".github/workflows/sync-upstream.yml",
            ".github/workflows/check-upstream-ancestry.yml",
            ".github/workflows/check-uvx-migration.yml",
            ".github/workflows/auto-tag.yml",
            ".github/workflows/publish-pypi.yml",
            ".opencode/command/sync-upstream.md",
            "skills/ppt-master/scripts/check_cli_sync.py",
            "skills/ppt-master/scripts/check_deps_sync.py",
            "skills/ppt-master/scripts/check_uvx_migration.py",
            "skills/ppt-master/scripts/attribution_guard.py",
            "skills/ppt-master/scripts/console_encoding.py",
            "skills/ppt-master/scripts/workflow_transcript.py",
            "skills/ppt-master/scripts/auto_fix_uvx.py",
            "cli.py",
            "skills/ppt-master/cli.py",
            "skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py",
            "skills/ppt-master/scripts/tests/test_sync_upstream_ownership.py",
            "skills/ppt-master/scripts/tests/test_sync_candidate.py",
            "skills/ppt-master/scripts/tests/test_sync_upstream_workflow.py",
            ".github/pull.yml",
        )
        for relative in protected_files:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    repo, base = self._new_repo(
                        Path(temporary),
                        extra_files={relative: b"trusted base\n"},
                    )
                    path = repo / relative
                    content = b"raise SystemExit(0)\n" if relative.endswith(("yaml.py", "console_encoding.py")) else b"candidate override\n"
                    path.write_bytes(content)
                    run_git(repo, "add", relative)
                    run_git(repo, "commit", "-m", "candidate gate change")
                    head = git_output(repo, "rev-parse", "HEAD")
                    result = self._run_check(repo, base=base, head=head)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("protected", result.stderr)

    def test_maintainer_approval_allows_protected_change_without_skipping_other_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                extra_files={".github/workflows/auto-tag.yml": b"trusted base\n"},
            )
            path = repo / ".github" / "workflows" / "auto-tag.yml"
            path.write_text("approved maintenance\n", encoding="utf-8")
            run_git(repo, "add", str(path.relative_to(repo)))
            run_git(repo, "commit", "-m", "maintain trusted gate")
            head = git_output(repo, "rev-parse", "HEAD")

            rejected = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(rejected.returncode, 0)
            approved = self._run_check(
                repo,
                base=base,
                head=head,
                allow_protected_changes=True,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)

    def test_workflow_fetches_public_pr_object_without_bearer_header_and_audits_label(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("http.extraheader", text)
        self.assertNotIn("GITHUB_TOKEN", text)
        self.assertIn('fetch --no-tags origin "refs/pull/${PR_NUMBER}/head"', text)
        self.assertIn("ci-maintenance-approved", text)
        self.assertIn("--allow-protected-changes", text)
        self.assertIn("labeled", text)
        self.assertIn("unlabeled", text)
        import yaml

        data = yaml.safe_load(text)
        self.assertEqual(set(data["jobs"]), {"ancestry-gate"})

    def test_new_unlisted_workflow_is_protected_by_directory_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary))
            relative = ".github/workflows/new-unlisted-gate.yml"
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("name: tampered\n", encoding="utf-8")
            run_git(repo, "add", relative)
            run_git(repo, "commit", "-m", "add unlisted workflow")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("protected", result.stderr)

    def test_changed_marker_requires_strict_two_parent_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            run_git(repo, "checkout", "-b", "sync", base)
            run_git(repo, "merge", "--no-ff", "--no-commit", target)
            (repo / MARKER).write_text(f"{target}\n", encoding="ascii")
            run_git(repo, "add", MARKER)
            run_git(repo, "commit", "-m", "sync merge")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("two-parent merge", result.stdout)
            rejected = self._run_check(repo, base=base, head=head, expected_target="0" * 40)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("immutable expected target", rejected.stderr)

    def test_single_parent_with_valid_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            run_git(repo, "checkout", "-b", "sync", target)
            head = self._commit_file(repo, str(MARKER), f"{target}\n", "single parent")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("two-parent merge", result.stderr)

    def test_single_head_gate_accepts_and_rejects_target_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target)
            accepted = self._run_check(repo, base=None, head=head, cwd=ROOT)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            run_git(repo, "checkout", "-b", "foreign-target", head)
            (repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")
            run_git(repo, "add", "foreign.txt")
            run_git(repo, "commit", "-m", "foreign target")
            rejected_target = git_output(repo, "rev-parse", "HEAD")
            (repo / MARKER).write_text(f"{rejected_target}\n", encoding="ascii")
            run_git(repo, "add", MARKER)
            run_git(repo, "commit", "-m", "record foreign target")
            rejected_head = git_output(repo, "rev-parse", "HEAD")
            rejected = self._run_check(repo, base=None, head=rejected_head)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("upstream/main history", rejected.stderr)

    def test_trusted_object_store_verifies_head_without_candidate_base_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate, base = self._new_repo(root, marker=f"{OLD_TARGET}\n".encode())
            run_git(candidate, "checkout", "-b", "upstream-branch")
            target = self._commit_file(candidate, "upstream.txt", "upstream\n", "upstream")
            run_git(candidate, "checkout", "-b", "sync", base)
            run_git(candidate, "merge", "--no-ff", "--no-commit", target)
            (candidate / MARKER).write_bytes(f"{target}\n".encode("ascii"))
            run_git(candidate, "add", MARKER)
            run_git(candidate, "commit", "-m", "sync merge")
            head = git_output(candidate, "rev-parse", "HEAD")
            run_git(candidate, "update-ref", "refs/remotes/upstream/main", target)

            trusted = root / "trusted"
            trusted.mkdir()
            run_git(trusted, "init", "-b", "main")
            run_git(trusted, "config", "user.name", "Trusted User")
            run_git(trusted, "config", "user.email", "trusted@example.invalid")
            run_git(trusted, "remote", "add", "origin", str(candidate))
            run_git(trusted, "fetch", "origin", "refs/heads/main:refs/remotes/origin/base")
            run_git(trusted, "checkout", "-b", "main", "refs/remotes/origin/base")

            run_git(candidate, "update-ref", "-d", "refs/heads/main")
            self.assertNotEqual(
                run_git(candidate, "rev-parse", "--verify", "refs/heads/main", check=False).returncode,
                0,
            )
            run_git(trusted, "fetch", "origin", "refs/heads/sync", check=True)
            self.assertEqual(git_output(trusted, "rev-parse", "FETCH_HEAD"), head)
            run_git(trusted, "update-ref", "refs/remotes/upstream/main", target)

            result = self._run_check(trusted, base=base, head=head, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("two-parent merge", result.stdout)

    def test_manifest_validator_accepts_exact_immutable_sha_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            values = {
                "base_sha": "a" * 40,
                "target_sha": "b" * 40,
                "verified_sha": "c" * 40,
            }
            path.write_text(json.dumps(values), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(HELPER),
                    "--manifest",
                    str(path),
                    "--expected-base-sha",
                    values["base_sha"],
                    "--expected-target-sha",
                    values["target_sha"],
                    "--expected-verified-sha",
                    values["verified_sha"],
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_candidate_requires_matching_forward_package_version_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={
                    "pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                    "skills/ppt-master/pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                },
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            run_git(repo, "checkout", "-b", "sync", base)
            run_git(repo, "merge", "--no-ff", "--no-commit", target)
            (repo / MARKER).write_text(f"{target}\n", encoding="ascii")
            run_git(repo, "add", MARKER)
            run_git(repo, "commit", "-m", "sync merge")
            for relative in ("pyproject.toml", "skills/ppt-master/pyproject.toml"):
                (repo / relative).write_text('[project]\nversion = "1.2.4"\n', encoding="utf-8")
                run_git(repo, "add", relative)
            run_git(repo, "commit", "-m", "bump version")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head, require_version_bump=True)
            self.assertEqual(result.returncode, 0, result.stderr)

            bad_root = Path(temporary) / "bad"
            bad_root.mkdir()
            bad_repo, bad_base = self._new_repo(
                bad_root,
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={
                    "pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                    "skills/ppt-master/pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                },
            )
            run_git(bad_repo, "checkout", "-b", "upstream-branch")
            bad_target = self._commit_file(bad_repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(bad_repo, bad_target)
            run_git(bad_repo, "checkout", "-b", "sync", bad_base)
            run_git(bad_repo, "merge", "--no-ff", "--no-commit", bad_target)
            (bad_repo / MARKER).write_text(f"{bad_target}\n", encoding="ascii")
            run_git(bad_repo, "add", MARKER)
            run_git(bad_repo, "commit", "-m", "sync merge")
            bad_head = git_output(bad_repo, "rev-parse", "HEAD")
            bad_result = subprocess.run(
                [
                    sys.executable,
                    str(HELPER),
                    "--repo",
                    str(bad_repo),
                    "--base-sha",
                    bad_base,
                    "--head-sha",
                    bad_head,
                    "--upstream-ref",
                    "upstream/main",
                    "--require-version-bump",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(bad_result.returncode, 0)
            self.assertIn("bump", bad_result.stderr)

    def test_ours_merge_cannot_bypass_upstream_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, strategy_ours=True)
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-overlay", result.stderr)

    def test_non_overlay_rollback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")

            def rollback() -> None:
                (repo / "upstream.txt").write_text("rolled back\n", encoding="utf-8")
                run_git(repo, "add", "upstream.txt")

            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, resolve=rollback)
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-overlay", result.stderr)

    def test_overlay_resolution_requires_an_exact_reviewed_entry(self) -> None:
        policy = "upstream.txt merge reviewed fork adaptation\n"

        def resolve_adaptation(repo: Path) -> None:
            (repo / "upstream.txt").write_text("fork adaptation\n", encoding="utf-8")
            run_git(repo, "add", "upstream.txt")

        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                overlay_policy=policy,
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, resolve=lambda: resolve_adaptation(repo))
            result = self._run_check(repo, base=base, head=head)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("content boundary", result.stdout)

        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, resolve=lambda: resolve_adaptation(repo))
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-overlay", result.stderr)

    def test_diverged_path_accepts_merge_without_static_policy_but_not_fork_rollback(self) -> None:
        for resolution, accepted in (("merged\n", True), ("fork\n", False)):
            with self.subTest(resolution=resolution):
                with tempfile.TemporaryDirectory() as temporary:
                    repo, common = self._new_repo(
                        Path(temporary),
                        marker=f"{OLD_TARGET}\n".encode(),
                        extra_files={"shared.txt": b"common\n"},
                    )
                    run_git(repo, "checkout", "-b", "upstream-branch")
                    target = self._commit_file(repo, "shared.txt", "upstream\n", "upstream")
                    run_git(repo, "checkout", "-b", "fork-branch", common)
                    first_parent = self._commit_file(repo, "shared.txt", "fork\n", "fork")

                    def resolve() -> None:
                        (repo / "shared.txt").write_text(resolution, encoding="utf-8")
                        run_git(repo, "add", "shared.txt")

                    self._set_upstream(repo, target)
                    head = self._build_sync_merge(
                        repo,
                        first_parent,
                        target,
                        resolve=resolve,
                    )
                    result = self._run_check(repo, base=first_parent, head=head)
                    if accepted:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("diverged path", result.stderr)

    def test_retain_base_overlay_requires_the_explicit_mode(self) -> None:
        def keep_base(repo: Path) -> None:
            (repo / "upstream.txt").write_text("base\n", encoding="utf-8")
            run_git(repo, "add", "upstream.txt")

        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={"upstream.txt": b"base\n"},
                overlay_policy="upstream.txt retain-base fork keeps its reviewed content\n",
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, resolve=lambda: keep_base(repo))
            result = self._run_check(repo, base=base, head=head)
            self.assertEqual(result.returncode, 0, result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={"upstream.txt": b"base\n"},
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, resolve=lambda: keep_base(repo))
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)

    def test_overlay_policy_parser_rejects_broad_or_malformed_entries(self) -> None:
        module = load_helper_module()
        valid = module.parse_overlay_policy(
            "# comment\nupstream.txt retain-base reviewed fork content\n"
        )
        self.assertEqual(valid, {"upstream.txt": "retain-base"})
        for text in (
            "skills/ppt-master/scripts/ merge broad directory\n",
            "docs/*.md merge glob\n",
            "../outside.txt merge escape\n",
            "upstream.txt unknown-mode reason\n",
            "upstream.txt merge\n",
            "upstream.txt merge reason\nupstream.txt merge duplicate\n",
            "upstream\tfile merge reason\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(module.CheckError):
                    module.parse_overlay_policy(text)

    def _write_object(self, repo: Path, content: str) -> str:
        return subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=content.encode("utf-8"),
            capture_output=True,
            check=True,
        ).stdout.decode().strip()

    def _build_tree_target(self, repo: Path, name: str) -> str:
        if name == "added":
            return self._commit_file(repo, "added.txt", "added\n", "upstream add")
        if name == "deleted":
            run_git(repo, "rm", "gone.txt")
            run_git(repo, "commit", "-m", "upstream delete")
            return git_output(repo, "rev-parse", "HEAD")
        if name == "mode":
            sha = self._write_object(repo, "mode\n")
            run_git(repo, "update-index", "--add", "--cacheinfo", f"100755,{sha},mode.sh")
            run_git(repo, "commit", "-m", "upstream chmod")
            return git_output(repo, "rev-parse", "HEAD")
        sha = self._write_object(repo, "target/path\n")
        run_git(repo, "update-index", "--add", "--cacheinfo", f"120000,{sha},link.txt")
        (repo / "link.txt").write_text("target/path\n", encoding="utf-8")
        run_git(repo, "commit", "-m", "upstream symlink")
        return git_output(repo, "rev-parse", "HEAD")

    def _mutate_tree_entry(self, repo: Path, name: str) -> None:
        if name == "added":
            (repo / "added.txt").unlink()
        elif name == "deleted":
            (repo / "gone.txt").write_text("gone\n", encoding="utf-8")
        elif name == "mode":
            sha = self._write_object(repo, "mode\n")
            run_git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},mode.sh")
        else:
            sha = self._write_object(repo, "target/path\n")
            run_git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},link.txt")
        run_git(repo, "add", "-A")

    def _commit_post_merge(self, repo: Path, mutate: Callable[[], object]) -> str:
        mutate()
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-m", "post-merge change")
        return git_output(repo, "rev-parse", "HEAD")

    def test_added_deleted_mode_and_symlink_paths_must_match_upstream(self) -> None:
        base_files = {
            "deleted": {"gone.txt": b"gone\n"},
            "mode": {"mode.sh": b"mode\n"},
            "symlink": {"link.txt": b"old-target\n"},
        }
        for name in ("added", "deleted", "mode", "symlink"):
            with self.subTest(name=name, outcome="accepted"), tempfile.TemporaryDirectory() as temporary:
                repo, base = self._new_repo(
                    Path(temporary),
                    marker=f"{OLD_TARGET}\n".encode(),
                    extra_files=base_files.get(name, {}),
                )
                run_git(repo, "checkout", "-b", "upstream-branch")
                target = self._build_tree_target(repo, name)
                self._set_upstream(repo, target)
                head = self._build_sync_merge(repo, base, target)
                accepted = self._run_check(repo, base=base, head=head)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)

            with self.subTest(name=name, outcome="rejected"), tempfile.TemporaryDirectory() as temporary:
                repo, base = self._new_repo(
                    Path(temporary),
                    marker=f"{OLD_TARGET}\n".encode(),
                    extra_files=base_files.get(name, {}),
                )
                run_git(repo, "checkout", "-b", "upstream-branch")
                target = self._build_tree_target(repo, name)
                self._set_upstream(repo, target)
                head = self._build_sync_merge(
                    repo, base, target, resolve=lambda: self._mutate_tree_entry(repo, name)
                )
                rejected = self._run_check(repo, base=base, head=head)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("non-overlay", rejected.stderr)

    def test_post_merge_version_commit_cannot_rollback_upstream_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={
                    "pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                    "skills/ppt-master/pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                },
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target)

            def mutate() -> None:
                (repo / "upstream.txt").write_text("rolled back\n", encoding="utf-8")
                for relative in ("pyproject.toml", "skills/ppt-master/pyproject.toml"):
                    (repo / relative).write_text(
                        '[project]\nversion = "1.2.4"\n', encoding="utf-8"
                    )

            head = self._commit_post_merge(repo, mutate)
            result = self._run_check(repo, base=base, head=head, require_version_bump=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("after the sync merge", result.stderr)

    def test_post_merge_overlay_rollback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                overlay_policy="upstream.txt merge reviewed adaptation\n",
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")

            def resolve_adaptation() -> None:
                (repo / "upstream.txt").write_text("fork adaptation\n", encoding="utf-8")
                run_git(repo, "add", "upstream.txt")

            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target, resolve=resolve_adaptation)

            def mutate() -> None:
                (repo / "upstream.txt").unlink()

            head = self._commit_post_merge(repo, mutate)
            result = self._run_check(repo, base=base, head=head, expected_target=target)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("after the sync merge", result.stderr)

    def test_post_merge_tree_drift_on_added_deleted_mode_and_symlink_paths(self) -> None:
        base_files = {
            "deleted": {"gone.txt": b"gone\n"},
            "mode": {"mode.sh": b"mode\n"},
            "symlink": {"link.txt": b"old-target\n"},
        }
        for name in ("added", "deleted", "mode", "symlink"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                repo, base = self._new_repo(
                    Path(temporary),
                    marker=f"{OLD_TARGET}\n".encode(),
                    extra_files=base_files.get(name, {}),
                )
                run_git(repo, "checkout", "-b", "upstream-branch")
                target = self._build_tree_target(repo, name)
                self._set_upstream(repo, target)
                self._build_sync_merge(repo, base, target)
                head = self._commit_post_merge(
                    repo, lambda: self._mutate_tree_entry(repo, name)
                )
                result = self._run_check(repo, base=base, head=head, expected_target=target)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("after the sync merge", result.stderr)

    def test_post_merge_pure_version_bump_with_locks_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={
                    "pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                    "skills/ppt-master/pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                    "uv.lock": b'version = "1.2.3"\n',
                    "skills/ppt-master/uv.lock": b'version = "1.2.3"\n',
                },
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target)

            def bump() -> None:
                for relative in ("pyproject.toml", "skills/ppt-master/pyproject.toml"):
                    (repo / relative).write_text(
                        '[project]\nversion = "1.2.4"\n', encoding="utf-8"
                    )
                for relative in ("uv.lock", "skills/ppt-master/uv.lock"):
                    (repo / relative).write_text('version = "1.2.4"\n', encoding="utf-8")

            head = self._commit_post_merge(repo, bump)
            accepted = self._run_check(repo, base=base, head=head, require_version_bump=True)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("no post-merge drift", accepted.stdout)

    def test_post_merge_unrelated_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target)

            def add_unrelated() -> None:
                (repo / "docs").mkdir()
                (repo / "docs" / "new.md").write_text("new\n", encoding="utf-8")

            head = self._commit_post_merge(repo, add_unrelated)
            result = self._run_check(repo, base=base, head=head, expected_target=target)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-version path", result.stderr)

    def test_policy_is_read_from_the_first_parent_not_candidate_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                overlay_policy="upstream.txt merge reviewed adaptation\n",
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")

            def resolve_adaptation() -> None:
                (repo / "upstream.txt").write_text("fork adaptation\n", encoding="utf-8")
                run_git(repo, "add", "upstream.txt")

            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target, resolve=resolve_adaptation)
            (repo / OVERLAY_POLICY).write_text("# changed after the merge\n", encoding="utf-8")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-m", "future policy change")
            head = git_output(repo, "rev-parse", "HEAD")
            accepted = self._run_check(repo, base=None, head=head)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_candidate_merge_policy_cannot_override_base_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                overlay_policy="upstream.txt merge reviewed adaptation\n",
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")

            def tamper_policy() -> None:
                (repo / OVERLAY_POLICY).write_text("# permissive\n", encoding="utf-8")
                run_git(repo, "add", OVERLAY_POLICY)

            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, resolve=tamper_policy)
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("protected CI gate file", result.stderr)

    def test_merge_commit_policy_tamper_is_rejected_in_head_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                overlay_policy="upstream.txt merge reviewed adaptation\n",
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")

            def tamper_policy() -> None:
                (repo / OVERLAY_POLICY).write_text("# permissive\n", encoding="utf-8")
                run_git(repo, "add", OVERLAY_POLICY)

            self._set_upstream(repo, target)
            head = self._build_sync_merge(repo, base, target, resolve=tamper_policy)
            result = self._run_check(repo, base=None, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("overlay policy", result.stderr)

    def test_protected_paths_are_scanned_per_commit_even_if_restored(self) -> None:
        module = load_helper_module()
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                overlay_policy="upstream.txt merge reviewed adaptation\n",
            )
            original_policy = (repo / OVERLAY_POLICY).read_text(encoding="utf-8")

            def tamper_policy() -> None:
                (repo / OVERLAY_POLICY).write_text("# P prime\n", encoding="utf-8")
                run_git(repo, "add", "-A")
                run_git(repo, "commit", "-m", "tamper policy")

            tamper_policy()
            (repo / OVERLAY_POLICY).write_text(original_policy, encoding="utf-8")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-m", "restore policy")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("protected CI gate file", result.stderr)
            self.assertIn("commit", result.stderr)
            self.assertNotIn(module.BOOTSTRAP_MERGE_SHA, result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={".github/scripts/check_release_gates.py": b"# original\n"},
            )
            protected = repo / ".github" / "scripts" / "check_release_gates.py"
            protected.write_text("# tampered\n", encoding="utf-8")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-m", "tamper protected gate")
            protected.write_text("# original\n", encoding="utf-8")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-m", "restore protected gate")
            head = git_output(repo, "rev-parse", "HEAD")
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("protected CI gate file", result.stderr)

    def test_post_merge_drift_applies_without_sync_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target)
            head = self._commit_post_merge(
                repo,
                lambda: (repo / "upstream.txt").write_text("rolled back\n", encoding="utf-8"),
            )
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("after the sync merge", result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                overlay_policy="upstream.txt merge reviewed adaptation\n",
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")

            def resolve_adaptation() -> None:
                (repo / "upstream.txt").write_text("fork adaptation\n", encoding="utf-8")
                run_git(repo, "add", "upstream.txt")

            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target, resolve=resolve_adaptation)
            head = self._commit_post_merge(repo, lambda: (repo / "upstream.txt").unlink())
            result = self._run_check(repo, base=base, head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("after the sync merge", result.stderr)

    def test_upstream_version_paths_allow_only_version_drift(self) -> None:
        pyproject_base = b'[project]\nname = "ppt-master"\nversion = "1.2.3"\n'
        lock_base = (
            b'version = 1\n\n[[package]]\nname = "ppt-master"\nversion = "1.2.3"\n'
            b'source = { virtual = "." }\n'
        )
        for name, path in (("pyproject", "pyproject.toml"), ("lock", "uv.lock")):
            with self.subTest(path=name, outcome="version-only"), tempfile.TemporaryDirectory() as temporary:
                repo, base = self._new_repo(
                    Path(temporary),
                    marker=f"{OLD_TARGET}\n".encode(),
                    extra_files={path: pyproject_base if name == "pyproject" else lock_base},
                )
                run_git(repo, "checkout", "-b", "upstream-branch")
                upstream_content = (
                    pyproject_base.replace(b"1.2.3", b"2.0.0")
                    if name == "pyproject"
                    else lock_base.replace(b"1.2.3", b"2.0.0")
                )
                target = self._commit_file(
                    repo, path, upstream_content.decode("utf-8"), "upstream version"
                )
                self._set_upstream(repo, target)
                self._build_sync_merge(repo, base, target)
                bumped = upstream_content.replace(b"2.0.0", b"2.0.1").decode("utf-8")
                head = self._commit_post_merge(
                    repo,
                    lambda: (repo / path).write_text(bumped, encoding="utf-8"),
                )
                accepted = self._run_check(repo, base=base, head=head)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)

            with self.subTest(path=name, outcome="extra-change"), tempfile.TemporaryDirectory() as temporary:
                repo, base = self._new_repo(
                    Path(temporary),
                    marker=f"{OLD_TARGET}\n".encode(),
                    extra_files={path: pyproject_base if name == "pyproject" else lock_base},
                )
                run_git(repo, "checkout", "-b", "upstream-branch")
                upstream_content = (
                    pyproject_base.replace(b"1.2.3", b"2.0.0")
                    if name == "pyproject"
                    else lock_base.replace(b"1.2.3", b"2.0.0")
                )
                target = self._commit_file(
                    repo, path, upstream_content.decode("utf-8"), "upstream version"
                )
                self._set_upstream(repo, target)
                self._build_sync_merge(repo, base, target)

                def drift() -> None:
                    mutated = upstream_content.replace(b"2.0.0", b"2.0.1")
                    if name == "pyproject":
                        mutated = mutated + b'description = "drift"\n'
                    else:
                        mutated = mutated + b'dependencies = []\n'
                    (repo / path).write_text(mutated.decode("utf-8"), encoding="utf-8")

                head = self._commit_post_merge(repo, drift)
                result = self._run_check(repo, base=base, head=head)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("beyond", result.stderr)

    def test_missing_policy_on_non_bootstrap_merge_fails(self) -> None:
        module = load_helper_module()
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                include_policy=False,
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            merge = self._build_sync_merge(repo, base, target)
            with self.assertRaisesRegex(module.CheckError, "overlay policy"):
                module._policy_from_first_parent(repo, merge, base, target, merge)

    def test_bootstrap_policy_fallback_requires_the_exact_pair(self) -> None:
        module = load_helper_module()
        self.assertEqual(module.BOOTSTRAP_MERGE_SHA, "1fcf7154221d1162867248458048f88d863cd80d")
        self.assertEqual(module.BOOTSTRAP_TARGET_SHA, "09ad58f0d58decc9d30799ca83374ff2604ef16b")
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                include_policy=False,
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            merge = self._build_sync_merge(repo, base, target)
            (repo / OVERLAY_POLICY).write_text("# added after the merge\n", encoding="utf-8")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-m", "add protected policy")
            fallback = git_output(repo, "rev-parse", "HEAD")
            output = io.StringIO()
            with mock.patch.object(module, "BOOTSTRAP_MERGE_SHA", merge), mock.patch.object(
                module, "BOOTSTRAP_TARGET_SHA", target
            ), contextlib.redirect_stdout(output):
                policy = module._policy_from_first_parent(repo, merge, base, target, fallback)
            self.assertEqual(policy, {})
            self.assertIn("bootstrap", output.getvalue())
            with mock.patch.object(module, "BOOTSTRAP_MERGE_SHA", merge), mock.patch.object(
                module, "BOOTSTRAP_TARGET_SHA", OLD_TARGET
            ):
                with self.assertRaisesRegex(module.CheckError, "overlay policy"):
                    module._policy_from_first_parent(repo, merge, base, target, fallback)

    def test_real_bootstrap_repair_merge_uses_the_documented_fallback(self) -> None:
        module = load_helper_module()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        _merge, first_parent = module._find_sync_merge(
            ROOT, head, module.BOOTSTRAP_TARGET_SHA
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            policy = module._policy_from_first_parent(
                ROOT,
                module.BOOTSTRAP_MERGE_SHA,
                first_parent,
                module.BOOTSTRAP_TARGET_SHA,
                head,
            )
        self.assertIn("bootstrap", output.getvalue())
        self.assertIn("skills/ppt-master/references/native-data-interface.md", policy)

    def test_version_bump_commits_still_verify_the_merge_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(
                Path(temporary),
                marker=f"{OLD_TARGET}\n".encode(),
                extra_files={
                    "pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                    "skills/ppt-master/pyproject.toml": b'[project]\nversion = "1.2.3"\n',
                },
            )
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target)
            for relative in ("pyproject.toml", "skills/ppt-master/pyproject.toml"):
                (repo / relative).write_text('[project]\nversion = "1.2.4"\n', encoding="utf-8")
                run_git(repo, "add", relative)
            run_git(repo, "commit", "-m", "bump version")
            head = git_output(repo, "rev-parse", "HEAD")
            accepted = self._run_check(repo, base=None, head=head)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("content boundary", accepted.stdout)

        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            self._build_sync_merge(repo, base, target, strategy_ours=True)
            run_git(repo, "commit", "--allow-empty", "-m", "bump placeholder")
            head = git_output(repo, "rev-parse", "HEAD")
            rejected = self._run_check(repo, base=None, head=head)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("non-overlay", rejected.stderr)

    def test_pre_existing_target_content_passes_the_content_boundary(self) -> None:
        module = load_helper_module()
        with tempfile.TemporaryDirectory() as temporary:
            repo, base = self._new_repo(Path(temporary), marker=f"{OLD_TARGET}\n".encode())
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            run_git(repo, "checkout", "main")
            run_git(repo, "merge", "--no-ff", "--no-commit", target)
            (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
            run_git(repo, "add", "merged.txt")
            run_git(repo, "commit", "-m", "already merged upstream")
            merge_commit = git_output(repo, "rev-parse", "HEAD")
            message = module._verify_content_boundary(
                repo, merge_commit, merge_commit, target, {}
            )
            self.assertIn("no upstream changes", message)
