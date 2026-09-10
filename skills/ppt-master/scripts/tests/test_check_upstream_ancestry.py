from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
HELPER = ROOT / ".github" / "scripts" / "check_upstream_ancestry.py"
MARKER = ".github/upstream-main.sha"
OLD_TARGET = "1" * 40


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
    ) -> tuple[Path, str]:
        repo = root / "repo"
        repo.mkdir()
        run_git(repo, "init", "-b", "main")
        run_git(repo, "config", "user.name", "Sandbox User")
        run_git(repo, "config", "user.email", "sandbox@example.invalid")
        (repo / ".github").mkdir()
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        if marker is not None:
            (repo / MARKER).write_bytes(marker)
        for relative, content in (extra_files or {}).items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", "base")
        return repo, git_output(repo, "rev-parse", "HEAD")

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
            "skills/ppt-master/scripts/auto_fix_uvx.py",
            "skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py",
            "skills/ppt-master/scripts/tests/test_sync_upstream_ownership.py",
            "skills/ppt-master/scripts/tests/test_sync_upstream_workflow.py",
        )
        for relative in protected_files:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    repo, base = self._new_repo(
                        Path(temporary),
                        extra_files={relative: b"trusted base\n"},
                    )
                    path = repo / relative
                    path.write_bytes(b"candidate override\n")
                    run_git(repo, "add", relative)
                    run_git(repo, "commit", "-m", "candidate gate change")
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
            repo, base = self._new_repo(Path(temporary))
            run_git(repo, "checkout", "-b", "upstream-branch")
            target = self._commit_file(repo, "upstream.txt", "upstream\n", "upstream")
            self._set_upstream(repo, target)
            head = self._commit_file(repo, str(MARKER), f"{target}\n", "record target")
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
