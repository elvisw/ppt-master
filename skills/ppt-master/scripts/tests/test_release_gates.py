from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".github" / "scripts" / "check_release_gates.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_release_gates_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load check_release_gates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


class ReleaseGateHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_version_and_tag_grammar_rejects_shell_metacharacters_and_noncanonical_numbers(self) -> None:
        self.module.validate_version_tag("0.1.122", "v0.1.122")
        for version, tag in (
            ("0.1.122; touch pwned", "v0.1.122; touch pwned"),
            ("01.1.2", "v01.1.2"),
            ("0.1.2", "v0.1.2/branch"),
        ):
            with self.subTest(version=version, tag=tag):
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module.validate_version_tag(version, tag)

    def test_release_identity_reads_the_declared_commit_not_mutable_worktree_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            run_git(repo, "init", "-b", "main")
            run_git(repo, "config", "user.name", "Release Test")
            run_git(repo, "config", "user.email", "release@example.invalid")
            for relative in ("pyproject.toml", "skills/ppt-master/pyproject.toml"):
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[project]\nname = 'ppt-master'\nversion = '0.1.2'\n", encoding="utf-8")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-m", "release")
            release_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
            for relative in ("pyproject.toml", "skills/ppt-master/pyproject.toml"):
                (repo / relative).write_text(
                    "[project]\nname = 'ppt-master'\nversion = '0.1.3'\n", encoding="utf-8"
                )

            with self.assertRaises(self.module.ReleaseGateError):
                self.module.validate_release_identity(repo, release_sha, "v0.1.3")

    def test_gate_rejects_a_local_yaml_shadow_that_exits_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            (repo / ".github" / "scripts").mkdir(parents=True)
            (repo / "skills" / "ppt-master").mkdir(parents=True)
            shutil.copyfile(SCRIPT, repo / ".github" / "scripts" / "check_release_gates.py")
            (repo / ".github" / "scripts" / "yaml.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8"
            )
            for relative in ("pyproject.toml", "skills/ppt-master/pyproject.toml"):
                path = repo / relative
                path.write_text(
                    "[project]\nname = 'ppt-master'\nversion = '0.1.2'\n", encoding="utf-8"
                )
            run_git(repo, "init", "-b", "main")
            run_git(repo, "config", "user.name", "Release Test")
            run_git(repo, "config", "user.email", "release@example.invalid")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-m", "shadow")
            release_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
            result = subprocess.run(
                [
                    sys.executable,
                    str(repo / ".github" / "scripts" / "check_release_gates.py"),
                    "--repo",
                    str(repo),
                    "--release-sha",
                    release_sha,
                    "--release-tag",
                    "v0.1.2",
                    "--validate-only",
                ],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_core_metadata_parser_uses_only_unique_exact_headers(self) -> None:
        self.module._validate_core_metadata(
            b"Name: ppt-master\nVersion: 0.1.2\n\nName: body-spoof\n",
            "0.1.2",
            "valid",
        )
        for metadata in (
            b"Version: 0.1.2\n\nName: ppt-master\n",
            b"Name: ppt-master\nName: ppt-master\nVersion: 0.1.2\n",
            b"Name: ppt-master\n\nVersion: 0.1.2\n",
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module._validate_core_metadata(metadata, "0.1.2", "invalid")

    def test_wheel_and_sdist_core_metadata_reject_body_and_duplicate_spoofing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, metadata in enumerate(
                (
                    "Version: 0.1.2\n\nName: ppt-master\n",
                    "Name: ppt-master\nName: ppt-master\nVersion: 0.1.2\n",
                )
            ):
                wheel = root / f"ppt_master-0.1.2-py3-none-any-{index}.whl"
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr("ppt_master-0.1.2.dist-info/METADATA", metadata)
                    for relative in (
                        "SKILL.md",
                        "LICENSE",
                        "SPONSORS.md",
                        "SPONSORS_CN.md",
                    ):
                        archive.writestr(
                            f"skills/ppt_master/{relative}", "attribution\n"
                        )
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module._validate_wheel_static(wheel, "0.1.2")

                sdist = root / f"ppt_master-0.1.2-{index}.tar.gz"
                with tarfile.open(sdist, "w:gz") as archive:
                    payload = metadata.encode("utf-8")
                    info = tarfile.TarInfo("ppt_master-0.1.2/PKG-INFO")
                    info.size = len(payload)
                    archive.addfile(info, fileobj=io.BytesIO(payload))
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module._validate_sdist_static(sdist, "0.1.2")

    def test_workflow_policy_rejects_extra_auto_tag_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            (repo / ".github" / "workflows").mkdir(parents=True)
            for relative in self.module.PRIVILEGED_WORKFLOWS:
                source = ROOT / relative
                destination = repo / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            auto = repo / ".github" / "workflows" / "auto-tag.yml"
            text = auto.read_text(encoding="utf-8")
            text = text.replace(
                "permissions:\n  contents: read\n  actions: read",
                "permissions:\n  contents: read\n  actions: read\n  pull-requests: write",
                1,
            )
            auto.write_text(text, encoding="utf-8")

            with self.assertRaises(self.module.ReleaseGateError):
                self.module._check_workflow_policy(repo)

    def test_current_privileged_workflow_policy_passes(self) -> None:
        self.module._check_workflow_policy(ROOT)

    def test_migration_workflow_policy_rejects_noop_and_bypass_shapes(self) -> None:
        mutations = (
            (
                "python -I .github/scripts/check_upstream_ancestry.py",
                "true # no-op",
            ),
            (
                "python -I skills/ppt-master/scripts/check_uvx_migration.py",
                "exit 2 # no-op",
            ),
            ("id: check\n", "id: check\n        if: always()\n"),
            ("- name: Run uvx migration check", "- name: No-op replacement"),
        )
        for original, replacement in mutations:
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary) / "repo"
                (repo / ".github" / "workflows").mkdir(parents=True)
                for relative in self.module.PRIVILEGED_WORKFLOWS:
                    destination = repo / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / relative, destination)
                migration = repo / ".github" / "workflows" / "check-uvx-migration.yml"
                migration.write_text(
                    migration.read_text(encoding="utf-8").replace(original, replacement, 1),
                    encoding="utf-8",
                )
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module._check_workflow_policy(repo)

    def test_workflow_evidence_requires_exact_successful_push_run_not_generic_check_name(self) -> None:
        release_sha = "a" * 40
        valid = {
            "workflow_runs": [
                {
                    "id": 123,
                    "name": "Check UVX Migration",
                    "path": ".github/workflows/check-uvx-migration.yml",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": release_sha,
                    "head_branch": "main",
                    "repository": {"full_name": "elvisw/ppt-master"},
                }
            ]
        }
        self.assertEqual(self.module.find_verified_workflow_run(valid, release_sha), 123)

        generic_check = {
            "workflow_runs": [
                {
                    **valid["workflow_runs"][0],
                    "path": ".github/workflows/other.yml",
                }
            ]
        }
        with self.assertRaises(self.module.ReleaseGateError):
            self.module.find_verified_workflow_run(generic_check, release_sha)

        for field, value in (
            ("name", "check"),
            ("event", "workflow_dispatch"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("head_sha", "c" * 40),
            ("head_branch", "feature"),
        ):
            with self.subTest(field=field):
                invalid = {"workflow_runs": [{**valid["workflow_runs"][0], field: value}]}
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module.find_verified_workflow_run(invalid, release_sha)

        with self.assertRaises(self.module.ReleaseGateError):
            self.module.find_verified_workflow_run({"workflow_runs": []}, release_sha)

    def test_privileged_workflow_policy_rejects_auto_tag_identity_mutations(self) -> None:
        mutations = (
            ("github.event.workflow_run.conclusion == 'success'", "github.event.workflow_run.conclusion == 'failure'"),
            ("github.event.workflow_run.event == 'push'", "github.event.workflow_run.event == 'workflow_dispatch'"),
            ("github.event.workflow_run.head_branch == 'main'", "github.event.workflow_run.head_branch == 'feature'"),
            (
                "github.event.workflow_run.repository.full_name == 'elvisw/ppt-master'",
                "github.event.workflow_run.repository.full_name == 'other/repo'",
            ),
            (
                "ref: ${{ github.event.workflow_run.head_sha }}",
                "ref: ${{ github.ref }}",
            ),
        )
        for original, replacement in mutations:
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary) / "repo"
                (repo / ".github" / "workflows").mkdir(parents=True)
                for relative in self.module.PRIVILEGED_WORKFLOWS:
                    destination = repo / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / relative, destination)
                auto = repo / ".github" / "workflows" / "auto-tag.yml"
                text = auto.read_text(encoding="utf-8").replace(original, replacement, 1)
                auto.write_text(text, encoding="utf-8")

                with self.assertRaises(self.module.ReleaseGateError):
                    self.module._check_workflow_policy(repo)

    def test_exact_tag_ref_ignores_same_named_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            run_git(repo, "init", "-b", "main")
            run_git(repo, "config", "user.name", "Release Test")
            run_git(repo, "config", "user.email", "release@example.invalid")
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            run_git(repo, "add", "file.txt")
            run_git(repo, "commit", "-m", "base")
            tag_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "file.txt").write_text("branch\n", encoding="utf-8")
            run_git(repo, "commit", "-am", "branch")
            branch_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
            run_git(repo, "branch", "v0.1.2", branch_sha)
            run_git(repo, "tag", "v0.1.2", tag_sha)

            self.module.verify_exact_tag_ref(repo, "v0.1.2", tag_sha)
            with self.assertRaises(self.module.ReleaseGateError):
                self.module.verify_exact_tag_ref(repo, "v0.1.2", branch_sha)

    def test_artifact_manifest_binds_sha_version_and_static_wheel_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact"
            artifact.mkdir()
            wheel = artifact / "ppt_master-0.1.2-py3-none-any.whl"
            sdist = artifact / "ppt_master-0.1.2.tar.gz"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "ppt_master-0.1.2.dist-info/METADATA",
                    "Name: ppt-master\nVersion: 0.1.2\n",
                )
                archive.writestr(
                    "skills/ppt_master/SKILL.md",
                    "uvx ppt-master attribution-guard\n",
                )
                archive.writestr("skills/ppt_master/LICENSE", "MIT\n")
                archive.writestr("skills/ppt_master/SPONSORS.md", "Sponsors\n")
                archive.writestr("skills/ppt_master/SPONSORS_CN.md", "Sponsors\n")
            with tarfile.open(sdist, "w:gz") as archive:
                info = tarfile.TarInfo("ppt_master-0.1.2/PKG-INFO")
                payload = b"Name: ppt-master\nVersion: 0.1.2\n"
                info.size = len(payload)
                archive.addfile(info, fileobj=io.BytesIO(payload))
                for relative, payload in {
                    "skills/ppt-master/SKILL.md": b"uvx ppt-master attribution-guard\n",
                    "skills/ppt-master/LICENSE": b"MIT\n",
                    "skills/ppt-master/SPONSORS.md": b"Sponsors\n",
                    "skills/ppt-master/SPONSORS_CN.md": b"Sponsors\n",
                }.items():
                    info = tarfile.TarInfo(f"ppt_master-0.1.2/{relative}")
                    info.size = len(payload)
                    archive.addfile(info, fileobj=io.BytesIO(payload))
            manifest = artifact / "manifest.json"
            release_sha = "b" * 40
            entries = [
                {"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
                {"filename": sdist.name, "sha256": hashlib.sha256(sdist.read_bytes()).hexdigest()},
            ]
            manifest.write_text(
                json.dumps(
                    {
                        "release_sha": release_sha,
                        "release_tag": "v0.1.2",
                        "version": "0.1.2",
                        "distributions": entries,
                    }
                ),
                encoding="utf-8",
            )

            source = root / "source"
            for relative, content in {
                "skills/ppt-master/SKILL.md": "uvx ppt-master attribution-guard\n",
                "skills/ppt-master/LICENSE": "MIT\n",
                "skills/ppt-master/SPONSORS.md": "Sponsors\n",
                "skills/ppt-master/SPONSORS_CN.md": "Sponsors\n",
            }.items():
                source_path = source / relative
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(content, encoding="utf-8")
            run_git(source, "init", "-b", "main")
            run_git(source, "config", "user.name", "Release Test")
            run_git(source, "config", "user.email", "release@example.invalid")
            run_git(source, "add", ".")
            run_git(source, "commit", "-m", "attribution source")
            release_sha = run_git(source, "rev-parse", "HEAD").stdout.strip()
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_data["release_sha"] = release_sha
            manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

            self.module.verify_artifact_manifest(
                artifact,
                manifest,
                release_sha=release_sha,
                release_tag="v0.1.2",
                version="0.1.2",
                source_root=source,
                source_sha=release_sha,
            )

            env_file = root / "artifact.env"
            self.module.emit_artifact_environment(
                artifact,
                manifest,
                env_file,
                release_sha=release_sha,
                release_tag="v0.1.2",
                version="0.1.2",
                source_root=source,
                source_sha=release_sha,
            )
            environment = env_file.read_text(encoding="utf-8")
            self.assertIn(f"wheel_name={wheel.name}\n", environment)
            self.assertIn(f"sdist_name={sdist.name}\n", environment)
            self.assertIn(f"wheel_sha256={entries[0]['sha256']}\n", environment)
            self.assertIn(f"sdist_sha256={entries[1]['sha256']}\n", environment)
            self.assertIn(
                f"manifest_sha256={hashlib.sha256(manifest.read_bytes()).hexdigest()}\n",
                environment,
            )

            for kwargs in (
                {"release_sha": "c" * 40, "release_tag": "v0.1.2", "version": "0.1.2"},
                {"release_sha": release_sha, "release_tag": "v0.1.3", "version": "0.1.3"},
                {"release_sha": release_sha, "release_tag": "v0.1.2", "version": "0.1.3"},
            ):
                with self.subTest(identity=kwargs):
                    with self.assertRaises(self.module.ReleaseGateError):
                        self.module.verify_artifact_manifest(
                            artifact,
                            manifest,
                            **kwargs,
                            source_root=source,
                            source_sha=release_sha,
                        )

            wheel_bytes = wheel.read_bytes()
            wheel.write_bytes(wheel_bytes + b"tampered")
            with self.assertRaises(self.module.ReleaseGateError):
                self.module.verify_artifact_manifest(
                    artifact,
                    manifest,
                    release_sha=release_sha,
                    release_tag="v0.1.2",
                    version="0.1.2",
                    source_root=source,
                    source_sha=release_sha,
                )
            wheel.write_bytes(wheel_bytes)

            sdist_bytes = sdist.read_bytes()
            sdist.unlink()
            with self.assertRaises(self.module.ReleaseGateError):
                self.module.verify_artifact_manifest(
                    artifact,
                    manifest,
                    release_sha=release_sha,
                    release_tag="v0.1.2",
                    version="0.1.2",
                    source_root=source,
                    source_sha=release_sha,
                )
            sdist.write_bytes(sdist_bytes)

            (artifact / "unexpected.txt").write_text("extra\n", encoding="utf-8")
            with self.assertRaises(self.module.ReleaseGateError):
                self.module.verify_artifact_manifest(
                    artifact, manifest, release_sha=release_sha, release_tag="v0.1.2", version="0.1.2"
                )

            unsafe_wheel = artifact / "ppt_master-0.1.2-py3-none-any-unsafe.whl"
            with zipfile.ZipFile(unsafe_wheel, "w") as archive:
                archive.writestr(r"C:\escape.txt", "unsafe\n")
            with self.assertRaises(self.module.ReleaseGateError):
                self.module._validate_wheel_static(unsafe_wheel, "0.1.2")


if __name__ == "__main__":
    unittest.main()
