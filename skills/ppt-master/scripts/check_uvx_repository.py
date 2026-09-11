#!/usr/bin/env python3
"""
PPT Master - Repository UVX Migration Scanner

Scan tracked documentation and workflow files for registered scripts invoked
through legacy Python or ``uv run`` forms.  The scanner is read-only and uses
``git ls-files`` so hidden directories and ignored files cannot silently alter
the release result.

Usage:
    uvx ppt-master check-uvx-repository [--repo PATH]

Examples:
    uvx ppt-master check-uvx-repository
    uvx ppt-master check-uvx-repository --repo /workspace/ppt-master

Dependencies:
    None (only uses the Python standard library and Git).
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402


TEXT_SUFFIXES = frozenset({
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
})
DOCUMENT_ROOT_PREFIXES = (
    ".github/",
    ".opencode/",
    "docs/",
    "examples/",
    "skills/",
)
ROOT_DOCUMENT_NAMES = frozenset({
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "README.md",
    "README_CN.md",
})

# These are reviewed historical examples or repository-owned Linux CI command
# examples.  They are exact normalized paths and exact rule ids; no content
# keyword is consulted.  A new file containing the same words is not exempt.
EXPLICIT_ALLOWLIST: dict[str, frozenset[str]] = {
    ".opencode/command/sync-upstream.md": frozenset({
        "python-script",
        "uv-run-script",
    }),
    "docs/rules/code-style.md": frozenset({
        "python-script",
        "uv-run-script",
    }),
    "docs/superpowers/plans/2026-06-08-uvx-refactor.md": frozenset({
        "python-script",
        "uv-run-script",
    }),
    "docs/superpowers/plans/2026-08-03-test-report-fixes.md": frozenset({
        "python-script",
    }),
    "docs/superpowers/plans/2026-09-10-sync-upstream-ancestry.md": frozenset({
        "python-script",
    }),
    "docs/superpowers/specs/2026-06-08-uvx-refactor-design.md": frozenset({
        "python-script",
        "uv-run-script",
    }),
    "docs/windows-installation.md": frozenset({
        "python-script",
        "uv-run-script",
    }),
    "docs/zh/upstream-sync.md": frozenset({
        "python-script",
        "uv-run-script",
    }),
    # This is a repository-owned Linux CI invocation of a registered checker;
    # new workflow files do not inherit this exception.
    ".github/workflows/check-uvx-migration.yml": frozenset({"python-script"}),
}
ALLOWED_LEGACY_PATHS = frozenset(EXPLICIT_ALLOWLIST)

LEGACY_COMMAND_RE = re.compile(
    r"(?<![\w-])(?P<interpreter>python3?|uv\s+run)\s+"
    r"(?P<script>(?:\$\{SKILL_DIR\}/|skills/ppt-master/|scripts/)"
    r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py)\b"
)


class ScannerError(RuntimeError):
    """Represent a fail-closed repository scan error."""


@dataclass(frozen=True)
class Violation:
    """Describe one prohibited legacy command reference."""

    path: str
    line: int
    rule: str
    text: str


def normalize_script_path(value: str) -> str:
    """Normalize a CLI script value or a documented script reference."""
    normalized = value.replace("\\", "/")
    for prefix in ("${SKILL_DIR}/scripts/", "skills/ppt-master/scripts/", "scripts/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def allowed_rules_for_path(relative: str) -> frozenset[str]:
    """Return the exact scanner rules reviewed for one normalized path."""
    return EXPLICIT_ALLOWLIST.get(relative, frozenset())


def _literal_commands(cli_path: Path) -> dict[str, str]:
    if cli_path.is_symlink() or not cli_path.is_file():
        raise ScannerError(f"Root CLI mapping is unavailable or is a symlink: {cli_path}")
    try:
        tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ScannerError(f"Unable to parse root CLI mapping: {cli_path}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "COMMANDS" for target in targets):
            continue
        if node.value is None:
            raise ScannerError("Root CLI COMMANDS has no value")
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise ScannerError("Root CLI COMMANDS is not a literal mapping") from exc
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise ScannerError("Root CLI COMMANDS must map string commands to script paths")
        return value
    raise ScannerError("Root CLI COMMANDS mapping is missing")


def registered_script_paths(repo: Path) -> frozenset[str]:
    """Return normalized script paths that have a registered UVX command."""
    return frozenset(normalize_script_path(path) for path in _literal_commands(repo / "cli.py").values())


def tracked_paths(repo: Path) -> list[str]:
    """List tracked repository paths without omitting dot-directories."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ScannerError("Unable to execute git ls-files; refusing to scan an incomplete repository") from exc
    if result.returncode != 0:
        raise ScannerError("git ls-files failed; refusing to scan an incomplete repository")
    try:
        paths = [path.decode("utf-8") for path in result.stdout.split(b"\0") if path]
    except UnicodeDecodeError as exc:
        raise ScannerError("Tracked repository paths are not valid UTF-8") from exc
    for relative in paths:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ScannerError(f"Tracked repository path is unsafe: {relative}")
    return sorted(paths)


def _is_document_path(path: str) -> bool:
    path_obj = Path(path)
    if path_obj.parent == Path("."):
        return path_obj.suffix.lower() in TEXT_SUFFIXES
    if not path.startswith(DOCUMENT_ROOT_PREFIXES):
        return False
    return path_obj.suffix.lower() in TEXT_SUFFIXES


def scan_repository(repo: Path) -> list[Violation]:
    """Scan tracked command documents and return violations without rewriting them."""
    repo = repo.resolve()
    registered = registered_script_paths(repo)
    violations: list[Violation] = []
    for relative in tracked_paths(repo):
        if not _is_document_path(relative):
            continue
        path = repo / relative
        try:
            if path.is_symlink() or not stat.S_ISREG(os.lstat(path).st_mode):
                raise ScannerError(f"Tracked document is not a regular file: {relative}")
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ScannerError(f"Tracked document is not valid UTF-8: {relative}") from exc
        except OSError as exc:
            raise ScannerError(f"Unable to read tracked document: {relative}") from exc
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in LEGACY_COMMAND_RE.finditer(line):
                script = normalize_script_path(match.group("script"))
                if script not in registered:
                    continue
                rule = "uv-run-script" if match.group("interpreter").startswith("uv") else "python-script"
                if rule in allowed_rules_for_path(relative):
                    continue
                violations.append(Violation(relative, line_number, rule, line.rstrip()))
    return violations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan tracked repository command documents for legacy script invocations.",
    )
    parser.add_argument("--repo", default=".", help="Repository root to scan (default: current directory)")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        violations = scan_repository(Path(args.repo))
    except ScannerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for violation in violations:
        print(f"{violation.path}:{violation.line}: {violation.rule}: {violation.text}")
    if violations:
        print(f"ERROR: {len(violations)} legacy UVX command reference(s) found.", file=sys.stderr)
        return 1
    print("OK: tracked repository command documents contain no unallowlisted legacy script references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
