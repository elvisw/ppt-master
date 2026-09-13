from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
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

        install = self._step(prepare, "Install pinned OpenCode CLI")
        self.assertNotIn("DEEPSEEK_API_KEY", install.get("env", {}))
        self.assertNotIn("PUSH_PAT", install.get("env", {}))
        self.assertIn("npm install -g opencode-ai@1.18.30", install["run"])
        self.assertNotIn("opencode run", install["run"])
        self.assertNotIn("npm install", model["run"])

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
        self.assertIn('uv venv --python 3.12 "$RUNNER_TEMP/ppt-master-gate-venv"', tools["run"])
        self.assertIn('GATE_PYTHON="$RUNNER_TEMP/ppt-master-gate-venv/bin/python"', tools["run"])
        self.assertIn(
            'uv pip sync --python "$GATE_PYTHON" --require-hashes --only-binary :all:',
            tools["run"],
        )
        self.assertIn('".github/release-gate-requirements.txt"', tools["run"])
        self.assertIn(
            'printf \'GATE_PYTHON=%s\\n\' "$GATE_PYTHON" >> "$GITHUB_ENV"', tools["run"]
        )
        self.assertNotIn("pip install --user", tools["run"])
        self.assertNotIn("--break-system-packages", tools["run"])
        self.assertNotIn("--system", tools["run"])
        setup_steps = [
            step
            for step in trusted["steps"]
            if isinstance(step, dict)
            and str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
        ]
        self.assertTrue(setup_steps)
        for step in setup_steps:
            self.assertEqual(step["with"]["version"], "0.12.13")
            self.assertEqual(
                step["with"]["checksum"],
                "745765a3b6e360ad76743599ae5c42e9278c7edf8bbff9fc76d05bf2623a04dd",
            )
        checker = self._step(trusted, "Run trusted candidate structure gates")
        self.assertIn('"$GATE_PYTHON" "$GITHUB_WORKSPACE/.github/scripts/check_sync_candidate.py"', checker["run"])
        verify = self._step(trusted, "Verify untrusted candidate with trusted helper")
        self.assertIn('"$GATE_PYTHON" .github/scripts/check_upstream_ancestry.py', verify["run"])
        syntax = self._step(trusted, "Run trusted syntax and F821 gates")
        self.assertIn('"$GATE_PYTHON" -I -m py_compile', syntax["run"])
        self.assertIn("check --isolated --select F821", syntax["run"])
        self.assertIn("$RUNNER_TEMP/ppt-master-gate-venv/bin/ruff", syntax["run"])
        self.assertIn("PYTHONPATH=", syntax["run"])
        self.assertIn("confirm_ui/server.py", syntax["run"])

    def test_candidate_gate_policy_is_directory_wide_and_includes_pull_workflow(self) -> None:
        helper = HELPER.read_text(encoding="utf-8")
        self.assertIn(".github/workflows/", helper)
        self.assertIn(".github/pull.yml", helper)
        self.assertIn("check_sync_candidate.py", helper)
        self.assertIn("test_sync_candidate.py", helper)

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
        self.assertIn("--jq '.base.sha'", publish["run"])
        self.assertIn("base.sha", publish["run"])
        self.assertNotIn("baseRef" + "Oid", publish["run"])
        self.assertIn("gh api", publish["run"])
        self.assertIn("gh pr close", publish["run"])
        self.assertIn('"$PUSH_URL" --delete "refs/heads/$SYNC_BRANCH"', publish["run"])
        self.assertIn("trap cleanup EXIT", publish["run"])
        self.assertIn("CLEANUP_REQUIRED", publish["run"])
        self.assertIn("GIT_TERMINAL_PROMPT=0", publish["run"])
        self.assertIn("GIT_ASKPASS=/bin/false", publish["run"])
        self.assertIn("SSH_ASKPASS=/bin/false", publish["run"])
        self.assertIn("-c credential.helper=", publish["run"])

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

    def test_publish_step_handles_create_and_base_oid_races_with_cleanup(self) -> None:
        publish = self._step(self._job("verify-and-open-pr"), "Publish verified candidate")
        self.assertIn("PR_NUMBER=", publish["run"])
        self.assertIn("--jq '.base.sha'", publish["run"])
        self.assertNotIn("baseRef" + "Oid", publish["run"])

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


