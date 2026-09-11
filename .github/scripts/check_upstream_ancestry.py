#!/usr/bin/env python3
"""PPT Master - CI upstream ancestry checker.

Validate the recorded upstream marker from Git commit objects only.  This is
an internal GitHub Actions helper, not a user-facing PPT Master command.

Usage:
    python .github/scripts/check_upstream_ancestry.py --head-sha SHA
    python .github/scripts/check_upstream_ancestry.py --base-sha SHA --head-sha SHA

Examples:
    python .github/scripts/check_upstream_ancestry.py --head-sha "$HEAD_SHA"
    python .github/scripts/check_upstream_ancestry.py --base-sha "$BASE_SHA" --head-sha "$HEAD_SHA"

Dependencies:
    None (only uses the Python standard library and Git).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


TARGET_FILE = ".github/upstream-main.sha"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MARKER_CONTENT_RE = re.compile(rb"^[0-9a-f]{40}\n$")
PROTECTED_PATHS = frozenset(
    {
        ".github/scripts/check_upstream_ancestry.py",
        ".github/scripts/check_sync_candidate.py",
        ".github/scripts/check_release_gates.py",
        ".github/scripts/yaml.py",
        ".github/upstream-overlay-paths.txt",
        ".github/pull.yml",
        ".github/workflows/sync-upstream.yml",
        ".github/workflows/check-upstream-ancestry.yml",
        ".github/workflows/check-uvx-migration.yml",
        ".github/workflows/auto-tag.yml",
        ".github/workflows/publish-pypi.yml",
        ".opencode/command/sync-upstream.md",
        "cli.py",
        "skills/ppt-master/cli.py",
        "skills/ppt-master/scripts/check_cli_sync.py",
        "skills/ppt-master/scripts/check_deps_sync.py",
        "skills/ppt-master/scripts/check_uvx_migration.py",
        "skills/ppt-master/scripts/check_uvx_repository.py",
        "skills/ppt-master/scripts/attribution_guard.py",
        "skills/ppt-master/scripts/auto_fix_uvx.py",
        "skills/ppt-master/scripts/console_encoding.py",
        "skills/ppt-master/scripts/workflow_transcript.py",
        "skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py",
        "skills/ppt-master/scripts/tests/test_check_cli_sync.py",
        "skills/ppt-master/scripts/tests/test_release_gates.py",
        "skills/ppt-master/scripts/tests/test_release_workflows.py",
        "skills/ppt-master/scripts/tests/test_sync_upstream_ownership.py",
        "skills/ppt-master/scripts/tests/test_sync_candidate.py",
        "skills/ppt-master/scripts/tests/test_uvx_repository_scan.py",
        "skills/ppt-master/scripts/tests/test_sync_upstream_workflow.py",
    }
)
OVERLAY_POLICY_FILE = ".github/upstream-overlay-paths.txt"
OVERLAY_MODES = frozenset({"merge", "retain-base"})
PROTECTED_PATHSPECS = (".github/scripts/**", ".github/workflows/**")
MANIFEST_KEYS = frozenset({"base_sha", "target_sha", "verified_sha"})
VERSION_RE = re.compile(rb"(?m)^[ \t]*version[ \t]*=[ \t]*[\"'](\d+)\.(\d+)\.(\d+)[\"'][ \t]*$")


class CheckError(RuntimeError):
    """Represent a fail-closed ancestry check error."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    object_name: str


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CheckError("Unable to execute Git for the ancestry check") from exc


def _validate_sha(value: str, label: str) -> None:
    if not SHA_RE.fullmatch(value):
        raise CheckError(f"{label} must be a 40-character lowercase SHA")


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CheckError("The manifest contains duplicate keys")
        result[key] = value
    return result


