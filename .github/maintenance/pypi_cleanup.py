#!/usr/bin/env python3
"""PPT Master - PyPI Cleanup Report and Local Apply

Report retention decisions from public metadata, and (only from a local
interactive terminal) confirm and apply the exact reported deletions.

Usage:
    uvx --from packaging==25.0 python .github/maintenance/pypi_cleanup.py report [options]
    uvx --from packaging==25.0 python .github/maintenance/pypi_cleanup.py apply --username <name>

Examples:
    uvx --from packaging==25.0 python .github/maintenance/pypi_cleanup.py report --json report.json
    uvx --from packaging==25.0 python .github/maintenance/pypi_cleanup.py apply --username yourname

Dependencies:
    packaging==25.0
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from packaging.version import InvalidVersion, Version


PROJECT = "ppt-master"
METADATA_URL = "https://pypi.org/pypi/ppt-master/json"
KEEP_LATEST = 10
KEEP_DAYS = 30
HTTP_TIMEOUT = 30
CLEANUP_TOOL = ("uvx", "--from", "pypi-cleanup==0.1.12", "pypi-cleanup")
CONFIRMATION_PHRASE = "DELETE ppt-master"
PASSWORD_ENV = "PYPI_CLEANUP_PASSWORD"
ACTION_ENV = "GITHUB_ACTIONS"
VERIFY_ATTEMPTS = 6
VERIFY_INTERVAL_SECONDS = 5


class CleanupRefused(RuntimeError):
    """A destructive apply was refused before any subprocess or deletion."""


def fetch_metadata() -> dict:
    """Read the fixed public project endpoint without authentication configuration."""
    with urlopen(METADATA_URL, timeout=HTTP_TIMEOUT) as response:
        return json.load(response)


def _utc_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must contain a timezone-aware upload timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid upload timestamp: {value!r}") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{label} upload timestamp must include a timezone")
    return stamp.astimezone(timezone.utc)


def _iso_utc(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_version(value: str) -> Version:
    if not isinstance(value, str):
        raise ValueError("Version must be a PEP 440 string")
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise ValueError(f"Invalid PEP 440 version: {value!r}") from exc


def build_report(metadata: dict, *, now: datetime) -> dict:
    """Partition non-empty releases by PEP 440 rank, latest upload, and default version."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    if not isinstance(metadata, dict):
        raise ValueError("PyPI metadata must be a JSON object")
    info = metadata.get("info")
    if not isinstance(info, dict) or info.get("name") != PROJECT:
        raise ValueError(f"PyPI metadata must identify project {PROJECT!r}")
    default_version = _parse_version(info.get("version"))
    releases = metadata.get("releases")
    if not isinstance(releases, dict):
        raise ValueError("PyPI metadata must contain a releases object")

    entries = []
    for version, files in releases.items():
        parsed = _parse_version(version)
        if not isinstance(files, list):
            raise ValueError(f"Release {version!r} must contain a file list")
        if not files:
            continue
        size_bytes = 0
        uploads = []
        for file in files:
            if not isinstance(file, dict):
                raise ValueError(f"Release {version!r} contains invalid file metadata")
            size = file.get("size")
            if type(size) is not int or size < 0:
                raise ValueError(f"Release {version!r} file size must be a non-negative integer")
            size_bytes += size
            uploads.append(_utc_timestamp(file.get("upload_time_iso_8601"), version))
        entries.append((parsed, max(uploads), {
            "version": version,
            "size_bytes": size_bytes,
            "latest_upload": _iso_utc(max(uploads)),
        }))

    entries.sort(key=lambda entry: (entry[0], entry[2]["version"]), reverse=True)
    cutoff = now - timedelta(days=KEEP_DAYS)
    keep, delete = [], []
    for rank, (parsed, latest_upload, entry) in enumerate(entries):
        retained = rank < KEEP_LATEST or latest_upload >= cutoff or parsed == default_version
        (keep if retained else delete).append(entry)
    return {
        "project": PROJECT,
        "generated_at": _iso_utc(now),
        "keep_latest": KEEP_LATEST,
        "keep_days": KEEP_DAYS,
        "total_bytes": sum(entry[2]["size_bytes"] for entry in entries),
        "reclaim_bytes": sum(entry["size_bytes"] for entry in delete),
        "keep": keep,
        "delete": delete,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        f"# PyPI cleanup report: {report['project']}",
        "",
        f"Generated at: {report['generated_at']}",
        "",
        f"Keep the latest {report['keep_latest']} non-empty versions, versions with uploads "
        f"in the last {report['keep_days']} days (inclusive), and the default version.",
        "",
        f"Total: {report['total_bytes']} bytes. Reclaimable: {report['reclaim_bytes']} bytes.",
        "",
        "Read-only report; no files or versions have been deleted.",
    ]
    for label, key in [("Keep", "keep"), ("Delete candidates", "delete")]:
        lines.extend([
            "", f"## {label} ({len(report[key])})", "",
            "| Version | Size (bytes) | Latest upload (UTC) |",
            "| --- | ---: | --- |",
        ])
        for entry in report[key]:
            lines.append(
                f"| {entry['version']} | {entry['size_bytes']} | {entry['latest_upload']} |"
            )
    return "\n".join(lines) + "\n"


