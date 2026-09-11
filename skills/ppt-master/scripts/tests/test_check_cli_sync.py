from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "skills" / "ppt-master" / "scripts" / "check_cli_sync.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_cli_sync_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load check_cli_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    @staticmethod
    def _write_cli(path: Path, commands: dict[str, str], aliases: dict[str, str]) -> None:
        path.write_text(
            "COMMANDS = " + repr(commands) + "\n" + "ALIASES = " + repr(aliases) + "\n",
            encoding="utf-8",
        )

    def test_equal_command_keys_with_different_script_paths_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root.py"
            skill = Path(temporary) / "skill.py"
            self._write_cli(root, {"render": "render.py"}, {})
            self._write_cli(skill, {"render": "different.py"}, {})

            errors = self.module.compare_cli_mappings(root, skill)

            self.assertTrue(errors)
            self.assertTrue(any("render" in error for error in errors))
            self.assertTrue(any("render.py" in error and "different.py" in error for error in errors))

    def test_path_separator_normalization_preserves_equal_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root.py"
            skill = Path(temporary) / "skill.py"
            self._write_cli(root, {"render": "nested\\render.py"}, {})
            self._write_cli(skill, {"render": "nested/render.py"}, {})

            self.assertEqual(self.module.compare_cli_mappings(root, skill), [])

    def test_alias_values_are_compared_as_command_names_without_script_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root.py"
            skill = Path(temporary) / "skill.py"
            self._write_cli(root, {"render": "render.py"}, {"old-render": "./render"})
            self._write_cli(skill, {"render": "render.py"}, {"old-render": "render"})

            errors = self.module.compare_cli_mappings(root, skill)

            self.assertTrue(any("ALIASES mapping 'old-render' differs" in error for error in errors))

    def test_aliases_are_compared_separately_and_cannot_enter_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root.py"
            skill = Path(temporary) / "skill.py"
            self._write_cli(root, {"render": "render.py"}, {"old-render": "render"})
            self._write_cli(skill, {"render": "render.py"}, {"old-render": "other"})

            errors = self.module.compare_cli_mappings(root, skill)

            self.assertTrue(any("ALIASES" in error for error in errors))

            self._write_cli(
                root,
                {"render": "render.py", "old-render": "render.py"},
                {"old-render": "render"},
            )
            self._write_cli(
                skill,
                {"render": "render.py", "old-render": "render.py"},
                {"old-render": "render"},
            )
            errors = self.module.compare_cli_mappings(root, skill)
            self.assertTrue(any("alias" in error.lower() for error in errors))

    def test_command_and_alias_mappings_are_single_top_level_literal_assignments(self) -> None:
        snippets = (
            "COMMANDS = {'render': 'render.py'}\nCOMMANDS = {'other': 'other.py'}\n",
            "COMMANDS = {'render': 'render.py'}\nCOMMANDS.update({'other': 'other.py'})\n",
            "COMMANDS = {'render': 'render.py'}\nCOMMANDS['other'] = 'other.py'\n",
            "COMMANDS = {'render': 'render.py'}\nCOMMANDS |= {'other': 'other.py'}\n",
            "ALIASES = {'r': 'render'}\nALIASES.update({'x': 'render'})\n",
        )
        for snippet in snippets:
            with self.subTest(snippet=snippet), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "cli.py"
                path.write_text(snippet, encoding="utf-8")
                mapping_name = "ALIASES" if snippet.startswith("ALIASES") else "COMMANDS"
                with self.assertRaises(self.module.CliMappingError):
                    self.module.parse_literal_mapping(path, mapping_name)

    def test_parser_rejects_store_bindings_and_dynamic_escapes(self) -> None:
        base = "COMMANDS = {'render': 'render.py'}\n"
        snippets = (
            base + "for COMMANDS in []:\n    pass\n",
            base + "for _ in []:\n    pass\nasync def f():\n    async for COMMANDS in []:\n        pass\n",
            base + "with open('x') as COMMANDS:\n    pass\n",
            base + "try:\n    pass\nexcept Exception as COMMANDS:\n    pass\n",
            base + "values = [COMMANDS for COMMANDS in []]\n",
            base + "values = {COMMANDS for COMMANDS in []}\n",
            base + "values = (COMMANDS := {})\n",
            base + "def f(COMMANDS):\n    return COMMANDS\n",
            base + "def f():\n    global COMMANDS\n",
            base + "def f():\n    nonlocal COMMANDS\n",
            base + "del COMMANDS\n",
            base + "match value:\n    case COMMANDS:\n        pass\n",
            base + "exec('COMMANDS = {}')\n",
            base + "eval('{}')\n",
            base + "globals()['COMMANDS'] = {}\n",
            base + "locals().update({})\n",
            base + "setattr(object(), 'COMMANDS', {})\n",
            base + "compile('COMMANDS = {}', '<s>', 'exec')\n",
            base + "__import__('os').system('true')\n",
            "ALIASES = {'r': 'render'}\nsetattr(object(), 'ALIASES', {})\n",
        )
        for snippet in snippets:
            with self.subTest(snippet=snippet), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "cli.py"
                path.write_text(snippet, encoding="utf-8")
                mapping_name = "ALIASES" if snippet.startswith("ALIASES") else "COMMANDS"
                with self.assertRaises(self.module.CliMappingError):
                    self.module.parse_literal_mapping(path, mapping_name)

    def test_parser_rejects_mapping_argument_passing_and_indirect_calls(self) -> None:
        base = "COMMANDS = {'render': 'render.py'}\n"
        snippets = (
            base + "unknown_helper(COMMANDS)\n",
            base + "unknown_helper(mapping=COMMANDS)\n",
            base + "unknown_helper(*COMMANDS)\n",
            base + "unknown_helper(**COMMANDS)\n",
            base + "dict.update(COMMANDS)\n",
            base + "operator.setitem(COMMANDS, 'x', 'y')\n",
            base + "getattr(builtins, 'exec')('COMMANDS = {}')\n",
            base + "getattr(builtins, 'eval')('{}')\n",
            base + "builtins.__dict__['exec']('COMMANDS = {}')\n",
            "ALIASES = {'r': 'render'}\nunknown_helper(ALIASES)\n",
        )
        for snippet in snippets:
            with self.subTest(snippet=snippet), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "cli.py"
                path.write_text(snippet, encoding="utf-8")
                mapping_name = "ALIASES" if snippet.startswith("ALIASES") else "COMMANDS"
                with self.assertRaises(self.module.CliMappingError):
                    self.module.parse_literal_mapping(path, mapping_name)

    def test_parser_allows_readonly_mapping_reads(self) -> None:
        base = "COMMANDS = {'render': 'render.py'}\nALIASES = {'r': 'render'}\n"
        snippets = (
            base + "for key in sorted(COMMANDS):\n    pass\n",
            base + "value = COMMANDS.get('render')\n",
            base + "names = list(COMMANDS.keys())\n",
            base + "for name, path in COMMANDS.items():\n    pass\n",
            base + "value = ALIASES.get('r', 'render')\n",
            base + "size = len(COMMANDS)\n",
        )
        for snippet in snippets:
            with self.subTest(snippet=snippet), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "cli.py"
                path.write_text(snippet, encoding="utf-8")
                commands = self.module.parse_literal_mapping(path, "COMMANDS")
                self.assertEqual(commands, {"render": "render.py"})

    def test_real_cli_files_still_parse_with_strict_parser(self) -> None:
        root = Path(__file__).resolve().parents[4]
        for relative in ("cli.py", "skills/ppt-master/cli.py"):
            with self.subTest(relative=relative):
                commands = self.module.parse_literal_mapping(str(root / relative), "COMMANDS")
                aliases = self.module.parse_literal_mapping(str(root / relative), "ALIASES")
                self.assertTrue(commands)
                self.assertIsInstance(aliases, dict)


if __name__ == "__main__":
    unittest.main()
