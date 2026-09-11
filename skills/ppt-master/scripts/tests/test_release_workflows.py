from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
AUTO_TAG = ROOT / ".github" / "workflows" / "auto-tag.yml"
PUBLISH = ROOT / ".github" / "workflows" / "publish-pypi.yml"
MIGRATION = ROOT / ".github" / "workflows" / "check-uvx-migration.yml"
ACTION_RE = re.compile(r"uses:\s*([^\s#]+)@([0-9a-f]{40})\s+#\s*v[^\s]+")


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_auto_tag_is_successful_push_workflow_run_only_and_never_autofixes_main(self) -> None:
        text = AUTO_TAG.read_text(encoding="utf-8")
        yaml.safe_load(text)
        self.assertIn("workflow_run:", text)
        self.assertNotIn("workflow_dispatch:", text)
        for fragment in (
            "workflow_run.conclusion == 'success'",
            "workflow_run.event == 'push'",
            "workflow_run.head_branch == 'main'",
            "workflow_run.repository.full_name == 'elvisw/ppt-master'",
            "github.event.workflow_run.head_sha",
            "refs/tags/$RELEASE_TAG",
            "check_release_gates.py",
            "workflow-runs-json",
        ):
            self.assertIn(fragment, text)
        self.assertNotIn("workflow_dispatch", text)
        self.assertNotIn("auto_fix_uvx.py", text)
        self.assertNotIn("PUSH_PAT", text.split("- name: Push exact release tag", 1)[0])
        self.assertNotIn("id-token: write", text)
        self.assertNotIn("push origin main", text)
        self.assertNotIn("git pull --rebase", text)
        push_step = text.split("- name: Push exact release tag", 1)[1]
        self.assertIn("fetch --no-tags --prune origin main", push_step)
        self.assertIn("ls-remote --exit-code --heads origin refs/heads/main", push_step)
        self.assertIn("REMOTE_MAIN_SHA", push_step)

    def test_publish_splits_non_oidc_build_and_fresh_pypi_publish_with_static_artifact_check(self) -> None:
        text = PUBLISH.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        yaml.safe_load(MIGRATION.read_text(encoding="utf-8"))
        jobs = data["jobs"]
        self.assertIn("build-and-gate", jobs)
        self.assertIn("verify-artifact", jobs)
        self.assertIn("publish", jobs)
        self.assertIn("outputs", jobs["verify-artifact"])
        for name in ("build-and-gate", "verify-artifact"):
            self.assertNotIn("id-token", jobs[name].get("permissions", {}))
        self.assertEqual(jobs["publish"]["environment"]["name"], "pypi")
        self.assertEqual(jobs["publish"]["permissions"], {"id-token": "write"})
        for fragment in (
            "release_sha",
            "release_tag",
            "check_release_gates.py",
            "workflow-runs-json",
            "actions/workflows/check-uvx-migration.yml/runs",
            "verify_artifact_manifest",
            "runner.temp",
            "uv publish",
        ):
            self.assertIn(fragment, text)
        self.assertNotIn("uvx --from", text)
        self.assertNotIn("python -c \"import", text)
        self.assertNotIn("pip install .whl", text)
        publish_text = text.split("  publish:\n", 1)[1]
        self.assertIn("sha256sum", publish_text)
        self.assertIn("needs.verify-artifact.outputs", publish_text)
        self.assertLess(
            publish_text.index("Bind verified artifact hashes before OIDC"),
            publish_text.index("Setup uv for OIDC publication"),
        )

    def test_privileged_workflow_actions_are_immutable_sha_pins_with_release_comments(self) -> None:
        for path in (AUTO_TAG, PUBLISH, MIGRATION):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "uses:" not in line:
                    continue
                self.assertRegex(line, ACTION_RE, f"unpinned action in {path}: {line}")

    def test_migration_workflow_keeps_read_only_permissions(self) -> None:
        data = yaml.safe_load(MIGRATION.read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
