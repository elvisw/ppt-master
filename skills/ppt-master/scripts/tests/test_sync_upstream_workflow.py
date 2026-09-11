from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github" / "workflows" / "sync-upstream.yml"
OPENCODE_WORKFLOW = ROOT / ".github" / "workflows" / "opencode.yml"
HELPER = ROOT / ".github" / "scripts" / "check_upstream_ancestry.py"


class SyncUpstreamWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow_data = yaml.safe_load(cls.workflow_text)

    def _job(self, name: str) -> dict:
        return self.workflow_data["jobs"][name]

    @staticmethod
    def _step(job: dict, name: str) -> dict:
        return next(step for step in job["steps"] if step.get("name") == name)

    def test_workflow_has_non_canceling_single_sync_concurrency(self) -> None:
        concurrency = self.workflow_data["concurrency"]
        self.assertIn("sync-upstream", concurrency["group"])
        self.assertFalse(concurrency["cancel-in-progress"])

    def test_prepare_job_is_read_only_and_model_has_no_repository_credentials(self) -> None:
        prepare = self._job("prepare-candidate")
        self.assertEqual(prepare["permissions"], {"contents": "read"})
        self.assertNotIn("id-token: write", self.workflow_text)
        self.assertNotIn("contents: write", self.workflow_text)
        self.assertNotIn("pull-requests: write", self.workflow_text)
        self.assertNotIn("anomalyco/opencode", self.workflow_text)
        self.assertNotIn("@latest", self.workflow_text)
        self.assertEqual(self.workflow_text.count("opencode-ai@1.18.30"), 1)

        model_steps = [step for step in prepare["steps"] if "opencode run" in step.get("run", "")]
        self.assertEqual(len(model_steps), 1)
        model = model_steps[0]
        self.assertEqual(
            set(model.get("env", {})),
            {"DEEPSEEK_API_KEY", "OPENCODE_MODEL", "EXPECTED_UPSTREAM_SHA"},
        )
        self.assertNotIn("github.token", self.workflow_text)
        self.assertNotIn("GITHUB_TOKEN", model.get("run", "") + str(model.get("env", {})))
        self.assertNotIn("GH_TOKEN", model.get("run", "") + str(model.get("env", {})))
        self.assertNotIn("PUSH_PAT", model.get("run", "") + str(model.get("env", {})))

    def test_schedule_and_manual_use_the_same_candidate_and_publish_jobs(self) -> None:
        self.assertEqual(set(self.workflow_data["jobs"]), {"prepare-candidate", "verify-and-open-pr"})
        prepare = self._job("prepare-candidate")
        model = next(step for step in prepare["steps"] if "opencode run" in step.get("run", ""))
        self.assertEqual(model["if"], "steps.upstream.outputs.has_changes == 'true'")
        self.assertNotIn("github.event_name == 'schedule'", model["if"])
        self.assertNotIn("github.event_name == 'workflow_dispatch'", model["if"])
        self.assertIn("workflow_dispatch", self.workflow_text)
        self.assertIn("schedule", self.workflow_text)

    def test_no_change_skips_artifact_and_trusted_publication(self) -> None:
        prepare = self._job("prepare-candidate")
        upload = self._step(prepare, "Upload candidate artifact")
        self.assertEqual(upload["if"], "steps.upstream.outputs.has_changes == 'true'")
        self.assertIn("candidate.bundle", upload["with"]["path"])
        self.assertIn("manifest.json", upload["with"]["path"])
        trusted = self._job("verify-and-open-pr")
        self.assertEqual(
            trusted["if"],
            "needs.prepare-candidate.outputs.has_changes == 'true'",
        )

    def test_trusted_job_uses_base_checkout_and_rechecks_base_helper(self) -> None:
        trusted = self._job("verify-and-open-pr")
        checkout = self._step(trusted, "Checkout trusted base without credentials")
        self.assertIn("git init", checkout["run"])
        self.assertIn("fetch --no-tags --prune origin", checkout["run"])
        self.assertIn("refs/heads/main:refs/remotes/origin/main", checkout["run"])
        self.assertIn("checkout --detach \"$BASE_SHA\"", checkout["run"])
        self.assertNotIn("actions/checkout", self.workflow_text)
        self.assertIn("fetch-depth: 0", self.workflow_text)
        verify = self._step(trusted, "Verify untrusted candidate with trusted helper")
        self.assertIn(".github/scripts/check_upstream_ancestry.py", verify["run"])
        self.assertIn("--base-sha", verify["run"])
        self.assertIn("--expected-target-sha", verify["run"])
        self.assertIn("--require-version-bump", verify["run"])
        self.assertNotIn("candidate/.github/scripts", verify["run"])

    def test_trusted_job_runs_base_candidate_gates_before_publication(self) -> None:
        trusted = self._job("verify-and-open-pr")
        materialize = self._step(trusted, "Materialize candidate as data")
        self.assertIn("worktree add", materialize["run"])
        self.assertIn("read-tree", materialize["run"])
        checker = self._step(trusted, "Run trusted candidate structure gates")
        self.assertIn(".github/scripts/check_sync_candidate.py", checker["run"])
        self.assertIn("--candidate-root", checker["run"])
        self.assertIn("--base-sha", checker["run"])
        self.assertIn("--head-sha", checker["run"])
        self.assertNotIn("candidate_root/.github/scripts", checker["run"])

        tools = self._step(trusted, "Install trusted candidate gate tools")
        self.assertIn("PyYAML==", tools["run"])
        self.assertIn("ruff==", tools["run"])
        syntax = self._step(trusted, "Run trusted syntax and F821 gates")
        self.assertIn("python -I -m py_compile", syntax["run"])
        self.assertIn("ruff check --isolated --select F821", syntax["run"])
        self.assertIn("PYTHONPATH=", syntax["run"])
        self.assertIn("confirm_ui/server.py", syntax["run"])

    def test_candidate_gate_policy_is_directory_wide_and_includes_pull_workflow(self) -> None:
        helper = HELPER.read_text(encoding="utf-8")
        self.assertIn(".github/workflows/", helper)
        self.assertIn(".github/pull.yml", helper)
        self.assertIn("check_sync_candidate.py", helper)

    def test_opencode_workflow_is_pinned_without_business_trigger_changes(self) -> None:
        text = OPENCODE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("anomalyco/opencode/github@77fc88c8ade8e5a620ebbe1197f3a572d29ae91a", text)
        self.assertNotIn("anomalyco/opencode/github@latest", text)
        self.assertIn("issue_comment:", text)
        self.assertIn("pull_request_review_comment:", text)
        data = yaml.safe_load(text)
        jobs = data["jobs"]
        self.assertEqual(
            jobs["opencode"]["permissions"],
            {"id-token": "write", "contents": "read"},
        )

    def test_final_publication_pushes_verified_sha_to_unique_branch_and_creates_pr(self) -> None:
        trusted = self._job("verify-and-open-pr")
        publish = self._step(trusted, "Publish verified candidate")
        self.assertEqual(set(publish["env"]), {"PUSH_PAT"})
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", publish["run"])
        self.assertIn("GIT_CONFIG_SYSTEM=/dev/null", publish["run"])
        self.assertIn("core.hooksPath=/dev/null", publish["run"])
        self.assertIn('"$VERIFIED_SHA:refs/heads/$SYNC_BRANCH"', publish["run"])
        self.assertRegex(publish["run"], r'SYNC_BRANCH="opencode/sync-\$\{GITHUB_RUN_ID\}')
        self.assertNotIn("HEAD:refs/heads", publish["run"])
        self.assertRegex(
            publish["run"],
            r"gh pr create \\\n\s+--repo elvisw/ppt-master \\\n\s+--base main",
        )
        self.assertIn("base_sha", publish["run"])
        self.assertIn("target_sha", publish["run"])
        self.assertIn("verified_sha", publish["run"])
        self.assertIn("GITHUB_SERVER_URL", publish["run"])
        self.assertIn("main advanced before PR creation", publish["run"])

        pat_steps = [
            step
            for step in trusted["steps"]
            if "PUSH_PAT" in step.get("env", {})
        ]
        self.assertEqual([step["name"] for step in pat_steps], ["Publish verified candidate"])

    def test_main_advancement_gate_is_fail_closed_without_force_push(self) -> None:
        trusted = self._job("verify-and-open-pr")
        pre_publish = self._step(trusted, "Confirm main has not advanced")
        self.assertIn("CURRENT_MAIN_SHA", pre_publish["run"])
        self.assertIn('!= "$BASE_SHA"', pre_publish["run"])
        publish = self._step(trusted, "Publish verified candidate")
        self.assertIn("main advanced before branch publication", publish["run"])
        self.assertIn("main advanced before PR creation", publish["run"])
        self.assertNotIn("--force", publish["run"])
        self.assertNotIn("git push --force", publish["run"])

    def test_artifact_contract_is_strict_and_bundle_is_not_a_worktree_archive(self) -> None:
        prepare = self._job("prepare-candidate")
        bundle = self._step(prepare, "Create candidate bundle and manifest")
        self.assertIn("git bundle create", bundle["run"])
        self.assertIn("^$BASE_SHA", bundle["run"])
        self.assertIn("base_sha", bundle["run"])
        self.assertIn("target_sha", bundle["run"])
        self.assertIn("verified_sha", bundle["run"])
        self.assertIsNone(re.search(r"\btar\b", bundle["run"]))
        self.assertIsNone(re.search(r"\bzip\b", bundle["run"]))

        trusted = self._job("verify-and-open-pr")
        manifest = self._step(trusted, "Validate bundle manifest")
        self.assertIn("--manifest", manifest["run"])
        self.assertIn("--expected-base-sha", manifest["run"])
        self.assertIn("--expected-target-sha", manifest["run"])
        self.assertIn("--expected-verified-sha", manifest["run"])
        self.assertIn("refs/heads/opencode/sync-candidate", self._step(trusted, "Import candidate bundle")["run"])