def _discover_bash() -> str | None:
    configured = os.environ.get("BASH")
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    discovered = shutil.which("bash")
    if discovered:
        return discovered
    for candidate in (
        Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramW6432", "")) / "Git" / "bin" / "bash.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


class PublicationRaceSandboxTests(unittest.TestCase):
    BASE_SHA = "a" * 40
    VERIFIED_SHA = "c" * 40

    @classmethod
    def setUpClass(cls) -> None:
        workflow_data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.publish_script = next(
            step["run"]
            for step in workflow_data["jobs"]["verify-and-open-pr"]["steps"]
            if step.get("name") == "Publish verified candidate"
        )
        cls.publish_script = cls.publish_script.replace(
            "${{ needs.prepare-candidate.outputs.base_sha }}", cls.BASE_SHA
        ).replace(
            "${{ needs.prepare-candidate.outputs.target_sha }}", "b" * 40
        ).replace(
            "${{ steps.trusted-verify.outputs.verified_sha }}", cls.VERIFIED_SHA
        )
        cls.bash = _discover_bash()

    def _write_fake_tools(self, root: Path, scenario: str) -> tuple[Path, Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        log = root / "events.log"
        main_calls = root / "main-calls"
        git_script = textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -u
            printf 'git:%s\\n' "$*" >> "$FAKE_LOG"
            if [[ "$*" == *" rev-parse --verify refs/remotes/origin/main"* ]]; then
              count=0
              if [ -f "$FAKE_MAIN_CALLS" ]; then count=$(cat "$FAKE_MAIN_CALLS"); fi
              count=$((count + 1))
              printf '%s' "$count" > "$FAKE_MAIN_CALLS"
              if [ "$count" -eq 1 ]; then printf '%s\\n' "$FAKE_MAIN_FIRST"; else printf '%s\\n' "$FAKE_MAIN_SECOND"; fi
              exit 0
            fi
            if [[ "$*" == *" rev-parse --verify refs/ppt-master/untrusted-candidate"* ]]; then
              printf '%s\\n' "$FAKE_VERIFIED_SHA"
              exit 0
            fi
            if [[ "$*" == *" push "*"--delete"* ]]; then
              printf 'branch-deleted\\n' >> "$FAKE_LOG"
              exit 0
            fi
            if [[ "$*" == *" push "*"refs/heads/opencode/sync-"* ]]; then
              printf 'branch-pushed\\n' >> "$FAKE_LOG"
              exit 0
            fi
            if [[ "$*" == *" fetch "* ]]; then exit 0; fi
            exit 0
            """
        ).strip() + "\n"
        gh_script = textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -u
            printf 'gh:%s\\n' "$*" >> "$FAKE_LOG"
            if [[ "$*" == *"pr create"* ]]; then
              if [ "$FAKE_CREATE" = "fail" ]; then exit 1; fi
              printf 'https://github.com/elvisw/ppt-master/pull/42\\n'
              exit 0
            fi
            if [[ "$*" == *"api"* ]]; then
              if [[ "$*" != *"--jq .base.sha"* ]]; then exit 2; fi
              printf '%s' "$FAKE_PR_JSON" | python -c "import json,sys; value=json.load(sys.stdin).get('base', dict()).get('sha'); print(value if isinstance(value, str) else 'null')"
              exit 0
            fi
            if [[ "$*" == *"pr close"* ]]; then
              printf 'pr-closed\\n' >> "$FAKE_LOG"
              exit 0
            fi
            exit 0
            """
        ).strip() + "\n"
        (fake_bin / "git").write_text(git_script, encoding="utf-8")
        (fake_bin / "gh").write_text(gh_script, encoding="utf-8")
        os.chmod(fake_bin / "git", 0o755)
        os.chmod(fake_bin / "gh", 0o755)
        return log, main_calls

    def _run_scenario(self, scenario: str) -> tuple[subprocess.CompletedProcess[str], str]:
        if self.bash is None:
            self.skipTest("Bash is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log, main_calls = self._write_fake_tools(root, scenario)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{root / 'bin'}{os.pathsep}{env.get('PATH', '')}",
                    "FAKE_LOG": str(log),
                    "FAKE_MAIN_CALLS": str(main_calls),
                    "FAKE_MAIN_FIRST": self.BASE_SHA if scenario != "base-before-push" else "d" * 40,
                    "FAKE_MAIN_SECOND": "e" * 40 if scenario == "between-push-create" else self.BASE_SHA,
                    "FAKE_VERIFIED_SHA": self.VERIFIED_SHA,
                    "FAKE_CREATE": "fail" if scenario == "create-failure" else "ok",
                    "FAKE_PR_JSON": (
                        '{"base": {"sha": null}}'
                        if scenario == "base-sha-null"
                        else '{"base": {}}'
                        if scenario == "base-sha-missing"
                        else '{"base": {"sha": "' + "b" * 40 + '"}}'
                        if scenario == "base-sha-mismatch"
                        else '{"base": {"sha": "' + self.BASE_SHA + '"}}'
                    ),
                    "PUSH_PAT": "test-pat",
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_SERVER_URL": "https://github.com",
                    "GITHUB_REPOSITORY": "elvisw/ppt-master",
                }
            )
            fake_prefix = textwrap.dedent(
                f"""
                FAKE_LOG={shlex.quote(str(log))}
                FAKE_MAIN_CALLS={shlex.quote(str(main_calls))}
                FAKE_MAIN_FIRST={shlex.quote(env["FAKE_MAIN_FIRST"])}
                FAKE_MAIN_SECOND={shlex.quote(env["FAKE_MAIN_SECOND"])}
                FAKE_VERIFIED_SHA={shlex.quote(self.VERIFIED_SHA)}
                FAKE_CREATE={shlex.quote(env["FAKE_CREATE"])}
                FAKE_PR_JSON={shlex.quote(env["FAKE_PR_JSON"])}
                git() {{
                  printf 'git:%s\\n' "$*" >> "$FAKE_LOG"
                  if [[ "$*" == *" rev-parse --verify refs/remotes/origin/main"* ]]; then
                    count=0
                    if [ -f "$FAKE_MAIN_CALLS" ]; then count=$(cat "$FAKE_MAIN_CALLS"); fi
                    count=$((count + 1))
                    printf '%s' "$count" > "$FAKE_MAIN_CALLS"
                    if [ "$count" -eq 1 ]; then printf '%s\\n' "$FAKE_MAIN_FIRST"; else printf '%s\\n' "$FAKE_MAIN_SECOND"; fi
                    return 0
                  fi
                  if [[ "$*" == *" rev-parse --verify refs/ppt-master/untrusted-candidate"* ]]; then
                    printf '%s\\n' "$FAKE_VERIFIED_SHA"
                    return 0
                  fi
                  if [[ "$*" == *" push "*"--delete"* ]]; then
                    printf 'branch-deleted\\n' >> "$FAKE_LOG"
                    return 0
                  fi
                  if [[ "$*" == *" push "*"refs/heads/opencode/sync-"* ]]; then
                    printf 'branch-pushed\\n' >> "$FAKE_LOG"
                    return 0
                  fi
                  return 0
                }}
                gh() {{
                  printf 'gh:%s\\n' "$*" >> "$FAKE_LOG"
                  if [[ "$*" == *"pr create"* ]]; then
                    if [ "$FAKE_CREATE" = "fail" ]; then return 1; fi
                    printf 'https://github.com/elvisw/ppt-master/pull/42\\n'
                    return 0
                  fi
                  if [[ "$*" == *"api"* ]]; then
                    if [[ "$*" != *"--jq .base.sha"* ]]; then return 2; fi
                    printf '%s' "$FAKE_PR_JSON" | python -c "import json,sys; value=json.load(sys.stdin).get('base', dict()).get('sha'); print(value if isinstance(value, str) else 'null')"
                    return $?
                  fi
                  if [[ "$*" == *"pr close"* ]]; then
                    printf 'pr-closed\\n' >> "$FAKE_LOG"
                    return 0
                  fi
                  return 0
                }}
                """
            )
            result = subprocess.run(
                [self.bash, "-euo", "pipefail", "-c", fake_prefix + self.publish_script],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            return result, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_base_advance_before_push_fails_without_branch_push(self) -> None:
        result, events = self._run_scenario("base-before-push")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("branch-pushed", events)

    def test_base_advance_between_push_and_create_deletes_branch(self) -> None:
        result, events = self._run_scenario("between-push-create")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch-pushed", events)
        self.assertIn("branch-deleted", events)
        self.assertNotIn("pr create", events)

    def test_pr_base_sha_mismatch_closes_pr_and_deletes_branch(self) -> None:
        result, events = self._run_scenario("base-sha-mismatch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pr-closed", events)
        self.assertIn("branch-deleted", events)

    def test_pr_base_sha_match_completes_without_cleanup(self) -> None:
        result, events = self._run_scenario("base-sha-match")
        self.assertEqual(result.returncode, 0)
        self.assertIn("branch-pushed", events)
        self.assertIn("pr create", events)
        self.assertNotIn("pr-closed", events)
        self.assertNotIn("branch-deleted", events)

    def test_pr_base_sha_null_or_missing_fails_closed_and_cleans_branch(self) -> None:
        for scenario in ("base-sha-null", "base-sha-missing"):
            with self.subTest(scenario=scenario):
                result, events = self._run_scenario(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("pr-closed", events)
                self.assertIn("branch-deleted", events)

    def test_pr_create_failure_deletes_branch(self) -> None:
        result, events = self._run_scenario("create-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch-deleted", events)

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
                "refs/heads/main:refs/remotes/origin/main",
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
                    sys.executable,
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
                    sys.executable,
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
                        sys.executable,
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
