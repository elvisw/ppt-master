"""Verify all scripts in the scripts directory have a mapping in cli.py."""

import ast
import os
import sys
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPTS_DIR)))
SKILL_DIR = os.path.join(ROOT_DIR, "skills", "ppt-master")
CLI_FILE = os.path.join(ROOT_DIR, "cli.py")
SKILL_CLI_FILE = os.path.join(SKILL_DIR, "cli.py")


def find_scripts_with_main(scripts_dir: str) -> set[str]:
    """Find all .py files under scripts_dir that define a main() function."""
    scripts: set[str] = set()
    for root, dirs, files in os.walk(scripts_dir):
        dirs[:] = [d for d in dirs if not d.startswith("_") and d != "__pycache__"]
        for f in files:
            if not f.endswith(".py") or f.startswith("__"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "main":
                    rel = os.path.relpath(path, scripts_dir)
                    scripts.add(rel.replace("\\", "/"))
                    break
    return scripts


def parse_commands_from_cli(cli_path: str) -> tuple[set[str], set[str]]:
    """Parse the COMMANDS dict from cli.py to extract (command_names, script_paths)."""
    commands, _ = parse_cli_mappings(cli_path)
    return set(commands), set(commands.values())


def normalize_script_path(script_path: str) -> str:
    """Normalize separators and redundant leading ``./`` in a script value."""
    normalized = script_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _normalize_script_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """Normalize only values that represent script paths."""
    return {
        key: normalize_script_path(value)
        for key, value in mapping.items()
    }


def _parse_literal_mapping(cli_path: str, name: str) -> dict[str, str]:
    with open(cli_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=cli_path)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if node.value is None:
            raise ValueError(f"{name} in {cli_path} has no value")
        try:
            value = ast.literal_eval(node.value)
        except (SyntaxError, TypeError, ValueError) as exc:
            raise ValueError(f"{name} in {cli_path} is not a literal mapping") from exc
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise ValueError(f"{name} in {cli_path} must map strings to strings")
        return value
    raise ValueError(f"{name} mapping missing from {cli_path}")


def parse_cli_mappings(cli_path: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse complete COMMANDS and ALIASES mappings from one CLI file."""
    return (
        _parse_literal_mapping(cli_path, "COMMANDS"),
        _parse_literal_mapping(cli_path, "ALIASES"),
    )


def compare_cli_mappings(root_cli: Path, skill_cli: Path) -> list[str]:
    """Compare every command and alias key together with its mapped value."""
    errors: list[str] = []
    try:
        root_commands, root_aliases = parse_cli_mappings(str(root_cli))
        skill_commands, skill_aliases = parse_cli_mappings(str(skill_cli))
    except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
        return [f"ERROR: Unable to parse CLI mappings: {exc}"]

    root_commands = _normalize_script_mapping(root_commands)
    skill_commands = _normalize_script_mapping(skill_commands)

    for label, root_mapping, skill_mapping in (
        ("COMMANDS", root_commands, skill_commands),
        ("ALIASES", root_aliases, skill_aliases),
    ):
        for key in sorted(set(root_mapping) | set(skill_mapping)):
            if key not in root_mapping:
                errors.append(f"{label} key {key!r} is missing from root cli.py")
            elif key not in skill_mapping:
                errors.append(f"{label} key {key!r} is missing from skills/ppt-master/cli.py")
            elif root_mapping[key] != skill_mapping[key]:
                errors.append(
                    f"{label} mapping {key!r} differs: root={root_mapping[key]!r}, "
                    f"skill={skill_mapping[key]!r}"
                )

    for label, commands, aliases in (
        ("root cli.py", root_commands, root_aliases),
        ("skills/ppt-master/cli.py", skill_commands, skill_aliases),
    ):
        overlap = sorted(set(commands) & set(aliases))
        if overlap:
            errors.append(f"{label} aliases must stay outside COMMANDS: {overlap}")
        unknown_targets = sorted(set(aliases.values()) - set(commands))
        if unknown_targets:
            errors.append(f"{label} aliases target unknown commands: {unknown_targets}")
    return errors


def collect_cli_sync_errors(repo_root: Path) -> list[str]:
    """Return all root/Skill CLI mapping and discovered-script errors."""
    root_cli = repo_root / "cli.py"
    skill_cli = repo_root / "skills" / "ppt-master" / "cli.py"
    if not root_cli.is_file() or not skill_cli.is_file():
        return [f"ERROR: CLI files are missing: {root_cli}, {skill_cli}"]
    errors = compare_cli_mappings(root_cli, skill_cli)
    try:
        root_commands, _ = parse_cli_mappings(str(root_cli))
    except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
        return errors + [f"ERROR: Unable to parse root COMMANDS: {exc}"]

    scripts_dir = repo_root / "skills" / "ppt-master" / "scripts"
    scripts = find_scripts_with_main(str(scripts_dir))
    scripts.discard("check_cli_sync.py")
    scripts.discard("check_uvx_migration.py")
    scripts.discard("svg_editor/app.py")
    scripts.discard("confirm_ui/server.py")
    scripts.discard("svg_to_pptx/pptx_cli.py")
    scripts.discard("svg_to_pptx/pptx_package/cli.py")
    scripts.discard("project_management/cli.py")
    scripts.discard("svg_quality/cli.py")
    scripts = {s for s in scripts if not s.startswith("svg_finalize/")}
    missing = scripts - {normalize_script_path(path) for path in root_commands.values()}
    if missing:
        errors.append(f"ERROR: CLI COMMANDS is missing script mappings: {sorted(missing)}")
    return errors


def derive_command_name(script_path: str) -> str:
    """Derive kebab-case command name from script file path."""
    basename = os.path.splitext(os.path.basename(script_path))[0]
    basename = basename.replace("_", "-")
    dirname = os.path.dirname(script_path)
    if dirname and dirname != ".":
        dirname = dirname.replace("_", "-").replace("\\", "/")
        if dirname == "source-to-md":
            return basename
    return basename


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv

    if not os.path.exists(CLI_FILE):
        print(f"ERROR: cli.py not found at {CLI_FILE}", file=sys.stderr)
        return 1

    errors = collect_cli_sync_errors(Path(ROOT_DIR))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("OK: All scripts and complete COMMANDS/ALIASES mappings are in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
