#!/usr/bin/env python3
"""PPT Master - PyPI Cleanup Report

Report retention decisions from public metadata without authentication or deletion.

Usage:
    uvx --from packaging==25.0 python .github/maintenance/pypi_cleanup.py report [options]

Examples:
    uvx --from packaging==25.0 python .github/maintenance/pypi_cleanup.py report --json report.json

Dependencies:
    packaging==25.0
"""

from __future__ import annotations

import argparse
import json
import sys
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


def build_parser() -> argparse.ArgumentParser:
    """Build the read-only maintenance CLI."""
    parser = argparse.ArgumentParser(
        description="Report PyPI cleanup candidates for ppt-master without deleting anything.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="Read public metadata and report retention decisions")
    report.add_argument("--json", type=Path, help="Write the machine-readable report to this path")
    report.add_argument("--markdown", type=Path, help="Write the Markdown report to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Fetch, validate, and emit a report; send failures to stderr."""
    args = build_parser().parse_args(argv)
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