def build_cleanup_command(versions: list[str], username: str) -> list[str]:
    """Build the fixed pypi-cleanup argument array for exactly the confirmed versions."""
    if not isinstance(versions, list) or not versions:
        raise ValueError("At least one confirmed version is required")
    if any(not isinstance(version, str) or not version for version in versions):
        raise ValueError("Confirmed versions must be non-empty strings")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("A PyPI username is required")
    pattern = "^(?:" + "|".join(re.escape(version) for version in sorted(versions)) + ")$"
    return [
        *CLEANUP_TOOL,
        "--package", PROJECT,
        "--username", username,
        "--version-regex", pattern,
        "--days", str(KEEP_DAYS),
        "--yes",
        "--do-it",
    ]


def verify_cleanup(before: dict, after: dict) -> dict:
    """Compare the confirmed candidates and kept versions against a fresh report."""
    confirmed = [entry["version"] for entry in before["delete"]]
    kept = [entry["version"] for entry in before["keep"]]
    present = {entry["version"] for entry in after["keep"]}
    present |= {entry["version"] for entry in after["delete"]}
    return {
        "deleted": [version for version in confirmed if version not in present],
        "remaining": [version for version in confirmed if version in present],
        "missing_kept": [version for version in kept if version not in present],
    }


def _require_local_terminal() -> None:
    """Refuse destructive work outside a local interactive terminal."""
    if os.environ.get(ACTION_ENV):
        raise CleanupRefused(
            f"refusing to delete: {ACTION_ENV} is set, so this is a CI environment; "
            "the workflow and this entry are read-only there."
        )
    if not sys.stdin.isatty():
        raise CleanupRefused("refusing to delete: stdin is not an interactive terminal.")


def _subprocess_environment() -> dict[str, str]:
    """Inherit the local environment but never pass a stored PyPI password."""
    environment = dict(os.environ)
    environment.pop(PASSWORD_ENV, None)
    return environment


def _render_candidates(report: dict, candidates: list[str]) -> str:
    """Render the complete candidate list and capacity shown before confirmation."""
    entries = {entry["version"]: entry for entry in report["delete"]}
    lines = [f"Delete candidates for {report['project']} ({len(candidates)}):"]
    for version in candidates:
        entry = entries[version]
        lines.append(
            f"  {version}  {entry['size_bytes']} bytes  latest upload {entry['latest_upload']}"
        )
    lines.append(
        f"Reclaimable: {report['reclaim_bytes']} bytes of "
        f"{report['total_bytes']} total nonempty-version bytes."
    )
    return "\n".join(lines)


