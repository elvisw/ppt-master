from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".github" / "scripts" / "check_release_gates.py"
MIGRATION_SCRIPT = ROOT / "skills" / "ppt-master" / "scripts" / "check_uvx_migration.py"


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
            b"NAME: ppt-master\nVersion: 0.1.2\n",
            b"name: ppt-master\nVersion: 0.1.2\n",
            b"Name: ppt-master\nname: ppt-master\nVersion: 0.1.2\n",
            b"Name: ppt-master\nVersion: 0.1.2\nVERSION: 0.1.2\n",
            b"N\xe4me: ppt-master\nVersion: 0.1.2\n",
            b"Name : ppt-master\nVersion: 0.1.2\n",
            b"Name\t: ppt-master\nVersion: 0.1.2\n",
            b"Version : 0.1.2\nName: ppt-master\n",
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
                    "NAME: ppt-master\nVersion: 0.1.2\n",
                    "Name : ppt-master\nVersion: 0.1.2\n",
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
            for relative in (*self.module.PRIVILEGED_WORKFLOWS, *self.module.ADDITIONAL_PINNED_WORKFLOWS):
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

    def test_release_tooling_lock_rejects_input_or_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            for input_name, lock_name, _expected_input, _expected_digest in self.module.RELEASE_TOOLING_LOCKS:
                for relative in (input_name, lock_name):
                    destination = repo / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / relative, destination)
            self.module._check_release_tooling_lock(repo)

            lock = repo / self.module.RELEASE_TOOLING_LOCKS[0][1]
            lock.write_bytes(lock.read_bytes() + b"# drift\n")
            with self.assertRaises(self.module.ReleaseGateError):
                self.module._check_release_tooling_lock(repo)

            shutil.copyfile(ROOT / self.module.RELEASE_TOOLING_LOCKS[0][1], lock)
            input_path = repo / self.module.RELEASE_TOOLING_LOCKS[0][0]
            input_path.write_bytes(input_path.read_bytes() + b"# drift\n")
            with self.assertRaises(self.module.ReleaseGateError):
                self.module._check_release_tooling_lock(repo)

    def test_fork_python_gate_resolves_ruff_beside_gate_interpreter(self) -> None:
        fake_python = Path("C:/trusted-venv/Scripts/python.exe")
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            mock.patch.object(self.module.sys, "executable", str(fake_python)),
            mock.patch.object(self.module.Path, "is_file", return_value=True),
            mock.patch.object(self.module.subprocess, "run", side_effect=fake_run),
        ):
            self.module._run_fork_python_gates(ROOT)

        expected = fake_python.with_name("ruff.exe" if self.module.os.name == "nt" else "ruff")
        self.assertEqual(Path(calls[-1][0]), expected)

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
                for relative in (*self.module.PRIVILEGED_WORKFLOWS, *self.module.ADDITIONAL_PINNED_WORKFLOWS):
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
                for relative in (*self.module.PRIVILEGED_WORKFLOWS, *self.module.ADDITIONAL_PINNED_WORKFLOWS):
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
                    "ppt_master-0.1.2.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nGenerator: setuptools\nRoot-Is-Purelib: true\n",
                )
                archive.writestr(
                    "ppt_master-0.1.2.dist-info/RECORD",
                    "ppt_master-0.1.2.dist-info/METADATA,,\n",
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


    def _mutated_workflow_policy(self, relative: str, transform) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            (repo / ".github" / "workflows").mkdir(parents=True)
            for workflow in self.module.PRIVILEGED_WORKFLOWS:
                destination = repo / workflow
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / workflow, destination)
            path = repo / relative
            path.write_text(transform(path.read_text(encoding="utf-8")), encoding="utf-8")
            with self.assertRaises(self.module.ReleaseGateError):
                self.module._check_workflow_policy(repo)

    def test_git_write_surface_rejects_reviewer_probe_commands(self) -> None:
        module = self.module
        probes = (
            "git add -A",
            "git -c user.name=x commit -m pwn",
            "git am patch.txt",
            "git apply patch.txt",
            "git merge upstream/main",
            "git rebase main",
            "git reset --hard",
            "git update-ref refs/heads/main HEAD",
            "git cherry-pick HEAD",
            "git push origin HEAD:refs/heads/main",
            "git push --force origin main",
            "git --git-dir=.git commit -m pwn",
        )
        for probe in probes:
            with self.subTest(probe=probe):
                with self.assertRaises(module.ReleaseGateError):
                    module._check_git_write_surface(probe, "probe", allowed_pushes=0)
        module._check_git_write_surface("git fetch origin main", "clean", allowed_pushes=0)
        module._check_git_write_surface(
            'git -c credential.helper= push "https://example.invalid/repo.git" "refs/tags/$RELEASE_TAG"',
            "tag push",
            allowed_pushes=1,
        )

    def test_auto_tag_policy_rejects_extra_steps_and_main_write_probes(self) -> None:
        def _append_step(run: str):
            def transform(text: str) -> str:
                return text + f"\n      - name: Sneaky write\n        run: {run}\n"
            return transform

        probes = (
            _append_step("git -c x=y commit -m pwn"),
            _append_step("git add -A"),
            _append_step("git update-ref refs/heads/main HEAD"),
        )
        for transform in probes:
            with self.subTest(run=transform("").strip()):
                self._mutated_workflow_policy(".github/workflows/auto-tag.yml", transform)

    def test_auto_tag_policy_rejects_unapproved_push_targets_and_force(self) -> None:
        def _push_to_main(text: str) -> str:
            prefix, separator, suffix = text.rpartition('"refs/tags/$RELEASE_TAG"')
            self.assertTrue(separator)
            return prefix + '"$RELEASE_SHA:refs/heads/main"' + suffix

        def _forced_push(text: str) -> str:
            prefix, separator, suffix = text.rpartition('push "https://x-access-token:')
            self.assertTrue(separator)
            return prefix + 'push --force "https://x-access-token:' + suffix

        def _run_body_commit(text: str) -> str:
            return text.replace(
                '            --workflow-runs-json "$RUNS_FILE"',
                '            --workflow-runs-json "$RUNS_FILE"\n            git commit -am pwn',
                1,
            )

        for transform in (_push_to_main, _forced_push, _run_body_commit):
            with self.subTest(transform=transform.__name__):
                self._mutated_workflow_policy(".github/workflows/auto-tag.yml", transform)

    def test_migration_run_templates_reject_early_success_and_exit_zero_bypass(self) -> None:
        def _early_exit(text: str) -> str:
            return text.replace("          EXIT=0\n", "          exit 0\n", 1)

        def _true_prefix(text: str) -> str:
            return text.replace("          EXIT=0\n", "          true\n          EXIT=0\n", 1)

        def _set_plus_e(text: str) -> str:
            return text.replace("          EXIT=0\n", "          set +e\n          EXIT=0\n", 1)

        def _swallow_failure(text: str) -> str:
            return text.replace("|| EXIT=$?", "|| EXIT=0", 1)

        def _always_branch(text: str) -> str:
            return text.replace('if [ "$EXIT" -eq 2 ]; then', "if true; then", 1)

        for transform in (_early_exit, _true_prefix, _set_plus_e, _swallow_failure, _always_branch):
            with self.subTest(transform=transform.__name__):
                self._mutated_workflow_policy(
                    ".github/workflows/check-uvx-migration.yml", transform
                )

    def test_migration_policy_rejects_deleted_reordered_and_extra_steps(self) -> None:
        def _delete_ancestry(text: str) -> str:
            start = text.index("      - name: Verify recorded upstream ancestry")
            end = text.index("      - name: Run uvx migration check")
            return text[:start] + text[end:]

        def _reorder(text: str) -> str:
            fetch_start = text.index("      - name: Fetch upstream")
            ancestry_start = text.index("      - name: Verify recorded upstream ancestry")
            migration_start = text.index("      - name: Run uvx migration check")
            fetch_block = text[fetch_start:ancestry_start]
            ancestry_block = text[ancestry_start:migration_start]
            return text[:fetch_start] + ancestry_block + fetch_block + text[migration_start:]

        def _extra_step(text: str) -> str:
            marker = "      - name: Run uvx migration check"
            return text.replace(marker, "      - name: Extra step\n        run: true\n" + marker, 1)

        for transform in (_delete_ancestry, _reorder, _extra_step):
            with self.subTest(transform=transform.__name__):
                self._mutated_workflow_policy(
                    ".github/workflows/check-uvx-migration.yml", transform
                )

    def test_publish_oidc_policy_rejects_extra_steps_and_execution_probes(self) -> None:
        def _append_step(run: str):
            def transform(text: str) -> str:
                return text + f"\n      - name: Sneaky OIDC step\n        run: {run}\n"
            return transform

        def _replace_publish(text: str) -> str:
            return text.replace(
                'uv publish --trusted-publishing always "$WHEEL_PATH" "$SDIST_PATH"',
                'uv run python -c "import ppt_master"',
                1,
            )

        def _install_inside_bind(text: str) -> str:
            return text.replace(
                "          mapfile -t FILES",
                "          pip install requests\n          mapfile -t FILES",
                1,
            )

        probes = (
            _append_step("pip install requests"),
            _append_step("python -c 'import os'"),
            _append_step("cat $WHEEL_PATH"),
            _replace_publish,
            _install_inside_bind,
        )
        for transform in probes:
            with self.subTest(transform=getattr(transform, "__name__", "probe")):
                self._mutated_workflow_policy(".github/workflows/publish-pypi.yml", transform)

    def test_oidc_run_safety_rejects_pip_and_wheel_path_misuse(self) -> None:
        for steps in (
            ({"name": "Install deps", "run": "pip install requests"},),
            ({"name": "Import package", "run": 'python -c "import ppt_master"'},),
            ({"name": "Peek wheel", "run": 'cat "$WHEEL_PATH"'},),
            ({"name": "Execute wheel", "run": "uvx --from dist/ppt_master.whl ppt-master"},),
        ):
            with self.subTest(steps=steps):
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module._check_oidc_run_safety(list(steps), "probe")

    def test_workflow_job_envelopes_reject_injection_scopes(self) -> None:
        probes = (
            (
                ".github/workflows/check-uvx-migration.yml",
                lambda text: text.replace(
                    "    runs-on: ubuntu-latest\n    steps:",
                    "    runs-on: ubuntu-latest\n    env:\n      BASH_ENV: /tmp/evil\n    steps:",
                    1,
                ),
            ),
            (
                ".github/workflows/check-uvx-migration.yml",
                lambda text: text.replace(
                    "    runs-on: ubuntu-latest\n    steps:",
                    "    runs-on: ubuntu-latest\n    defaults:\n      run:\n        shell: bash\n    steps:",
                    1,
                ),
            ),
            (
                ".github/workflows/check-uvx-migration.yml",
                lambda text: text.replace(
                    "    runs-on: ubuntu-latest\n    steps:",
                    "    runs-on: ubuntu-latest\n    if: false\n    steps:",
                    1,
                ),
            ),
            (
                ".github/workflows/auto-tag.yml",
                lambda text: text.replace(
                    "    steps:\n      - name: Checkout immutable workflow-run commit",
                    "    env:\n      BASH_ENV: /tmp/evil\n    steps:\n"
                    "      - name: Checkout immutable workflow-run commit",
                    1,
                ),
            ),
            (
                ".github/workflows/auto-tag.yml",
                lambda text: text.replace(
                    "github.event.workflow_run.conclusion == 'success'",
                    "true",
                    1,
                ),
            ),
            (
                ".github/workflows/publish-pypi.yml",
                lambda text: text.replace(
                    "    steps:\n      - name: Checkout immutable release SHA\n",
                    "    container: ubuntu:latest\n"
                    "    steps:\n      - name: Checkout immutable release SHA\n",
                    1,
                ),
            ),
            (
                ".github/workflows/publish-pypi.yml",
                lambda text: text.replace(
                    "    steps:\n      - name: Checkout immutable release SHA for static verifier",
                    "    services:\n      db:\n        image: busybox\n"
                    "    steps:\n      - name: Checkout immutable release SHA for static verifier",
                    1,
                ),
            ),
            (
                ".github/workflows/publish-pypi.yml",
                lambda text: text.replace(
                    "\njobs:\n",
                    "\nenv:\n  BASH_ENV: /tmp/evil\njobs:\n",
                    1,
                ),
            ),
        )
        for relative, transform in probes:
            with self.subTest(relative=relative, probe=transform.__name__):
                self._mutated_workflow_policy(relative, transform)

    def test_publish_build_and_verify_step_contracts_reject_mutations(self) -> None:
        def _extra_build_step(text: str) -> str:
            return text.replace(
                "      - name: Upload only verified distributions and manifest",
                "      - name: Sneaky build step\n        run: echo x\n"
                "      - name: Upload only verified distributions and manifest",
                1,
            )

        def _renamed_build_step(text: str) -> str:
            return text.replace(
                "- name: Build distributions without OIDC",
                "- name: Build",
                1,
            )

        def _shrunk_build_run(text: str) -> str:
            return text.replace(
                'uv build --no-build-isolation --python "$GATE_PYTHON"',
                "uv build",
                1,
            )

        def _upload_with_mutation(text: str) -> str:
            return text.replace("retention-days: 7", "retention-days: 30", 1)

        def _extra_verify_step(text: str) -> str:
            return text.replace(
                "      - name: Run verify_artifact_manifest for artifact and attribution",
                "      - name: Sneaky verify step\n        run: echo x\n"
                "      - name: Run verify_artifact_manifest for artifact and attribution",
                1,
            )

        def _renamed_verify_step(text: str) -> str:
            return text.replace(
                "- name: Run verify_artifact_manifest for artifact and attribution",
                "- name: Verify",
                1,
            )

        def _verify_checkout_with_mutation(text: str) -> str:
            return text.replace("fetch-depth: 1", "fetch-depth: 0", 1)

        def _env_mutation(text: str) -> str:
            return text.replace(
                "          RELEASE_TAG: ${{ github.event_name == 'push' && github.ref_name || inputs.release_tag }}\n",
                "          RELEASE_TAG: ${{ github.event_name == 'push' && github.ref_name || inputs.release_tag }}\n"
                "          EVIL: x\n",
                1,
            )

        for transform in (
            _extra_build_step,
            _renamed_build_step,
            _shrunk_build_run,
            _upload_with_mutation,
            _extra_verify_step,
            _renamed_verify_step,
            _verify_checkout_with_mutation,
            _env_mutation,
        ):
            with self.subTest(transform=transform.__name__):
                self._mutated_workflow_policy(".github/workflows/publish-pypi.yml", transform)

    def test_wheel_metadata_path_and_companions_are_exact(self) -> None:
        version = "0.1.2"
        attribution = {
            "skills/ppt_master/SKILL.md": "uvx ppt-master attribution-guard\n",
            "skills/ppt_master/LICENSE": "MIT\n",
            "skills/ppt_master/SPONSORS.md": "Sponsors\n",
            "skills/ppt_master/SPONSORS_CN.md": "Sponsors\n",
        }

        def _write_wheel(path: Path, metadata_path: str, include: set[str]) -> None:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(metadata_path, f"Name: ppt-master\nVersion: {version}\n")
                for companion in include:
                    archive.writestr(companion, "companion\n")
                for relative, content in attribution.items():
                    archive.writestr(relative, content)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid.whl"
            _write_wheel(
                valid,
                f"ppt_master-{version}.dist-info/METADATA",
                {
                    f"ppt_master-{version}.dist-info/WHEEL",
                    f"ppt_master-{version}.dist-info/RECORD",
                },
            )
            self.module._validate_wheel_static(valid, version)

            for name, metadata_path, companions in (
                (
                    "wrong-path.whl",
                    f"other-{version}.dist-info/METADATA",
                    {
                        f"other-{version}.dist-info/WHEEL",
                        f"other-{version}.dist-info/RECORD",
                    },
                ),
                (
                    "missing-wheel.whl",
                    f"ppt_master-{version}.dist-info/METADATA",
                    {f"ppt_master-{version}.dist-info/RECORD"},
                ),
                (
                    "missing-record.whl",
                    f"ppt_master-{version}.dist-info/METADATA",
                    {f"ppt_master-{version}.dist-info/WHEEL"},
                ),
                (
                    "extra-metadata.whl",
                    f"ppt_master-{version}.dist-info/METADATA",
                    {
                        f"ppt_master-{version}.dist-info/WHEEL",
                        f"ppt_master-{version}.dist-info/RECORD",
                        f"second-{version}.dist-info/METADATA",
                    },
                ),
            ):
                with self.subTest(name=name):
                    wheel = root / name
                    _write_wheel(wheel, metadata_path, companions)
                    with self.assertRaises(self.module.ReleaseGateError):
                        self.module._validate_wheel_static(wheel, version)

    def test_release_gate_text_subprocess_paths_pin_utf8_replacement(self) -> None:
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
        for target, args in (
            ("_run_git", (Path("."), "status")),
            ("_run_migration_gate", (Path("."), "a" * 40)),
        ):
            with self.subTest(target=target), mock.patch.object(
                self.module.subprocess, "run", return_value=completed
            ) as mocked:
                getattr(self.module, target)(*args)
                self.assertEqual(mocked.call_args.kwargs.get("encoding"), "utf-8")
                self.assertEqual(mocked.call_args.kwargs.get("errors"), "replace")

    def test_migration_checker_text_subprocess_paths_pin_utf8_replacement(self) -> None:
        spec = importlib.util.spec_from_file_location("migration_under_test", MIGRATION_SCRIPT)
        if spec is None or spec.loader is None:
            raise AssertionError("unable to load check_uvx_migration.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
        with mock.patch.object(migration.subprocess, "run", return_value=completed) as mocked:
            migration.is_merge_commit("a" * 40)
        self.assertEqual(mocked.call_args.kwargs.get("encoding"), "utf-8")
        self.assertEqual(mocked.call_args.kwargs.get("errors"), "replace")


    def test_additional_trusted_workflows_pin_every_privileged_action(self) -> None:
        expected_lines = {
            ".github/workflows/sync-upstream.yml": (
                "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1",
                "uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1",
            ),
            ".github/workflows/check-upstream-ancestry.yml": (
                "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            ),
            ".github/workflows/opencode.yml": (
                "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
                "uses: anomalyco/opencode/github@77fc88c8ade8e5a620ebbe1197f3a572d29ae91a # github-v1.2.19",
            ),
        }
        for relative, lines in expected_lines.items():
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                for line in lines:
                    self.assertIn(line, text)
                self.module._check_action_pins(ROOT / relative)

    def test_pinned_workflow_pin_mutations_fail_policy(self) -> None:
        mutations = (
            (
                ".github/workflows/opencode.yml",
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
                "actions/checkout@v6",
            ),
            (
                ".github/workflows/sync-upstream.yml",
                "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1",
                "actions/upload-artifact@v4",
            ),
            (
                ".github/workflows/check-upstream-ancestry.yml",
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
                "actions/checkout@v4",
            ),
        )
        for relative, original, replacement in mutations:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary) / "repo"
                (repo / ".github" / "workflows").mkdir(parents=True)
                for workflow in (*self.module.PRIVILEGED_WORKFLOWS, *self.module.ADDITIONAL_PINNED_WORKFLOWS):
                    destination = repo / workflow
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / workflow, destination)
                target = repo / relative
                target.write_text(
                    target.read_text(encoding="utf-8").replace(original, replacement, 1),
                    encoding="utf-8",
                )
                with self.assertRaises(self.module.ReleaseGateError):
                    self.module._check_workflow_policy(repo)

    def test_entry_reconfigures_utf8_replacement_streams(self) -> None:
        class FakeStream:
            def __init__(self) -> None:
                self.kwargs: dict[str, str] | None = None

            def reconfigure(self, **kwargs: str) -> None:
                self.kwargs = kwargs

        stdout = FakeStream()
        stderr = FakeStream()
        with mock.patch.object(self.module.sys, "stdout", stdout), mock.patch.object(
            self.module.sys, "stderr", stderr
        ):
            self.module._configure_utf8_stdio()
        self.assertEqual(stdout.kwargs, {"encoding": "utf-8", "errors": "replace"})
        self.assertEqual(stderr.kwargs, {"encoding": "utf-8", "errors": "replace"})

    def test_entry_tolerates_streams_without_reconfigure(self) -> None:
        with mock.patch.object(self.module.sys, "stdout", object()), mock.patch.object(
            self.module.sys, "stderr", object()
        ):
            self.module._configure_utf8_stdio()

    def test_gbk_console_cannot_break_gate_output(self) -> None:
        script = (
            "import importlib.util,sys; "
            f"spec=importlib.util.spec_from_file_location('g', r'{SCRIPT}'); "
            "m=importlib.util.module_from_spec(spec); sys.modules['g']=m; spec.loader.exec_module(m); "
            "m._configure_utf8_stdio(); print('\\u2713 gate ok')"
        )
        env = dict(os.environ, PYTHONIOENCODING="gbk")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn(b"gate ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
