#!/usr/bin/env python3
"""Trusted, data-only structural gates for an upstream sync candidate.

This file is executed only from the trusted base checkout.  It reads an
isolated candidate worktree as data and never imports, sources, or executes a
candidate-controlled Python module, configuration file, hook, or action.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

try:
    import yaml
except ImportError:  # pragma: no cover - trusted job installs the pinned parser
    yaml = None


FORK_CONTRACTS = {
    "skills/ppt-master/scripts/confirm_ui/server.py": (
        "PPT_MASTER_LAUNCH_TOKEN",
        "normalized_project_key",
    ),
    "skills/ppt-master/scripts/svg_editor/server.py": (
        "PPT_MASTER_LAUNCH_TOKEN",
        "normalized_project_key",
    ),
    "skills/ppt-master/scripts/visual_review.py": ("normalized_project_key",),
    "skills/ppt-master/scripts/server_common.py": ("def normalized_project_key",),
    "skills/ppt-master/scripts/config.py": (
        "from project_management.paths import projects_root",
        "PROJECTS_DIR = projects_root()",
    ),
    "skills/ppt-master/scripts/project_management/paths.py": (
        "def projects_root",
        "PPT_MASTER_PROJECTS",
    ),
    "skills/ppt-master/scripts/project_manager.py": (
        "from project_management.paths import projects_root",
        "projects_root()",
    ),
    "skills/ppt-master/scripts/register_template.py": (
        "PPT_MASTER_TEMPLATES_DIR",
        "_TEMPLATES_DIR_SOURCE",
    ),
}

REQUIRED_REGULAR_FILES = {
    "cli.py",
    "skills/ppt-master/cli.py",
    "pyproject.toml",
    "skills/ppt-master/pyproject.toml",
    "skills/ppt-master/requirements.txt",
    "uv.lock",
    "skills/ppt-master/uv.lock",
    "MANIFEST.in",
    "skills/ppt-master/MANIFEST.in",
    "skills/ppt-master/SKILL.md",
    "skills/ppt-master/LICENSE",
    "skills/ppt-master/SPONSORS.md",
    "skills/ppt-master/SPONSORS_CN.md",
    *FORK_CONTRACTS,
}

ATTRIBUTION_NOTICES = {
    "README.md": (
        "Fork notice",
        "uvx ppt-master <command>",
        "sponsor and donation information below belongs to the original author",
    ),
    "README_CN.md": (
        "Fork 声明",
        "uvx ppt-master <command>",
        "赞助商与捐赠信息属于原作者",
    ),
    "PYPI_README.md": (
        "This PyPI package is a fork",
        "Sponsor and donation information shipped with the package belongs to the original author",
    ),
    "CONTRIBUTING.md": ("> **Fork note**", "uvx ppt-master <command>"),
    "docs/faq.md": ("| `uvx` (PyPI) |",),
    "docs/zh/faq.md": ("| `uvx`（PyPI） |",),
    "docs/windows-installation.md": ("> **Fork users**", "uvx ppt-master <command>"),
    "docs/zh/windows-installation.md": ("> **Fork 用户**", "uvx ppt-master <command>"),
    "docs/roadmap.md": ("> **Fork note**", "uvx ppt-master <command>"),
    "docs/zh/roadmap.md": ("> **Fork 注记**", "uvx ppt-master <command>"),
}

MANIFEST_CONTRACTS = {
    "MANIFEST.in": (
        "include skills/ppt-master/SKILL.md",
        "include skills/ppt-master/LICENSE",
        "include skills/ppt-master/SPONSORS.md",
        "include skills/ppt-master/SPONSORS_CN.md",
    ),
    "skills/ppt-master/MANIFEST.in": (
        "include SKILL.md",
        "include LICENSE",
        "include SPONSORS.md",
        "include SPONSORS_CN.md",
    ),
}


class CandidateGateError(RuntimeError):
    """Represent a trusted structural gate failure."""


def _regular(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise CandidateGateError(f"required candidate file is unavailable: {relative}") from exc
    if not stat.S_ISREG(mode):
        raise CandidateGateError(f"candidate file is not a regular file: {relative}")
    return path


def _reject_symlinks(root: Path) -> None:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            path = Path(directory) / name
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                raise CandidateGateError(f"candidate worktree contains a symbolic link: {relative}")


def _read_text(root: Path, relative: str) -> str:
    return _regular(root, relative).read_text(encoding="utf-8")


def _load_trusted_module(repo: Path, relative: str, name: str) -> ModuleType:
    path = _regular(repo, relative)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CandidateGateError(f"unable to load trusted gate module: {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _literal_mapping(root: Path, relative: str, name: str) -> dict[str, str]:
    path = _regular(root, relative)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise CandidateGateError(f"unable to parse candidate mapping: {relative}:{name}") from exc
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if value is None:
            break
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise CandidateGateError(f"candidate mapping is not a literal dict: {relative}:{name}") from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
        ):
            raise CandidateGateError(f"candidate mapping must be string-to-string: {relative}:{name}")
        return parsed
    raise CandidateGateError(f"candidate mapping is missing: {relative}:{name}")


def _check_cli_contract(repo: Path, candidate: Path) -> None:
    trusted_cli = _load_trusted_module(
        repo,
        "skills/ppt-master/scripts/check_cli_sync.py",
        "trusted_check_cli_sync",
    )
    root_cli = candidate / "cli.py"
    skill_cli = candidate / "skills/ppt-master/cli.py"
    root_names, root_scripts = trusted_cli.parse_commands_from_cli(str(root_cli))
    skill_names, skill_scripts = trusted_cli.parse_commands_from_cli(str(skill_cli))
    root_commands = _literal_mapping(candidate, "cli.py", "COMMANDS")
    skill_commands = _literal_mapping(candidate, "skills/ppt-master/cli.py", "COMMANDS")
    root_aliases = _literal_mapping(candidate, "cli.py", "ALIASES")
    skill_aliases = _literal_mapping(candidate, "skills/ppt-master/cli.py", "ALIASES")
    if root_commands != skill_commands or root_aliases != skill_aliases:
        raise CandidateGateError("CLI COMMANDS and ALIASES mappings differ between root and Skill")
    if root_names != skill_names or root_scripts != skill_scripts:
        raise CandidateGateError("CLI command names or mapped script values differ between root and Skill")

    discovered = trusted_cli.find_scripts_with_main(str(candidate / "skills/ppt-master/scripts"))
    excluded = {
        "check_cli_sync.py",
        "check_uvx_migration.py",
        "svg_editor/app.py",
        "confirm_ui/server.py",
        "svg_to_pptx/pptx_cli.py",
        "svg_to_pptx/pptx_package/cli.py",
        "project_management/cli.py",
        "svg_quality/cli.py",
    }
    discovered = {path for path in discovered if path not in excluded and not path.startswith("svg_finalize/")}
    missing = discovered - set(root_commands.values())
    if missing:
        raise CandidateGateError(f"CLI COMMANDS is missing script mappings: {sorted(missing)}")


def _check_uvx_contract(repo: Path, candidate: Path, base_sha: str, head_sha: str) -> None:
    trusted_uvx = _load_trusted_module(
        repo,
        "skills/ppt-master/scripts/check_uvx_migration.py",
        "trusted_check_uvx_migration",
    )
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}..{head_sha}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CandidateGateError("unable to enumerate candidate files for the uvx gate")
    changed = [line for line in result.stdout.splitlines() if line]
    checkable = [
        relative
        for relative in changed
        if Path(relative).suffix.lower() in trusted_uvx.CHECK_EXTENSIONS
        and relative not in trusted_uvx.ALLOWED_FILES
        and not any(relative == allowed or relative.startswith(allowed) for allowed in trusted_uvx.ALLOWED_DIRS)
    ]
    violations = []
    for relative in checkable:
        path = candidate / relative
        if not path.exists() and not path.is_symlink():
            continue
        violations.extend(trusted_uvx.check_file(str(_regular(candidate, relative))))
    if violations:
        raise CandidateGateError("trusted uvx migration gate found old-style command references")


def _check_dependency_contract(repo: Path, candidate: Path) -> None:
    trusted_deps = _load_trusted_module(
        repo,
        "skills/ppt-master/scripts/check_deps_sync.py",
        "trusted_check_deps_sync",
    )
    root_deps = trusted_deps.parse_pyproject_deps(candidate / "pyproject.toml")
    skill_deps = trusted_deps.parse_pyproject_deps(candidate / "skills/ppt-master/pyproject.toml")
    requirements = trusted_deps.parse_requirements_txt(candidate / "skills/ppt-master/requirements.txt")
    errors = []
    errors.extend(trusted_deps.compare_pyproject_pair(root_deps, skill_deps))
    errors.extend(
        trusted_deps.compare_req_vs_pyproject(
            requirements,
            root_deps,
            req_label="requirements.txt",
            pyproject_label="root pyproject.toml",
        )
    )
    errors.extend(
        trusted_deps.compare_req_vs_pyproject(
            requirements,
            skill_deps,
            req_label="requirements.txt",
            pyproject_label="skills/ppt-master/pyproject.toml",
        )
    )
    if (candidate / "uv.lock").read_bytes() != (candidate / "skills/ppt-master/uv.lock").read_bytes():
        errors.append("uv.lock files differ byte-for-byte")
    if errors:
        raise CandidateGateError("dependency gate failed: " + "; ".join(errors))


def _check_attribution_contract(repo: Path, candidate: Path) -> None:
    trusted_guard = _load_trusted_module(
        repo,
        "skills/ppt-master/scripts/attribution_guard.py",
        "trusted_attribution_guard",
    )
    if not trusted_guard.integrity_is_valid_for(candidate / "skills/ppt-master"):
        raise CandidateGateError("attribution guard rejected the candidate Skill tree")


def _check_fork_contract(candidate: Path) -> None:
    for relative, markers in FORK_CONTRACTS.items():
        text = _read_text(candidate, relative)
        missing = [marker for marker in markers if marker not in text]
        if missing:
            raise CandidateGateError(f"fork contract markers missing from {relative}: {missing}")


def _check_yaml_contract(candidate: Path) -> None:
    if yaml is None:
        raise CandidateGateError("trusted YAML parser is unavailable")
    paths = sorted((candidate / ".github" / "workflows").rglob("*.yml"))
    paths.extend(sorted((candidate / ".github" / "workflows").rglob("*.yaml")))
    pull_path = candidate / ".github" / "pull.yml"
    if pull_path.exists() or pull_path.is_symlink():
        paths.append(pull_path)
    for path in paths:
        relative = path.relative_to(candidate).as_posix()
        try:
            yaml.safe_load(_regular(candidate, relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise CandidateGateError(f"candidate YAML is invalid: {relative}") from exc


def _check_manifest_contract(candidate: Path) -> None:
    for relative, fragments in MANIFEST_CONTRACTS.items():
        text = _read_text(candidate, relative)
        missing = [fragment for fragment in fragments if fragment not in text]
        if missing:
            raise CandidateGateError(f"package manifest contract missing from {relative}: {missing}")


def _check_notice_contract(candidate: Path) -> None:
    for relative, fragments in ATTRIBUTION_NOTICES.items():
        text = _read_text(candidate, relative)
        missing = [fragment for fragment in fragments if fragment not in text]
        if missing:
            raise CandidateGateError(f"fork notice/sponsor contract missing from {relative}: {missing}")


def check_candidate(
    repo: Path,
    candidate: Path,
    *,
    base_sha: str | None,
    candidate_ref: str | None,
    head_sha: str | None,
) -> None:
    """Run all trusted data-only candidate gates."""
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise CandidateGateError("candidate worktree is unavailable")
    _reject_symlinks(candidate)
    for relative in sorted(REQUIRED_REGULAR_FILES):
        _regular(candidate, relative)
    if candidate_ref is not None and head_sha is not None:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{candidate_ref}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != head_sha:
            raise CandidateGateError("candidate ref tip does not match the trusted verified SHA")
    _check_cli_contract(repo, candidate)
    if base_sha is not None and head_sha is not None:
        _check_uvx_contract(repo, candidate, base_sha, head_sha)
    _check_dependency_contract(repo, candidate)
    _check_attribution_contract(repo, candidate)
    _check_fork_contract(candidate)
    _check_manifest_contract(candidate)
    _check_notice_contract(candidate)
    _check_yaml_contract(candidate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run trusted data-only sync candidate gates.")
    parser.add_argument("--repo", required=True, help="Trusted Git repository object store")
    parser.add_argument("--candidate-root", required=True, help="Isolated candidate worktree root")
    parser.add_argument("--base-sha", help="Trusted base SHA for changed-file structural gates")
    parser.add_argument("--candidate-ref", help="Trusted imported candidate ref")
    parser.add_argument("--head-sha", help="Authoritative verified candidate SHA")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        check_candidate(
            Path(args.repo),
            Path(args.candidate_root),
            base_sha=args.base_sha,
            candidate_ref=args.candidate_ref,
            head_sha=args.head_sha,
        )
    except CandidateGateError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print("Trusted data-only sync candidate gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
