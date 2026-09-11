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
    assignments = [
        statement
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "COMMANDS"
            for target in (
                statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            )
        )
    ]
    if not assignments:
        raise ScannerError("Root CLI COMMANDS mapping is missing")
    if len(assignments) != 1:
        raise ScannerError("Root CLI COMMANDS must be assigned exactly once at module level")
    assignment = assignments[0]
    for node in ast.walk(tree):
        if node is assignment:
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                (isinstance(target, ast.Name) and target.id == "COMMANDS")
                or (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "COMMANDS"
                )
                for target in targets
            ):
                raise ScannerError("Root CLI COMMANDS is dynamically mutated")
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if (isinstance(target, ast.Name) and target.id == "COMMANDS") or (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "COMMANDS"
            ):
                raise ScannerError("Root CLI COMMANDS is dynamically mutated")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "COMMANDS"
            and node.func.attr in {"clear", "pop", "popitem", "setdefault", "update"}
        ):
            raise ScannerError("Root CLI COMMANDS is dynamically mutated")
    if assignment.value is None:
        raise ScannerError("Root CLI COMMANDS has no value")
    try:
        value = ast.literal_eval(assignment.value)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise ScannerError("Root CLI COMMANDS is not a literal mapping") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ScannerError("Root CLI COMMANDS must map string commands to script paths")
    return value


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


def _logical_command_lines(text: str) -> list[tuple[int, str]]:
    """Join shell backslash continuations while keeping the first physical line."""
    logical: list[tuple[int, str]] = []
    buffer = ""
    start_line = 1
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.rstrip()
        if not buffer:
            start_line = line_number
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        buffer += stripped
        logical.append((start_line, buffer))
        buffer = ""
    if buffer:
        logical.append((start_line, buffer.rstrip()))
    return logical


def scan_text(
    relative: str,
    text: str,
    registered: frozenset[str] | None = None,
) -> list[Violation]:
    """Report legacy script references in one document without rewriting it."""
    violations: list[Violation] = []
    for line_number, logical_line in _logical_command_lines(text):
        for match in LEGACY_COMMAND_RE.finditer(logical_line):
            script = normalize_script_path(match.group("script"))
            if registered is not None and script not in registered:
                continue
            rule = "uv-run-script" if match.group("interpreter").startswith("uv") else "python-script"
            if rule in allowed_rules_for_path(relative):
                continue
            violations.append(Violation(relative, line_number, rule, logical_line.rstrip()))
    return violations


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
        violations.extend(scan_text(relative, text, registered))
    return violations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan tracked repository command documents for legacy script invocations.",
    )
    parser.add_argument("--repo", default=".", help="Repository root to scan (default: current directory)")
    return parser


def main(argv: list[str] | None = None) -> int:
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from console_encoding import configure_utf8_stdio

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