def apply_cleanup(username: str) -> int:
    """Confirm interactively, delete exactly the reported candidates, then verify."""
    try:
        _require_local_terminal()
        before = build_report(fetch_metadata(), now=datetime.now(timezone.utc))
        candidates = [entry["version"] for entry in before["delete"]]
        if not candidates:
            print(
                "No delete candidates for ppt-master; nothing to delete and no subprocess "
                "was started.",
                file=sys.stderr,
            )
            return 1
        print(_render_candidates(before, candidates))
        answer = input(f"Type {CONFIRMATION_PHRASE} to delete these {len(candidates)} versions: ")
        if answer != CONFIRMATION_PHRASE:
            print(
                f"Confirmation did not match {CONFIRMATION_PHRASE!r}; no deletion was attempted.",
                file=sys.stderr,
            )
            return 1
        try:
            fresh = build_report(fetch_metadata(), now=datetime.now(timezone.utc))
        except (OSError, URLError, ValueError) as exc:
            print(
                f"Pre-deletion re-check failed: {exc}. No deletion was attempted; "
                "run the report subcommand and retry from a fresh preview.",
                file=sys.stderr,
            )
            return 1
        deletable = {entry["version"] for entry in fresh["delete"]}
        changed = [version for version in candidates if version not in deletable]
        if changed:
            print(
                "These confirmed candidates are no longer deletable and will not be sent to "
                f"the tool: {', '.join(changed)}. No deletion was attempted; run apply again "
                "to preview the current candidates.",
                file=sys.stderr,
            )
            return 1
        completed = subprocess.run(
            build_cleanup_command(candidates, username),
            env=_subprocess_environment(),
            check=False,
        )
        if completed.returncode != 0:
            print(
                f"pypi-cleanup exited with {completed.returncode}; no deletion is confirmed. "
                f"Unfinished candidates: {', '.join(candidates)}. "
                "Inspect the project and retry from a fresh preview.",
                file=sys.stderr,
            )
            return 1
        return _verify_deletion(before, candidates)
    except CleanupRefused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except EOFError:
        print("No confirmation was read; no deletion was attempted.", file=sys.stderr)
        return 1
    except (OSError, URLError, ValueError) as exc:
        print(
            f"PyPI cleanup apply failed: {exc}. Check public metadata and the terminal, then retry.",
            file=sys.stderr,
        )
        return 1


def _verify_deletion(before: dict, candidates: list[str]) -> int:
    """Re-query the public endpoint a bounded number of times; GET only, never delete again."""
    remaining = list(candidates)
    for _ in range(VERIFY_ATTEMPTS):
        time.sleep(VERIFY_INTERVAL_SECONDS)
        try:
            after = build_report(fetch_metadata(), now=datetime.now(timezone.utc))
        except (OSError, URLError, ValueError) as exc:
            print(
                f"Post-deletion verification query failed: {exc}. "
                f"Unfinished candidates: {', '.join(candidates)}. "
                "Run the report subcommand to inspect the project, then retry from a fresh preview.",
                file=sys.stderr,
            )
            return 1
        protected = sorted(set(candidates) & {entry["version"] for entry in after["keep"]})
        if protected:
            print(
                "These confirmed candidates are now protected by a new file upload or the "
                f"retention rules: {', '.join(protected)}. No deletion was retried; "
                "run apply again to preview the current candidates.",
                file=sys.stderr,
            )
            return 1
        result = verify_cleanup(before, after)
        if result["missing_kept"]:
            print(
                f"Kept versions disappeared unexpectedly: {', '.join(result['missing_kept'])}. "
                "Stop and inspect the project on PyPI before any further cleanup.",
                file=sys.stderr,
            )
            return 1
        remaining = result["remaining"]
        if not remaining:
            print(
                f"Verified deletion of {len(result['deleted'])} version(s): "
                f"{', '.join(result['deleted'])}"
            )
            return 0
    print(
        f"Candidates were not fully deleted after {VERIFY_ATTEMPTS} checks "
        f"({VERIFY_INTERVAL_SECONDS}s apart): {', '.join(remaining)}. "
        "No deletion is retried; inspect the project and retry from a fresh preview.",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the maintenance CLI: a read-only report and a local interactive apply."""
    parser = argparse.ArgumentParser(
        description="Report PyPI cleanup candidates for ppt-master, and apply them only "
                    "from a local interactive terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="Read public metadata and report retention decisions")
    report.add_argument("--json", type=Path, help="Write the machine-readable report to this path")
    report.add_argument("--markdown", type=Path, help="Write the Markdown report to this path")
    apply_cmd = commands.add_parser(
        "apply",
        help="Interactively delete the exact reported candidates and verify the result",
    )
    apply_cmd.add_argument(
        "--username",
        required=True,
        help="PyPI username for the child tool; the password is never read, passed or stored here",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch report generation or the local interactive apply; send failures to stderr."""
    args = build_parser().parse_args(argv)
    if args.command == "apply":
        return apply_cleanup(args.username)
    try:
        report = build_report(fetch_metadata(), now=datetime.now(timezone.utc))
        markdown = _render_markdown(report)
        if args.json is not None:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if args.markdown is not None:
            args.markdown.write_text(markdown, encoding="utf-8")
        if args.json is None and args.markdown is None:
            print(markdown, end="")
        return 0
    except (OSError, URLError, ValueError) as exc:
        print(f"PyPI cleanup report failed: {exc}. Check public metadata and output paths, then retry.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Repository-only tools do not load the skill's project transcript/integrity runtime.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    raise SystemExit(main())
