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


if __name__ == "__main__":
    unittest.main()
