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
import re
import subprocess
import sys
from dataclasses import dataclass


TARGET_FILE = ".github/upstream-main.sha"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MARKER_CONTENT_RE = re.compile(rb"^[0-9a-f]{40}\n?$")


class CheckError(RuntimeError):
    """Represent a fail-closed ancestry check error."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    object_name: str


def _run_git(*args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CheckError("Unable to execute Git for the ancestry check") from exc


def _validate_sha(value: str, label: str) -> None:
    if not SHA_RE.fullmatch(value):
        raise CheckError(f"{label} must be a 40-character lowercase SHA")


def _require_commit(value: str, label: str) -> None:
    _validate_sha(value, label)
    result = _run_git("cat-file", "-e", f"{value}^{{commit}}")
    if result.returncode != 0:
        raise CheckError(f"{label} commit object is unavailable")


def _read_tree_entry(commit_sha: str, label: str) -> TreeEntry | None:
    result = _run_git("ls-tree", "-z", "--full-tree", commit_sha, "--", TARGET_FILE)
    if result.returncode != 0:
        raise CheckError(f"Unable to inspect the {label} commit object")

    records = [record for record in result.stdout.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1:
        raise CheckError(f"The {label} marker path is ambiguous in the commit object")

    try:
        metadata, path = records[0].split(b"\t", 1)
        mode, object_type, object_name = metadata.decode("ascii").split()
        path_text = path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise CheckError(f"The {label} marker tree entry is malformed") from exc
    if path_text != TARGET_FILE:
        raise CheckError(f"The {label} marker tree entry is malformed")
    return TreeEntry(mode, object_type, object_name)


def _require_regular_blob(entry: TreeEntry, label: str) -> None:
    if entry.mode != "100644" or entry.object_type != "blob":
        raise CheckError(f"The {label} marker entry must be a regular 100644 blob")


def _read_marker(commit_sha: str, label: str) -> str:
    result = _run_git("show", f"{commit_sha}:{TARGET_FILE}")
    if result.returncode != 0 or not MARKER_CONTENT_RE.fullmatch(result.stdout):
        raise CheckError(
            f"The {label} marker content is invalid; expected one lowercase 40-character SHA"
        )
    return result.stdout.rstrip(b"\n").decode("ascii")


def _is_ancestor(ancestor: str, descendant: str, message: str) -> None:
    result = _run_git("merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise CheckError(message)
    raise CheckError("Git could not evaluate the required ancestry relationship")


def _verify_target_ancestry(target: str, head_sha: str, upstream_ref: str) -> None:
    _is_ancestor(target, upstream_ref, "The recorded upstream target is not in upstream/main history")
    _is_ancestor(target, head_sha, "The recorded upstream target is not an ancestor of the checked commit")


def _verify_strict_merge(base_sha: str, head_sha: str, target: str) -> str:
    result = _run_git("rev-list", "--merges", "--parents", f"{base_sha}..{head_sha}")
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


def verify_head_commit(head_sha: str, upstream_ref: str) -> str:
    """Verify a recorded marker in one commit object and its ancestry."""
    _require_commit(head_sha, "HEAD")
    head_entry = _read_tree_entry(head_sha, "HEAD")
    if head_entry is None:
        raise CheckError("The HEAD marker is missing")
    _require_regular_blob(head_entry, "HEAD")
    target = _read_marker(head_sha, "HEAD")
    _verify_target_ancestry(target, head_sha, upstream_ref)
    return "Verified recorded upstream ancestry"


def verify_pull_request(base_sha: str, head_sha: str, upstream_ref: str) -> str:
    """Verify the marker transition and strict merge ancestry for a PR."""
    _require_commit(base_sha, "base")
    _require_commit(head_sha, "head")
    base_entry = _read_tree_entry(base_sha, "base")
    head_entry = _read_tree_entry(head_sha, "head")

    if base_entry is None and head_entry is None:
        return "Recorded upstream marker absent at both ends; ordinary PR skipped"
    if base_entry is not None:
        _require_regular_blob(base_entry, "base")
    if head_entry is None:
        raise CheckError("The head marker is deleted")
    _require_regular_blob(head_entry, "head")

    if base_entry is not None and base_entry.object_name == head_entry.object_name:
        return "Recorded upstream marker unchanged; ordinary PR skipped"

    target = _read_marker(head_sha, "head")
    _verify_target_ancestry(target, head_sha, upstream_ref)
    merge_commit = _verify_strict_merge(base_sha, head_sha, target)
    return f"Verified two-parent merge commit {merge_commit}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal CI upstream ancestry check.")
    parser.add_argument("--head-sha", required=True, help="Commit object to verify")
    parser.add_argument("--base-sha", help="PR base commit; enables marker transition checks")
    parser.add_argument(
        "--upstream-ref",
        required=True,
        help="Fetched upstream ref containing the recorded target, e.g. upstream/main",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.base_sha:
            message = verify_pull_request(args.base_sha, args.head_sha, args.upstream_ref)
        else:
            message = verify_head_commit(args.head_sha, args.upstream_ref)
    except CheckError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
