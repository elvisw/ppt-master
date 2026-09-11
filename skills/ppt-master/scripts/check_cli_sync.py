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


class CliMappingError(ValueError):
    """Represent a CLI mapping that is not one literal top-level assignment."""


_MUTATING_METHODS = frozenset({"clear", "pop", "popitem", "setdefault", "update"})
_DYNAMIC_ESCAPES = frozenset(
    {"compile", "eval", "exec", "globals", "locals", "setattr", "vars", "__import__"}
)


def _dynamic_escape_name(node: ast.AST) -> str | None:
    """Return a dangerous dynamic-execution name called by one node, if any."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id if func.id in _DYNAMIC_ESCAPES else None
    if isinstance(func, ast.Attribute):
        return func.attr if func.attr in _DYNAMIC_ESCAPES else None
    return None


def _binding_violation(
    tree: ast.AST,
    name: str,
    allowed_targets: frozenset[int],
) -> str | None:
    """Detect any Store/Del binding or dynamic escape beyond the one assignment."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            if isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in allowed_targets:
                return "is rebound or deleted outside its single literal assignment"
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            return "is bound by an except handler"
        elif isinstance(node, ast.arg) and node.arg == name:
            return "is used as a function parameter"
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return "participates in a global or nonlocal declaration"
        elif isinstance(node, ast.MatchAs) and node.name == name:
            return "is bound by a match pattern"
        elif isinstance(node, ast.MatchStar) and node.name == name:
            return "is bound by a match star pattern"
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            return "is bound by a match mapping rest"
    return None


def _assigned_names(target: ast.expr) -> set[str]:
    """Collect the plain-name targets bound by one assignment target."""
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            names.update(_assigned_names(element))
    return names


def _mutates_mapping(node: ast.AST, name: str, assignment: ast.AST | None) -> bool:
    """Detect rebinding, subscripting, or dynamic mutation of one mapping name."""
    if node is assignment:
        return False
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if name in _assigned_names(target):
                return True
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == name
            ):
                return True
        return False
    if isinstance(node, ast.AugAssign):
        target = node.target
        if isinstance(target, ast.Name) and target.id == name:
            return True
        return (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == name
        )
    if isinstance(node, ast.Delete):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return True
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == name
            ):
                return True
        return False
    if isinstance(node, ast.Call):
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == name
            and func.attr in _MUTATING_METHODS
        )
    return False


def parse_literal_mapping(cli_path: str, name: str) -> dict[str, str]:
    """Parse exactly one top-level literal mapping without dynamic mutation."""
    try:
        with open(cli_path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=cli_path)
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise CliMappingError(f"Unable to parse {cli_path}: {exc}") from exc
    assignments = [
        statement
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and (
            any(name in _assigned_names(target) for target in (
                statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            ))
            or any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == name
                for target in (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
            )
        )
    ]
    if not assignments:
        raise CliMappingError(f"{name} mapping missing from {cli_path}")
    if len(assignments) != 1:
        raise CliMappingError(f"{name} in {cli_path} must be assigned exactly once at module level")
    assignment = assignments[0]
    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
    if any(
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == name
        for target in targets
    ):
        raise CliMappingError(f"{name} in {cli_path} must not be created through subscript assignment")
    if assignment.value is None:
        raise CliMappingError(f"{name} in {cli_path} has no value")
    try:
        value = ast.literal_eval(assignment.value)
    except (SyntaxError, TypeError, ValueError) as exc:
        raise CliMappingError(f"{name} in {cli_path} is not a literal mapping") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise CliMappingError(f"{name} in {cli_path} must map strings to strings")
    allowed_targets = frozenset(
        id(node)
        for node in ast.walk(assignment)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
    )
    binding_violation = _binding_violation(tree, name, allowed_targets)
    if binding_violation is not None:
        raise CliMappingError(f"{name} in {cli_path} {binding_violation}")
    for node in ast.walk(tree):
        escape = _dynamic_escape_name(node)
        if escape is not None:
            raise CliMappingError(
                f"{cli_path} uses dynamic escape {escape!r} near the {name} mapping"
            )
    for node in ast.walk(tree):
        if _mutates_mapping(node, name, assignment):
            raise CliMappingError(f"{name} in {cli_path} is dynamically mutated")
    return value


def parse_cli_mappings(cli_path: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse complete COMMANDS and ALIASES mappings from one CLI file."""
    return (
        parse_literal_mapping(cli_path, "COMMANDS"),
        parse_literal_mapping(cli_path, "ALIASES"),
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