class BundleManifestSandboxTests(unittest.TestCase):
    @staticmethod
    def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result

    def test_git_bundle_round_trip_binds_imported_candidate_to_manifest_tip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_repo = root / "candidate"
            trusted_repo = root / "trusted"
            candidate_repo.mkdir()
            trusted_repo.mkdir()
            self._run_git(candidate_repo, "init", "-b", "main")
            self._run_git(candidate_repo, "config", "user.name", "Sandbox")
            self._run_git(candidate_repo, "config", "user.email", "sandbox@example.invalid")
            (candidate_repo / "base.txt").write_text("base\n", encoding="utf-8")
            self._run_git(candidate_repo, "add", ".")
            self._run_git(candidate_repo, "commit", "-m", "base")
            base_sha = self._run_git(candidate_repo, "rev-parse", "HEAD").stdout.strip()

            self._run_git(candidate_repo, "checkout", "-b", "upstream")
            (candidate_repo / "upstream.txt").write_text("upstream\n", encoding="utf-8")
            self._run_git(candidate_repo, "add", "upstream.txt")
            self._run_git(candidate_repo, "commit", "-m", "upstream")
            target_sha = self._run_git(candidate_repo, "rev-parse", "HEAD").stdout.strip()
            self._run_git(candidate_repo, "checkout", "main")
            self._run_git(candidate_repo, "checkout", "-b", "sync")
            self._run_git(candidate_repo, "merge", "--no-ff", "--no-commit", target_sha)
            (candidate_repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            self._run_git(candidate_repo, "add", "candidate.txt")
            self._run_git(candidate_repo, "commit", "-m", "sync")
            verified_sha = self._run_git(candidate_repo, "rev-parse", "HEAD").stdout.strip()
            self._run_git(candidate_repo, "update-ref", "refs/heads/opencode/sync-candidate", verified_sha)

            bundle = root / "candidate.bundle"
            self._run_git(
                candidate_repo,
                "bundle",
                "create",
                str(bundle),
                "refs/heads/opencode/sync-candidate",
                f"^{base_sha}",
            )
            self._run_git(candidate_repo, "bundle", "verify", str(bundle))
            self._run_git(trusted_repo, "init", "-b", "main")
            self._run_git(trusted_repo, "remote", "add", "origin", str(candidate_repo))
            self._run_git(
                trusted_repo,
                "fetch",
                "origin",
                f"refs/heads/main:refs/remotes/origin/main",
            )
            self._run_git(trusted_repo, "checkout", "-b", "main", "refs/remotes/origin/main")
            self._run_git(
                trusted_repo,
                "fetch",
                str(bundle),
                "refs/heads/opencode/sync-candidate:refs/ppt-master/untrusted-candidate",
            )
            imported_sha = self._run_git(
                trusted_repo,
                "rev-parse",
                "refs/ppt-master/untrusted-candidate",
            ).stdout.strip()
            self.assertEqual(imported_sha, verified_sha)
            self.assertEqual(
                self._run_git(trusted_repo, "rev-parse", "refs/remotes/origin/main").stdout.strip(),
                base_sha,
            )

            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {"base_sha": base_sha, "target_sha": target_sha, "verified_sha": verified_sha}
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "python",
                    str(HELPER),
                    "--manifest",
                    str(manifest),
                    "--expected-base-sha",
                    base_sha,
                    "--expected-target-sha",
                    target_sha,
                    "--expected-verified-sha",
                    verified_sha,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            manifest.write_text(
                json.dumps(
                    {"base_sha": base_sha, "target_sha": target_sha, "verified_sha": "d" * 40}
                ),
                encoding="utf-8",
            )
            rejected = subprocess.run(
                [
                    "python",
                    str(HELPER),
                    "--manifest",
                    str(manifest),
                    "--expected-base-sha",
                    base_sha,
                    "--expected-target-sha",
                    target_sha,
                    "--expected-verified-sha",
                    verified_sha,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_manifest_validator_rejects_malformed_and_mismatched_values(self) -> None:
        base = "a" * 40
        target = "b" * 40
        candidate = "c" * 40
        cases = (
            {"base_sha": base, "target_sha": target},
            {"base_sha": base, "target_sha": target, "verified_sha": "not-a-sha"},
            {
                "base_sha": base,
                "target_sha": target,
                "verified_sha": candidate,
                "extra": "rejected",
            },
            {"base_sha": "d" * 40, "target_sha": target, "verified_sha": candidate},
        )
        for manifest in cases:
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "manifest.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                result = subprocess.run(
                    [
                        "python",
                        str(HELPER),
                        "--manifest",
                        str(path),
                        "--expected-base-sha",
                        base,
                        "--expected-target-sha",
                        target,
                        "--expected-verified-sha",
                        candidate,
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