def validate_manifest(
    manifest_path: Path,
    *,
    expected_base_sha: str,
    expected_target_sha: str,
    expected_verified_sha: str,
) -> None:
    """Validate the exact immutable manifest exchanged between sync jobs."""
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise CheckError("The sync manifest is unavailable or is a symlink")
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise CheckError("Unable to read the sync manifest") from exc
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_manifest_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CheckError("The manifest contains a non-finite JSON value")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckError("The sync manifest is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != MANIFEST_KEYS:
        raise CheckError("The sync manifest must contain exactly base_sha, target_sha, and verified_sha")

    expected = {
        "base_sha": expected_base_sha,
        "target_sha": expected_target_sha,
        "verified_sha": expected_verified_sha,
    }
    for key in sorted(MANIFEST_KEYS):
        value = parsed[key]
        if not isinstance(value, str):
            raise CheckError(f"The manifest field {key} must be a SHA string")
        _validate_sha(value, f"manifest {key}")
        _validate_sha(expected[key], f"expected {key}")
        if value != expected[key]:
            raise CheckError(f"The manifest field {key} does not match the trusted value")


def _read_project_version(repo: Path, commit_sha: str, path: str, label: str) -> tuple[int, int, int] | None:
    result = _run_git(repo, "show", f"{commit_sha}:{path}")
    if result.returncode != 0:
        return None
    matches = VERSION_RE.findall(result.stdout)
    if len(matches) != 1:
        raise CheckError(f"The {label} package version is missing or ambiguous")
    try:
        return int(matches[0][0]), int(matches[0][1]), int(matches[0][2])
    except ValueError as exc:
        raise CheckError(f"The {label} package version is invalid") from exc


def _verify_version_bump(repo: Path, base_sha: str, head_sha: str) -> None:
    paths = (
        ("pyproject.toml", "root"),
        ("skills/ppt-master/pyproject.toml", "skill"),
    )
    base_versions = [_read_project_version(repo, base_sha, path, label) for path, label in paths]
    head_versions = [_read_project_version(repo, head_sha, path, label) for path, label in paths]
    if not any(version is not None for version in base_versions + head_versions):
        return
    if any(version is None for version in base_versions + head_versions):
        raise CheckError("Both fork package manifests must carry a single semantic version")
    base_root, base_skill = base_versions
    head_root, head_skill = head_versions
    if base_root is None or base_skill is None or head_root is None or head_skill is None:
        raise CheckError("Both fork package manifests must carry a single semantic version")
    if head_root != head_skill:
        raise CheckError("The two fork package versions must match in the sync candidate")
    if head_root <= base_root or head_skill <= base_skill:
        raise CheckError("The sync candidate must bump both fork package versions")


def _require_commit(repo: Path, value: str, label: str) -> None:
    _validate_sha(value, label)
    result = _run_git(repo, "cat-file", "-e", f"{value}^{{commit}}")
    if result.returncode != 0:
        raise CheckError(f"{label} commit object is unavailable")


def _read_path_entry(repo: Path, commit_sha: str, path: str, label: str) -> TreeEntry | None:
    """Read one exact tree entry from a commit object, or None when absent."""
    result = _run_git(repo, "ls-tree", "-z", "--full-tree", commit_sha, "--", path)
    if result.returncode != 0:
        raise CheckError(f"Unable to inspect the {label} commit object")

    records = [record for record in result.stdout.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1:
        raise CheckError(f"The {label} path is ambiguous in the commit object")

    try:
        metadata, record_path = records[0].split(b"\t", 1)
        mode, object_type, object_name = metadata.decode("ascii").split()
        path_text = record_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise CheckError(f"The {label} tree entry is malformed") from exc
    if path_text != path:
        raise CheckError(f"The {label} tree entry is malformed")
    return TreeEntry(mode, object_type, object_name)


def _read_tree_entry(repo: Path, commit_sha: str, label: str) -> TreeEntry | None:
    return _read_path_entry(repo, commit_sha, TARGET_FILE, label)


def parse_overlay_policy(text: str) -> dict[str, str]:
    """Parse the reviewed exact overlay policy for upstream sync content."""
    policy: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(None, 2)
        if len(fields) != 3:
            raise CheckError(
                f"The overlay policy line {number} must contain an exact path, mode, and reason"
            )
        path, mode, reason = fields
        if mode not in OVERLAY_MODES:
            raise CheckError(f"The overlay policy mode on line {number} is not approved")
        if (
            not path
            or path.startswith("/")
            or path.startswith("./")
            or path.endswith("/")
            or "\\" in path
            or any(character in path for character in "*?[]")
            or ".." in Path(path).parts
            or not reason.strip()
        ):
            raise CheckError(f"The overlay policy entry on line {number} is not an exact file path")
        if path in policy:
            raise CheckError(f"The overlay policy path on line {number} is duplicated")
        policy[path] = mode
    return policy


def verify_overlay_policy(repo: Path, commit_sha: str) -> dict[str, str]:
    """Require the protected overlay policy at one commit object and parse it."""
    _require_commit(repo, commit_sha, "overlay policy")
    entry = _read_path_entry(repo, commit_sha, OVERLAY_POLICY_FILE, "overlay policy")
    if entry is None:
        raise CheckError("The protected upstream overlay policy is missing")
    _require_regular_blob(entry, "overlay policy")
    result = _run_git(repo, "show", f"{commit_sha}:{OVERLAY_POLICY_FILE}")
    if result.returncode != 0:
        raise CheckError("Unable to read the protected upstream overlay policy")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckError("The protected upstream overlay policy is not valid UTF-8") from exc
    return parse_overlay_policy(text)


def _read_overlay_policy(repo: Path, commit_sha: str) -> dict[str, str]:
    return verify_overlay_policy(repo, commit_sha)


def _require_regular_blob(entry: TreeEntry, label: str) -> None:
    if entry.mode != "100644" or entry.object_type != "blob":
        raise CheckError(f"The {label} marker entry must be a regular 100644 blob")


def _read_marker(repo: Path, commit_sha: str, label: str) -> str:
    result = _run_git(repo, "show", f"{commit_sha}:{TARGET_FILE}")
    if result.returncode != 0 or not MARKER_CONTENT_RE.fullmatch(result.stdout):
        raise CheckError(
            f"The {label} marker content is invalid; expected one lowercase 40-character SHA"
        )
    return result.stdout.rstrip(b"\n").decode("ascii")


def _is_ancestor(repo: Path, ancestor: str, descendant: str, message: str) -> None:
    result = _run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise CheckError(message)
    raise CheckError("Git could not evaluate the required ancestry relationship")


def _verify_target_ancestry(repo: Path, target: str, head_sha: str, upstream_ref: str) -> None:
    _is_ancestor(repo, target, upstream_ref, "The recorded upstream target is not in upstream/main history")
    _is_ancestor(repo, target, head_sha, "The recorded upstream target is not an ancestor of the checked commit")


def _verify_strict_merge(repo: Path, base_sha: str, head_sha: str, target: str) -> str:
    result = _run_git(repo, "rev-list", "--merges", "--parents", f"{base_sha}..{head_sha}")
    if result.returncode != 0:
        raise CheckError("Unable to enumerate merge commits after the PR base")

    matches = []
    for line in result.stdout.decode("ascii", errors="replace").splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] == base_sha and fields[2] == target:
            matches.append(fields[0])
    if len(matches) != 1:
        raise CheckError(
            "Expected exactly one two-parent merge with the base as first parent "
            "and the recorded upstream target as second parent"
        )
    return matches[0]


def _tree_snapshot(repo: Path, commit_sha: str, label: str) -> dict[str, TreeEntry]:
    """Return every blob/tree entry in one commit tree, keyed by exact path."""
    result = _run_git(repo, "ls-tree", "-r", "-z", "--full-tree", commit_sha)
    if result.returncode != 0:
        raise CheckError(f"Unable to inspect the {label} tree for content verification")
    snapshot: dict[str, TreeEntry] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path = record.split(b"\t", 1)
            mode, object_type, object_name = metadata.decode("ascii").split()
            decoded = path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise CheckError(f"The {label} tree contains a malformed entry") from exc
        snapshot[decoded] = TreeEntry(mode, object_type, object_name)
    return snapshot


def _changed_paths(repo: Path, base_sha: str, target: str) -> list[str]:
    """Enumerate every upstream-changed path, with renames expanded to add/delete."""
    result = _run_git(
        repo,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        base_sha,
        target,
    )
    if result.returncode != 0:
        raise CheckError("Unable to enumerate upstream paths for content verification")
    try:
        return [path.decode("utf-8") for path in result.stdout.split(b"\0") if path]
    except UnicodeDecodeError as exc:
        raise CheckError("The upstream diff contains a non-UTF-8 path") from exc


def _merge_base(repo: Path, first_parent: str, target: str) -> str:
    result = _run_git(repo, "merge-base", first_parent, target)
    if result.returncode != 0:
        raise CheckError("Unable to compute the upstream base for content verification")
    value = result.stdout.decode("ascii", errors="replace").strip()
    _validate_sha(value, "computed upstream base")
    return value


def _verify_content_boundary(
    repo: Path,
    merge_commit: str,
    first_parent: str,
    target: str,
    policy: dict[str, str],
) -> str:
    """Prove the sync merge carries upstream content or a reviewed overlay resolution."""
    upstream_base = _merge_base(repo, first_parent, target)
    changed = _changed_paths(repo, upstream_base, target)
    if not changed:
        return f"Verified upstream content boundary {merge_commit} (no upstream changes)"
    merge_tree = _tree_snapshot(repo, merge_commit, "sync merge")
    target_tree = _tree_snapshot(repo, target, "upstream target")
    first_tree = _tree_snapshot(repo, first_parent, "fork base")
    for path in changed:
        merge_entry = merge_tree.get(path)
        target_entry = target_tree.get(path)
        first_entry = first_tree.get(path)
        mode = policy.get(path)
        if mode is None:
            if merge_entry != target_entry:
                raise CheckError(
                    f"The sync merge does not carry the upstream content for the non-overlay path {path}"
                )
            continue
        if first_entry == target_entry:
            if merge_entry != target_entry:
                raise CheckError(
                    f"The overlay path {path} must equal upstream content when no adaptation is needed"
                )
            continue
        if mode == "retain-base":
            if merge_entry != first_entry:
                raise CheckError(f"The retain-base overlay path {path} was not kept from the fork base")
        elif merge_entry == first_entry:
            raise CheckError(
                f"The overlay path {path} silently kept fork content instead of a reviewed resolution"
            )
    return f"Verified upstream content boundary {merge_commit} ({len(changed)} upstream paths)"


def _find_sync_merge(repo: Path, head_sha: str, target: str) -> tuple[str, str]:
    """Locate the unique merge whose second parent is the upstream target."""
    result = _run_git(repo, "rev-list", "--merges", "--parents", head_sha)
    if result.returncode != 0:
        raise CheckError("Unable to enumerate merge commits for content verification")
    matches = []
    for line in result.stdout.decode("ascii", errors="replace").splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == target:
            matches.append((fields[0], fields[1]))
    if len(matches) != 1:
        raise CheckError(
            "Expected exactly one upstream sync merge with the recorded target as second parent"
        )
    return matches[0]


def _read_sync_policy(repo: Path, base_sha: str, head_sha: str) -> dict[str, str]:
    """Require the protected overlay policy to be identical at the trusted base and head."""
    base_entry = _read_path_entry(repo, base_sha, OVERLAY_POLICY_FILE, "base overlay policy")
    if base_entry is None:
        raise CheckError("The protected upstream overlay policy is missing from the sync base")
    _require_regular_blob(base_entry, "sync base overlay policy")
    head_entry = _read_path_entry(repo, head_sha, OVERLAY_POLICY_FILE, "head overlay policy")
    if head_entry != base_entry:
        raise CheckError("The sync candidate changes the protected upstream overlay policy")
    return _read_overlay_policy(repo, base_sha)


def _assert_no_protected_changes(repo: Path, base_sha: str, head_sha: str) -> None:
    result = _run_git(
        repo,
        "diff",
        "--no-renames",
        "--name-only",
        f"{base_sha}..{head_sha}",
        "--",
        *sorted(PROTECTED_PATHS),
        *PROTECTED_PATHSPECS,
    )
    if result.returncode != 0:
        raise CheckError("Unable to inspect protected CI files in the PR diff")
    if result.stdout.strip():
        raise CheckError("The PR changes a protected CI gate file")


def verify_head_commit(
    repo: Path,
    head_sha: str,
    upstream_ref: str,
    *,
    expected_target_sha: str | None = None,
) -> str:
    """Verify a recorded marker in one commit object and its ancestry."""
    _require_commit(repo, head_sha, "HEAD")
    head_entry = _read_tree_entry(repo, head_sha, "HEAD")
    if head_entry is None:
        raise CheckError("The HEAD marker is missing")
    _require_regular_blob(head_entry, "HEAD")
    target = _read_marker(repo, head_sha, "HEAD")
    if expected_target_sha is not None:
        _validate_sha(expected_target_sha, "expected target")
        if target != expected_target_sha:
            raise CheckError("The recorded upstream target differs from the immutable expected target")
    _verify_target_ancestry(repo, target, head_sha, upstream_ref)
    merge_commit, first_parent = _find_sync_merge(repo, head_sha, target)
    policy = _read_overlay_policy(repo, head_sha)
    boundary = _verify_content_boundary(repo, merge_commit, first_parent, target, policy)
    return f"Verified recorded upstream ancestry; {boundary}"


def verify_pull_request(
    repo: Path,
    base_sha: str,
    head_sha: str,
    upstream_ref: str,
    *,
    expected_target_sha: str | None = None,
    require_version_bump: bool = False,
) -> str:
    """Verify the marker transition and strict merge ancestry for a PR."""
    _require_commit(repo, base_sha, "base")
    _require_commit(repo, head_sha, "head")
    _assert_no_protected_changes(repo, base_sha, head_sha)
    base_entry = _read_tree_entry(repo, base_sha, "base")
    head_entry = _read_tree_entry(repo, head_sha, "head")

    sync_mode = expected_target_sha is not None or require_version_bump
    if base_entry is None and head_entry is None:
        if sync_mode:
            raise CheckError("The sync candidate marker is missing at both ends")
        return "Recorded upstream marker absent at both ends; ordinary PR skipped"
    if base_entry is not None:
        _require_regular_blob(base_entry, "base")
    if head_entry is None:
        raise CheckError("The head marker is deleted")
    _require_regular_blob(head_entry, "head")

    if base_entry is not None and base_entry.object_name == head_entry.object_name:
        if sync_mode:
            raise CheckError("The sync candidate marker is unchanged")
        return "Recorded upstream marker unchanged; ordinary PR skipped"

    target = _read_marker(repo, head_sha, "head")
    if expected_target_sha is not None:
        _validate_sha(expected_target_sha, "expected target")
        if target != expected_target_sha:
            raise CheckError("The recorded upstream target differs from the immutable expected target")
    _verify_target_ancestry(repo, target, head_sha, upstream_ref)
    merge_commit = _verify_strict_merge(repo, base_sha, head_sha, target)
    if require_version_bump:
        _verify_version_bump(repo, base_sha, head_sha)
    policy = _read_sync_policy(repo, base_sha, head_sha)
    boundary = _verify_content_boundary(repo, merge_commit, base_sha, target, policy)
    return f"Verified two-parent merge commit {merge_commit}; {boundary}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal CI upstream ancestry check.")
    parser.add_argument(
        "--repo",
        default=".",
        help="Git repository containing the objects to inspect (default: current directory)",
    )
    parser.add_argument("--head-sha", help="Commit object to verify")
    parser.add_argument("--base-sha", help="PR base commit; enables marker transition checks")
    parser.add_argument(
        "--upstream-ref",
        help="Fetched upstream ref containing the recorded target, e.g. upstream/main",
    )
    parser.add_argument("--manifest", help="Manifest JSON exchanged between sync jobs")
    parser.add_argument("--expected-base-sha", help="Trusted base SHA for manifest mode")
    parser.add_argument(
        "--expected-target-sha",
        help="Trusted upstream target SHA for manifest or ancestry mode",
    )
    parser.add_argument("--expected-verified-sha", help="Trusted candidate tip SHA for manifest mode")
    parser.add_argument(
        "--require-version-bump",
        action="store_true",
        help="Require both fork package manifests to advance together",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = Path(args.repo).resolve()
        if not repo.is_dir():
            raise CheckError("The Git repository path is unavailable")
        if args.manifest is not None:
            expected_values = (
                args.expected_base_sha,
                args.expected_target_sha,
                args.expected_verified_sha,
            )
            if any(value is None for value in expected_values):
                raise CheckError("Manifest mode requires all three expected SHA values")
            validate_manifest(
                Path(args.manifest),
                expected_base_sha=args.expected_base_sha,
                expected_target_sha=args.expected_target_sha,
                expected_verified_sha=args.expected_verified_sha,
            )
            print("Verified exact sync manifest")
            return 0
        if args.head_sha is None or args.upstream_ref is None:
            raise CheckError("Ancestry mode requires --head-sha and --upstream-ref")
        if args.require_version_bump and args.base_sha is None:
            raise CheckError("--require-version-bump requires --base-sha")
        if args.base_sha is not None:
            if not args.base_sha:
                raise CheckError("--base-sha was provided with an empty value")
            message = verify_pull_request(
                repo,
                args.base_sha,
                args.head_sha,
                args.upstream_ref,
                expected_target_sha=args.expected_target_sha,
                require_version_bump=args.require_version_bump,
            )
        else:
            message = verify_head_commit(
                repo,
                args.head_sha,
                args.upstream_ref,
                expected_target_sha=args.expected_target_sha,
            )
    except CheckError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
