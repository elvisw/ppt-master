#!/usr/bin/env python3
"""PPT Master - immutable release gate owner.

Run the complete read-only source gate set for one release commit, validate
the immutable Check UVX Migration workflow evidence, and statically bind the
distributions that may enter the OIDC publication job.  This helper is trusted
workflow code and never imports, installs, or executes a built wheel.

Usage:
    python .github/scripts/check_release_gates.py --release-sha SHA --release-tag TAG \
        --upstream-ref upstream/main --workflow-runs-json RUNS.json

Examples:
    python .github/scripts/check_release_gates.py --release-sha "$RELEASE_SHA" \
        --release-tag "$RELEASE_TAG" --upstream-ref upstream/main \
        --workflow-runs-json "$RUNS_FILE"

Dependencies:
    PyYAML and Ruff are installed by the trusted workflow gate job.
"""

from __future__ import annotations

import os
import sys

_SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))


def _path_without_script_directory(path_entries: list[str]) -> list[str]:
    """Drop the gate's own directory so a co-located module cannot shadow imports."""
    blocked = {os.path.normcase(os.path.realpath(_SCRIPT_DIRECTORY))}
    kept: list[str] = []
    for entry in path_entries:
        if not entry:
            kept.append(entry)
            continue
        candidate = os.path.normcase(os.path.realpath(entry))
        if candidate not in blocked:
            kept.append(entry)
    return kept


sys.path[:] = _path_without_script_directory(sys.path)

import argparse  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
import textwrap  # noqa: E402
import tomllib  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402
from types import ModuleType  # noqa: E402
from typing import Any  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - trusted workflow installs the pinned parser
    yaml = None


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*)|\.post(?:0|[1-9][0-9]*)|\.dev(?:0|[1-9][0-9]*))?$"
)
TAG_RE = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*)|\.post(?:0|[1-9][0-9]*)|\.dev(?:0|[1-9][0-9]*))?$"
)
CHECK_WORKFLOW_PATH = ".github/workflows/check-uvx-migration.yml"
REPOSITORY = "elvisw/ppt-master"
PRIVILEGED_WORKFLOWS = (
    ".github/workflows/auto-tag.yml",
    ".github/workflows/check-uvx-migration.yml",
    ".github/workflows/publish-pypi.yml",
)
ADDITIONAL_PINNED_WORKFLOWS = (
    ".github/workflows/sync-upstream.yml",
    ".github/workflows/check-upstream-ancestry.yml",
    ".github/workflows/opencode.yml",
)
OVERLAY_POLICY_PATH = ".github/upstream-overlay-paths.txt"
FORK_PYTHON_FILES = (
    "skills/ppt-master/scripts/confirm_ui/server.py",
    "skills/ppt-master/scripts/svg_editor/server.py",
    "skills/ppt-master/scripts/visual_review.py",
    "skills/ppt-master/scripts/server_common.py",
    "skills/ppt-master/scripts/config.py",
    "skills/ppt-master/scripts/project_management/paths.py",
    "skills/ppt-master/scripts/project_manager.py",
    "skills/ppt-master/scripts/register_template.py",
)
REQUIRED_WHEEL_SUFFIXES = (
    "skills/ppt_master/SKILL.md",
    "skills/ppt_master/LICENSE",
    "skills/ppt_master/SPONSORS.md",
    "skills/ppt_master/SPONSORS_CN.md",
)
REQUIRED_SOURCE_ATTRIBUTION_RELATIVES = tuple(
    suffix.replace("skills/ppt_master/", "skills/ppt-master/", 1)
    for suffix in REQUIRED_WHEEL_SUFFIXES
)
WHEEL_ATTRIBUTION_BASENAMES = ("SKILL.md", "LICENSE", "SPONSORS.md", "SPONSORS_CN.md")
WHEEL_ATTRIBUTION_LOCATIONS = {
    basename: (
        f"skills/ppt_master/{basename}",
        f"skills/ppt-master/{basename}",
    )
    for basename in WHEEL_ATTRIBUTION_BASENAMES
}
ACTION_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<action>[^\s#]+)@(?P<sha>[^\s#]+)\s+#\s*(?P<release>\S+)\s*$"
)
EXPECTED_ACTION_PINS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "astral-sh/setup-uv": ("c771a70e6277c0a99b617c7a806ffedaca235ff9", "v9.0.0"),
    "actions/upload-artifact": ("043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "v7.0.1"),
    "actions/download-artifact": ("3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", "v8.0.1"),
    "anomalyco/opencode/github": ("77fc88c8ade8e5a620ebbe1197f3a572d29ae91a", "github-v1.2.19"),
}
AUTO_TAG_PERMISSIONS = {"contents": "read", "actions": "read"}
MIGRATION_PERMISSIONS = {"contents": "read"}
PUBLISH_BUILD_PERMISSIONS = {"contents": "read", "actions": "read"}
PUBLISH_PERMISSIONS = {"id-token": "write"}
EXPECTED_UV_VERSION = "0.12.13"
EXPECTED_UV_CHECKSUM = "745765a3b6e360ad76743599ae5c42e9278c7edf8bbff9fc76d05bf2623a04dd"
RELEASE_TOOLING_LOCKS = (
    (
        ".github/release-gate-requirements.in",
        ".github/release-gate-requirements.txt",
        b"PyYAML==6.0.2\nruff==0.12.10\n",
        "3b4263e1978921d15a10af95d9b422dc39556ce9bb2273c3fa99a370d3349d75",
    ),
    (
        ".github/release-build-requirements.in",
        ".github/release-build-requirements.txt",
        b"PyYAML==6.0.2\nruff==0.12.10\nsetuptools==80.9.0\n",
        "f31366cdcd90a773bfbf97182aa8e6d567f11f2134bd57c25f52157ee2dae357",
    ),
)
TOOLING_ENV = {"UV_PYTHON_DOWNLOADS": "never"}
TRUSTED_SCRIPT_DIRECTORY = ".github/scripts"
TRUSTED_SCRIPT_MODULES = (
    "check_release_gates.py",
    "check_sync_candidate.py",
    "check_upstream_ancestry.py",
)
_UNSET = object()
WORKFLOW_ALLOWED_KEYS = frozenset({"name", "on", "permissions", "concurrency", "jobs"})
JOB_STRUCTURAL_KEYS = frozenset({"env", "defaults", "container", "services"})
AUTO_TAG_JOB_ALLOWED_KEYS = frozenset(
    {"runs-on", "timeout-minutes", "if", "permissions", "steps"}
)
MIGRATION_JOB_ALLOWED_KEYS = frozenset({"runs-on", "steps"})
PUBLISH_BUILD_JOB_ALLOWED_KEYS = frozenset(
    {"runs-on", "timeout-minutes", "permissions", "outputs", "steps"}
)
PUBLISH_VERIFY_JOB_ALLOWED_KEYS = frozenset(
    {"needs", "runs-on", "timeout-minutes", "permissions", "outputs", "steps"}
)
PUBLISH_OIDC_JOB_ALLOWED_KEYS = frozenset(
    {"needs", "runs-on", "timeout-minutes", "environment", "permissions", "steps"}
)


def _template(text: str) -> str:
    """Return one approved run block as canonical normalized template text."""
    return textwrap.dedent(text).strip("\n")


USES_CHECKOUT = "actions/checkout@" + EXPECTED_ACTION_PINS["actions/checkout"][0]
USES_SETUP_UV = "astral-sh/setup-uv@" + EXPECTED_ACTION_PINS["astral-sh/setup-uv"][0]
USES_UPLOAD_ARTIFACT = "actions/upload-artifact@" + EXPECTED_ACTION_PINS["actions/upload-artifact"][0]
USES_DOWNLOAD_ARTIFACT = (
    "actions/download-artifact@" + EXPECTED_ACTION_PINS["actions/download-artifact"][0]
)

AUTO_TAG_FETCH_RUN = _template(
    r"""
    set -euo pipefail
    git -c credential.helper= -c core.hooksPath=/dev/null fetch --no-tags --prune origin main
    git remote add upstream https://github.com/hugohe3/ppt-master.git
    git fetch --no-tags upstream main
    if [ "$(git rev-parse HEAD)" != "$RELEASE_SHA" ]; then
      echo "::error::workflow_run checkout is not the immutable head SHA"
      exit 1
    fi
    if ! git merge-base --is-ancestor "$RELEASE_SHA" refs/remotes/origin/main; then
      echo "::error::release SHA is not in current origin/main history"
      exit 1
    fi
    """
)
AUTO_TAG_PARSE_RUN = _template(
    r"""
    set -euo pipefail
    "$GATE_PYTHON" -I .github/scripts/check_release_gates.py \
      --repo "$GITHUB_WORKSPACE" \
      --release-sha "$RELEASE_SHA" \
      --emit-release-env \
      --env-file "$GITHUB_ENV"
    """
)
AUTO_TAG_QUERY_RUN = _template(
    r"""
    set -euo pipefail
    RUNS_FILE="$RUNNER_TEMP/check-uvx-migration-runs.json"
    gh api --paginate --slurp \
      "repos/elvisw/ppt-master/actions/workflows/check-uvx-migration.yml/runs?event=push&head_sha=$RELEASE_SHA&status=completed&per_page=100" \
      > "$RUNS_FILE"
    printf 'RUNS_FILE=%s\n' "$RUNS_FILE" >> "$GITHUB_ENV"
    """
)
AUTO_TAG_TOOLING_RUN = _template(
    r"""
    set -euo pipefail
    uv venv --python 3.12 "$RUNNER_TEMP/ppt-master-gate-venv"
    GATE_PYTHON="$RUNNER_TEMP/ppt-master-gate-venv/bin/python"
    uv pip sync --python "$GATE_PYTHON" --require-hashes --only-binary :all: --no-progress ".github/release-gate-requirements.txt"
    printf 'GATE_PYTHON=%s\n' "$GATE_PYTHON" >> "$GITHUB_ENV"
    """
)
AUTO_TAG_GATE_RUN = _template(
    r"""
    set -euo pipefail
    "$GATE_PYTHON" -I .github/scripts/check_release_gates.py \
      --repo "$GITHUB_WORKSPACE" \
      --release-sha "$RELEASE_SHA" \
      --release-tag "$RELEASE_TAG" \
      --upstream-ref upstream/main \
      --workflow-runs-json "$RUNS_FILE"
    """
)
AUTO_TAG_PUSH_RUN = _template(
    r"""
    set -euo pipefail
    if [ -z "${PUSH_PAT:-}" ]; then
      echo "::error::PUSH_PAT is required for the final tag push"
      exit 1
    fi
    git -c credential.helper= -c core.hooksPath=/dev/null fetch --no-tags --prune origin main
    if [ "$(git rev-parse HEAD)" != "$RELEASE_SHA" ]; then
      echo "::error::workflow_run checkout is not the immutable release SHA"
      exit 1
    fi
    if ! git merge-base --is-ancestor "$RELEASE_SHA" refs/remotes/origin/main; then
      echo "::error::release SHA is not in current origin/main history"
      exit 1
    fi
    if ! REMOTE_MAIN_SHA=$(git -c credential.helper= -c core.hooksPath=/dev/null ls-remote --exit-code --heads origin refs/heads/main | awk 'NF == 2 { print $1 }'); then
      echo "::error::Unable to read the current origin/main tip"
      exit 1
    fi
    echo "Recorded origin/main at tag time: $REMOTE_MAIN_SHA"
    if [ -z "$REMOTE_MAIN_SHA" ]; then
      echo "::error::origin/main tip is empty at tag time"
      exit 1
    fi
    if git -c credential.helper= -c core.hooksPath=/dev/null ls-remote --exit-code --refs origin "refs/tags/$RELEASE_TAG" >/dev/null 2>&1; then
      echo "::error::Exact release tag already exists on origin"
      exit 1
    fi
    git -c user.name="github-actions[bot]" \
      -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
      -c core.hooksPath=/dev/null \
      tag -a "$RELEASE_TAG" "$RELEASE_SHA" -m "Release $RELEASE_TAG"
    if [ "$(git rev-parse "refs/tags/$RELEASE_TAG^{commit}")" != "$RELEASE_SHA" ]; then
      echo "::error::Local exact tag does not point to release_sha"
      exit 1
    fi
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    GIT_ASKPASS=/bin/false \
    SSH_ASKPASS=/bin/false \
    git -c credential.helper= -c core.hooksPath=/dev/null \
      push "https://x-access-token:${PUSH_PAT}@github.com/elvisw/ppt-master.git" \
      "refs/tags/$RELEASE_TAG"
    """
)

MIGRATION_FETCH_RUN = _template(
    r"""
    git remote add upstream https://github.com/hugohe3/ppt-master.git
    git fetch --no-tags upstream main
    """
)
MIGRATION_ANCESTRY_RUN = _template(
    r"""
    HEAD_SHA=$(git rev-parse HEAD)
    python -I .github/scripts/check_upstream_ancestry.py \
      --head-sha "$HEAD_SHA" \
      --upstream-ref upstream/main
    """
)
MIGRATION_CHECK_RUN = _template(
    r"""
    EXIT=0
    python -I skills/ppt-master/scripts/check_uvx_migration.py "${{ github.sha }}" || EXIT=$?
    echo "exit_code=$EXIT" >> $GITHUB_OUTPUT
    if [ "$EXIT" -eq 2 ]; then
      echo "::notice::Not a merge commit — skipped (no upstream code to check)"
    elif [ "$EXIT" -ne 0 ]; then
      echo "::error::legacy interpreter remnants found in merge commit — uvx adaptation incomplete"
      exit 1
    fi
    """
)

PUBLISH_BIND_RUN = _template(
    r"""
    set -euo pipefail
    for name in "$VERIFIED_WHEEL_NAME" "$VERIFIED_SDIST_NAME"; do
      case "$name" in
        ""|*[!A-Za-z0-9_.-]*)
          echo "::error::Verifier returned an unsafe distribution filename"
          exit 1
          ;;
      esac
    done
    for digest in "$VERIFIED_WHEEL_SHA256" "$VERIFIED_SDIST_SHA256" "$VERIFIED_MANIFEST_SHA256"; do
      if [ "${#digest}" -ne 64 ]; then
        echo "::error::Verifier returned an invalid SHA-256 digest"
        exit 1
      fi
      case "$digest" in
        ""|*[!0-9a-f]*)
          echo "::error::Verifier returned an invalid SHA-256 digest"
          exit 1
          ;;
      esac
    done
    if [ "$VERIFIED_WHEEL_NAME" = "$VERIFIED_SDIST_NAME" ]; then
      echo "::error::Verifier returned duplicate distribution filenames"
      exit 1
    fi
    WHEEL_PATH="$ARTIFACT_DIR/$VERIFIED_WHEEL_NAME"
    SDIST_PATH="$ARTIFACT_DIR/$VERIFIED_SDIST_NAME"
    MANIFEST_PATH="$ARTIFACT_DIR/manifest.json"
    for path in "$WHEEL_PATH" "$SDIST_PATH" "$MANIFEST_PATH"; do
      if [ ! -f "$path" ] || [ -L "$path" ]; then
        echo "::error::Verified artifact path is missing or not regular"
        exit 1
      fi
    done
    mapfile -t FILES < <(find "$ARTIFACT_DIR" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
    EXPECTED_FILES=("$VERIFIED_WHEEL_NAME" "$VERIFIED_SDIST_NAME" "manifest.json")
    mapfile -t EXPECTED_SORTED < <(printf '%s\n' "${EXPECTED_FILES[@]}" | sort)
    if [ "${#FILES[@]}" -ne 3 ] || [ "${FILES[*]}" != "${EXPECTED_SORTED[*]}" ]; then
      echo "::error::The downloaded artifact contains missing or unexpected files"
      exit 1
    fi
    if [ "$(sha256sum -- "$WHEEL_PATH" | cut -d ' ' -f1)" != "$VERIFIED_WHEEL_SHA256" ] ||
       [ "$(sha256sum -- "$SDIST_PATH" | cut -d ' ' -f1)" != "$VERIFIED_SDIST_SHA256" ] ||
       [ "$(sha256sum -- "$MANIFEST_PATH" | cut -d ' ' -f1)" != "$VERIFIED_MANIFEST_SHA256" ]; then
      echo "::error::Downloaded artifact bytes do not match the static verifier outputs"
      exit 1
    fi
    printf 'WHEEL_PATH=%s\nSDIST_PATH=%s\n' "$WHEEL_PATH" "$SDIST_PATH" >> "$GITHUB_ENV"
    """
)

PUBLISH_OIDC_BIND_ENV = {
    "ARTIFACT_DIR": "${{ runner.temp }}/release-artifact",
    "VERIFIED_WHEEL_NAME": "${{ needs.verify-artifact.outputs.wheel_name }}",
    "VERIFIED_SDIST_NAME": "${{ needs.verify-artifact.outputs.sdist_name }}",
    "VERIFIED_WHEEL_SHA256": "${{ needs.verify-artifact.outputs.wheel_sha256 }}",
    "VERIFIED_SDIST_SHA256": "${{ needs.verify-artifact.outputs.sdist_sha256 }}",
    "VERIFIED_MANIFEST_SHA256": "${{ needs.verify-artifact.outputs.manifest_sha256 }}",
}

class WorkflowStepContract:
    """Describe one approved privileged-workflow step."""

    __slots__ = ("name", "keys", "uses", "with_values", "env", "run_template", "step_id")

    def __init__(
        self,
        name: str | None,
        keys: frozenset[str],
        uses: str | None = None,
        with_values: dict[str, Any] | None = None,
        env: dict[str, Any] | None = None,
        run_template: str | None = None,
        step_id: str | None = None,
    ) -> None:
        self.name = name
        self.keys = keys
        self.uses = uses
        self.with_values = with_values
        self.env = env
        self.run_template = run_template
        self.step_id = step_id


DISPATCH_SHA = "${{ github.event_name == 'push' && github.sha || inputs.release_sha }}"
DISPATCH_TAG = "${{ github.event_name == 'push' && github.ref_name || inputs.release_tag }}"
PUBLISH_IDENTITY_ENV = {"RELEASE_SHA": DISPATCH_SHA, "RELEASE_TAG": DISPATCH_TAG}
PUBLISH_QUERY_ENV = {"GH_TOKEN": "${{ github.token }}", "RELEASE_SHA": DISPATCH_SHA}
AUTO_TAG_JOB_CONDITION = (
    "github.event_name == 'workflow_run' && "
    "github.event.workflow_run.conclusion == 'success' && "
    "github.event.workflow_run.event == 'push' && "
    "github.event.workflow_run.head_branch == 'main' && "
    "github.event.workflow_run.repository.full_name == 'elvisw/ppt-master'"
)

PUBLISH_VALIDATE_RUN = _template(
    r"""
    set -euo pipefail
    python -I .github/scripts/check_release_gates.py \
      --repo "$GITHUB_WORKSPACE" \
      --release-sha "$RELEASE_SHA" \
      --release-tag "$RELEASE_TAG" \
      --validate-only
    if [ "$GITHUB_EVENT_NAME" = "push" ] || [ "$GITHUB_EVENT_NAME" = "workflow_dispatch" ]; then
      if [ "$GITHUB_REF" != "refs/tags/$RELEASE_TAG" ]; then
        echo "::error::Publication must be dispatched from the exact release tag ref"
        exit 1
      fi
    fi
    """
)
PUBLISH_FETCH_RUN = _template(
    r"""
    set -euo pipefail
    git fetch --no-tags --prune origin main
    git fetch --no-tags origin "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
    git remote add upstream https://github.com/hugohe3/ppt-master.git
    git fetch --no-tags upstream main
    if [ "$(git rev-parse HEAD)" != "$RELEASE_SHA" ]; then
      echo "::error::Checkout is not release_sha"
      exit 1
    fi
    if [ "$(git rev-parse "refs/tags/$RELEASE_TAG^{commit}")" != "$RELEASE_SHA" ]; then
      echo "::error::Exact tag ref does not point to release_sha"
      exit 1
    fi
    """
)
PUBLISH_QUERY_RUN = _template(
    r"""
    set -euo pipefail
    gh api --paginate --slurp \
      "repos/elvisw/ppt-master/actions/workflows/check-uvx-migration.yml/runs?event=push&head_sha=$RELEASE_SHA&status=completed&per_page=100" \
      > "$RUNNER_TEMP/check-uvx-migration-runs.json"
    """
)
PUBLISH_TOOLING_RUN = _template(
    r"""
    set -euo pipefail
    uv venv --python 3.12 "$RUNNER_TEMP/ppt-master-gate-venv"
    GATE_PYTHON="$RUNNER_TEMP/ppt-master-gate-venv/bin/python"
    uv pip sync --python "$GATE_PYTHON" --require-hashes --only-binary :all: --no-progress ".github/release-build-requirements.txt"
    printf 'GATE_PYTHON=%s\n' "$GATE_PYTHON" >> "$GITHUB_ENV"
    """
)
PUBLISH_GATE_RUN = _template(
    r"""
    set -euo pipefail
    "$GATE_PYTHON" -I .github/scripts/check_release_gates.py \
      --repo "$GITHUB_WORKSPACE" \
      --release-sha "$RELEASE_SHA" \
      --release-tag "$RELEASE_TAG" \
      --upstream-ref upstream/main \
      --workflow-runs-json "$RUNNER_TEMP/check-uvx-migration-runs.json" \
      --require-existing-tag
    """
)
PUBLISH_BUILD_RUN = _template(
    r"""
    set -euo pipefail
    rm -rf dist
    uv build --no-build-isolation --python "$GATE_PYTHON"
    if ! git diff --quiet || ! git diff --cached --quiet; then
      echo "::error::The build modified tracked release sources"
      exit 1
    fi
    """
)
PUBLISH_STAGE_RUN = _template(
    r"""
    set -euo pipefail
    ARTIFACT_DIR="$RUNNER_TEMP/release-artifact"
    rm -rf "$ARTIFACT_DIR"
    mkdir -p "$ARTIFACT_DIR"
    cp dist/*.whl dist/*.tar.gz "$ARTIFACT_DIR/"
    python -I .github/scripts/check_release_gates.py \
      --repo "$GITHUB_WORKSPACE" \
      --release-sha "$RELEASE_SHA" \
      --release-tag "$RELEASE_TAG" \
      --artifact-dir "$ARTIFACT_DIR" \
      --write-artifact-manifest "$ARTIFACT_DIR/manifest.json"
    """
)
PUBLISH_VERIFY_RUN = _template(
    r"""
    set -euo pipefail
    python -I .github/scripts/check_release_gates.py \
      --repo "$GITHUB_WORKSPACE" \
      --release-sha "$RELEASE_SHA" \
      --release-tag "$RELEASE_TAG" \
      --artifact-dir "$RUNNER_TEMP/release-artifact" \
      --verify-artifact-manifest "$RUNNER_TEMP/release-artifact/manifest.json" \
      --emit-artifact-env "$GITHUB_OUTPUT"
    """
)

PUBLISH_BUILD_STEP_CONTRACTS = (
    WorkflowStepContract(
        name="Checkout immutable release SHA",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_CHECKOUT,
        with_values={"ref": DISPATCH_SHA, "fetch-depth": 0, "persist-credentials": False},
    ),
    WorkflowStepContract(
        name="Validate canonical manual or tag identity",
        keys=frozenset({"name", "env", "run"}),
        env=PUBLISH_IDENTITY_ENV,
        run_template=PUBLISH_VALIDATE_RUN,
    ),
    WorkflowStepContract(
        name="Fetch and bind exact release refs",
        keys=frozenset({"name", "env", "run"}),
        env=PUBLISH_IDENTITY_ENV,
        run_template=PUBLISH_FETCH_RUN,
    ),
    WorkflowStepContract(
        name="Query immutable Check UVX Migration run evidence",
        keys=frozenset({"name", "env", "run"}),
        env=PUBLISH_QUERY_ENV,
        run_template=PUBLISH_QUERY_RUN,
    ),
    WorkflowStepContract(
        name="Setup uv for trusted gate and build tooling",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_SETUP_UV,
        with_values={
            "version": EXPECTED_UV_VERSION,
            "checksum": EXPECTED_UV_CHECKSUM,
            "enable-cache": False,
        },
    ),
    WorkflowStepContract(
        name="Install pinned trusted gate tooling",
        keys=frozenset({"name", "env", "run"}),
        env=TOOLING_ENV,
        run_template=PUBLISH_TOOLING_RUN,
    ),
    WorkflowStepContract(
        name="Rerun complete release gates for exact tag and SHA",
        keys=frozenset({"name", "env", "run"}),
        env=PUBLISH_IDENTITY_ENV,
        run_template=PUBLISH_GATE_RUN,
    ),
    WorkflowStepContract(
        name="Build distributions without OIDC",
        keys=frozenset({"name", "run"}),
        run_template=PUBLISH_BUILD_RUN,
    ),
    WorkflowStepContract(
        name="Stage and statically bind exact distributions",
        keys=frozenset({"name", "env", "run"}),
        env=PUBLISH_IDENTITY_ENV,
        run_template=PUBLISH_STAGE_RUN,
    ),
    WorkflowStepContract(
        name="Upload only verified distributions and manifest",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_UPLOAD_ARTIFACT,
        with_values={
            "name": "release-distributions-${{ github.run_id }}-${{ github.run_attempt }}",
            "path": "${{ runner.temp }}/release-artifact",
            "if-no-files-found": "error",
            "retention-days": 7,
        },
    ),
)

PUBLISH_VERIFY_STEP_CONTRACTS = (
    WorkflowStepContract(
        name="Checkout immutable release SHA for static verifier",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_CHECKOUT,
        with_values={"ref": DISPATCH_SHA, "fetch-depth": 1, "persist-credentials": False},
    ),
    WorkflowStepContract(
        name="Download exact build artifact to runner temp",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_DOWNLOAD_ARTIFACT,
        with_values={
            "name": "${{ needs.build-and-gate.outputs.artifact_name }}",
            "path": "${{ runner.temp }}/release-artifact",
        },
    ),
    WorkflowStepContract(
        name="Run verify_artifact_manifest for artifact and attribution",
        keys=frozenset({"name", "id", "env", "run"}),
        env=PUBLISH_IDENTITY_ENV,
        run_template=PUBLISH_VERIFY_RUN,
        step_id="verify",
    ),
)

GIT_SUBCOMMAND_RE = re.compile(
    r"(?<![\w.-])git\s+"
    r"(?P<options>(?:(?:-c|-C|--exec-path|--git-dir|--work-tree|--namespace)\s+\S+\s+"
    r"|--[A-Za-z][A-Za-z-]*(?:=\S+)?\s+)*)"
    r"(?P<subcommand>[A-Za-z][\w-]*)"
)
FORBIDDEN_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "cherry-pick",
        "commit",
        "merge",
        "rebase",
        "reset",
        "update-ref",
    }
)
OIDC_FORBIDDEN_RUN_PATTERNS = (
    r"(?<![\w-])pip\b",
    r"(?<![\w-])python3?\b",
    r"(?<![\w-])import\b",
    r"(?<![\w-])exec\s*\(",
    r"(?<![\w-])eval\s*\(",
    r"(?<![\w-])uvx\b",
    r"\.whl\b",
)


AUTO_TAG_STEP_CONTRACTS = (
    WorkflowStepContract(
        name="Checkout immutable workflow-run commit",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_CHECKOUT,
        with_values={
            "ref": "${{ github.event.workflow_run.head_sha }}",
            "fetch-depth": 0,
            "persist-credentials": False,
        },
    ),
    WorkflowStepContract(
        name="Fetch immutable release refs",
        keys=frozenset({"name", "env", "run"}),
        env={"RELEASE_SHA": "${{ github.event.workflow_run.head_sha }}"},
        run_template=AUTO_TAG_FETCH_RUN,
    ),
    WorkflowStepContract(
        name="Setup uv for trusted gate tooling",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_SETUP_UV,
        with_values={
            "version": EXPECTED_UV_VERSION,
            "checksum": EXPECTED_UV_CHECKSUM,
            "enable-cache": False,
        },
    ),
    WorkflowStepContract(
        name="Install pinned trusted gate tooling",
        keys=frozenset({"name", "env", "run"}),
        env=TOOLING_ENV,
        run_template=AUTO_TAG_TOOLING_RUN,
    ),
    WorkflowStepContract(
        name="Parse and bind package version through environment",
        keys=frozenset({"name", "env", "run"}),
        env={"RELEASE_SHA": "${{ github.event.workflow_run.head_sha }}"},
        run_template=AUTO_TAG_PARSE_RUN,
    ),
    WorkflowStepContract(
        name="Query immutable Check UVX Migration run evidence",
        keys=frozenset({"name", "env", "run"}),
        env={
            "GH_TOKEN": "${{ github.token }}",
            "RELEASE_SHA": "${{ github.event.workflow_run.head_sha }}",
        },
        run_template=AUTO_TAG_QUERY_RUN,
    ),
    WorkflowStepContract(
        name="Run complete release gates for exact SHA",
        keys=frozenset({"name", "env", "run"}),
        env={"RELEASE_SHA": "${{ github.event.workflow_run.head_sha }}"},
        run_template=AUTO_TAG_GATE_RUN,
    ),
    WorkflowStepContract(
        name="Push exact release tag",
        keys=frozenset({"name", "env", "run"}),
        env={
            "PUSH_PAT": "${{ secrets.PUSH_PAT }}",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
        },
        run_template=AUTO_TAG_PUSH_RUN,
    ),
)

MIGRATION_STEP_CONTRACTS = (
    WorkflowStepContract(
        name=None,
        keys=frozenset({"uses", "with"}),
        uses=USES_CHECKOUT,
        with_values={"fetch-depth": 0, "persist-credentials": False},
    ),
    WorkflowStepContract(
        name="Fetch upstream",
        keys=frozenset({"name", "run"}),
        run_template=MIGRATION_FETCH_RUN,
    ),
    WorkflowStepContract(
        name="Verify recorded upstream ancestry",
        keys=frozenset({"name", "run"}),
        run_template=MIGRATION_ANCESTRY_RUN,
    ),
    WorkflowStepContract(
        name="Run uvx migration check",
        keys=frozenset({"name", "id", "run"}),
        run_template=MIGRATION_CHECK_RUN,
        step_id="check",
    ),
)

PUBLISH_OIDC_STEP_CONTRACTS = (
    WorkflowStepContract(
        name="Download verified artifact to fresh runner temp",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_DOWNLOAD_ARTIFACT,
        with_values={
            "name": "${{ needs.verify-artifact.outputs.artifact_name }}",
            "path": "${{ runner.temp }}/release-artifact",
        },
    ),
    WorkflowStepContract(
        name="Bind verified artifact hashes before OIDC",
        keys=frozenset({"name", "env", "run"}),
        env=PUBLISH_OIDC_BIND_ENV,
        run_template=PUBLISH_BIND_RUN,
    ),
    WorkflowStepContract(
        name="Setup uv for OIDC publication",
        keys=frozenset({"name", "uses", "with"}),
        uses=USES_SETUP_UV,
        with_values={
            "version": EXPECTED_UV_VERSION,
            "checksum": EXPECTED_UV_CHECKSUM,
            "enable-cache": False,
        },
    ),
    WorkflowStepContract(
        name="Publish only statically verified paths through PyPI trusted publishing",
        keys=frozenset({"name", "run"}),
        run_template='uv publish --trusted-publishing always "$WHEEL_PATH" "$SDIST_PATH"',
    ),
)


class ReleaseGateError(RuntimeError):
    """Represent a fail-closed release gate failure."""


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, UnicodeError) as exc:
        raise ReleaseGateError("Unable to execute Git for the release gate") from exc


def _git_output(repo: Path, *args: str) -> str:
    result = _run_git(repo, *args)
    if result.returncode != 0:
        raise ReleaseGateError(f"Git command failed: git {' '.join(args)}")
    return result.stdout.strip()


def _ensure_release_head(repo: Path, release_sha: str) -> None:
    """Require the checked-out worktree HEAD to be the declared release object."""
    actual = _git_output(repo, "rev-parse", "--verify", "HEAD")
    if actual != release_sha:
        raise ReleaseGateError("Checked-out HEAD does not equal release_sha")


def _ensure_clean_checkout(repo: Path) -> None:
    """Reject source gates running against modified or untracked repository data."""
    for args, label in (
        (("diff", "--quiet"), "working-tree changes"),
        (("diff", "--cached", "--quiet"), "staged changes"),
    ):
        result = _run_git(repo, *args)
        if result.returncode != 0:
            if result.returncode == 1:
                raise ReleaseGateError(f"Release checkout contains {label}")
            raise ReleaseGateError(f"Unable to inspect release checkout {label}")
    status = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        raise ReleaseGateError("Unable to inspect release checkout status")
    if status.stdout.strip():
        raise ReleaseGateError("Release checkout contains untracked files")


def _validate_trusted_script_directory(repo: Path) -> None:
    """Reject shadow modules next to the trusted release gate files."""
    directory = repo / TRUSTED_SCRIPT_DIRECTORY
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseGateError("Trusted release gate directory is unavailable")
    entries = {entry.name: entry for entry in directory.iterdir()}
    for name, entry in entries.items():
        if name == "__pycache__" and entry.is_dir() and not entry.is_symlink():
            continue
        if name in TRUSTED_SCRIPT_MODULES and not entry.is_symlink():
            continue
        raise ReleaseGateError(
            f"Trusted release gate directory contains an unexpected entry: {name}"
        )
    for name in TRUSTED_SCRIPT_MODULES:
        entry = entries.get(name)
        if entry is None or entry.is_symlink() or not stat.S_ISREG(os.lstat(entry).st_mode):
            raise ReleaseGateError(f"Trusted release gate module is unavailable: {name}")


def _load_trusted_module(repo: Path, relative: str, name: str) -> ModuleType:
    path = repo / relative
    if path.is_symlink() or not path.is_file():
        raise ReleaseGateError(f"Trusted release gate file is unavailable: {relative}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReleaseGateError(f"Unable to load trusted release gate file: {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _validate_sha(value: str, label: str) -> None:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ReleaseGateError(f"{label} must be a 40-character lowercase SHA")


def validate_version_tag(version: str, release_tag: str) -> None:
    """Require a canonical safe package version and its exact v-prefixed tag."""
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise ReleaseGateError("Package version is not canonical or safe")
    if not isinstance(release_tag, str) or TAG_RE.fullmatch(release_tag) is None:
        raise ReleaseGateError("Release tag is not canonical or safe")
    if release_tag != f"v{version}":
        raise ReleaseGateError("Release tag does not equal the package version")


def _read_version(repo: Path, relative: str, label: str, commit_sha: str | None = None) -> str:
    """Read a package version from a file or an immutable commit object."""
    try:
        if commit_sha is None:
            with (repo / relative).open("rb") as handle:
                data = tomllib.load(handle)
        else:
            result = _run_git(repo, "show", f"{commit_sha}:{relative}")
            if result.returncode != 0:
                raise ReleaseGateError(f"Unable to read {label} package manifest from release_sha")
            data = tomllib.loads(result.stdout)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseGateError(f"Unable to parse {label} package manifest") from exc
    try:
        version = data["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise ReleaseGateError(f"{label} package version is missing") from exc
    if not isinstance(version, str):
        raise ReleaseGateError(f"{label} package version is not a string")
    return version


def validate_release_identity(repo: Path, release_sha: str, release_tag: str) -> str:
    """Validate commit identity and return the one synchronized package version."""
    _validate_sha(release_sha, "release_sha")
    if _run_git(repo, "cat-file", "-e", f"{release_sha}^{{commit}}").returncode != 0:
        raise ReleaseGateError("release_sha commit object is unavailable")
    _ensure_release_head(repo, release_sha)
    root_version = _read_version(repo, "pyproject.toml", "root", release_sha)
    skill_version = _read_version(repo, "skills/ppt-master/pyproject.toml", "Skill", release_sha)
    if root_version != skill_version:
        raise ReleaseGateError("Root and Skill package versions differ")
    validate_version_tag(root_version, release_tag)
    return root_version


def verify_exact_tag_ref(repo: Path, release_tag: str, expected_sha: str) -> None:
    """Verify an exact tag ref, never an ambiguous branch-or-tag name."""
    _validate_sha(expected_sha, "expected tag SHA")
    if TAG_RE.fullmatch(release_tag) is None:
        raise ReleaseGateError("Release tag is not canonical or safe")
    tag_ref = f"refs/tags/{release_tag}"
    if _run_git(repo, "show-ref", "--verify", "--quiet", tag_ref).returncode != 0:
        raise ReleaseGateError("Exact release tag ref is unavailable")
    actual_sha = _git_output(repo, "rev-parse", f"{tag_ref}^{{commit}}")
    if actual_sha != expected_sha:
        raise ReleaseGateError("Exact release tag ref does not point to release_sha")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseGateError("Workflow or artifact JSON contains duplicate keys")
        result[key] = value
    return result


def _flatten_workflow_runs(payload: Any) -> list[dict[str, Any]]:
    pages = payload if isinstance(payload, list) else [payload]
    runs: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list):
            raise ReleaseGateError("GitHub workflow-run response has an invalid shape")
        for run in page["workflow_runs"]:
            if not isinstance(run, dict):
                raise ReleaseGateError("GitHub workflow-run response contains a malformed run")
            runs.append(run)
    return runs


def find_verified_workflow_run(payload: Any, release_sha: str) -> int:
    """Find one exact successful push run for Check UVX Migration."""
    _validate_sha(release_sha, "release_sha")
    for run in _flatten_workflow_runs(payload):
        repository = run.get("repository")
        if (
            run.get("name") == "Check UVX Migration"
            and run.get("path") == CHECK_WORKFLOW_PATH
            and run.get("event") == "push"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("head_sha") == release_sha
            and run.get("head_branch") == "main"
            and isinstance(repository, dict)
            and repository.get("full_name") == REPOSITORY
            and type(run.get("id")) is int
            and run["id"] > 0
        ):
            return run["id"]
    raise ReleaseGateError(
        "No completed successful push run of check-uvx-migration.yml matches release_sha"
    )


def verify_workflow_runs_file(path: Path, release_sha: str) -> int:
    """Parse the trusted GitHub API response and return its exact run id."""
    if path.is_symlink() or not path.is_file():
        raise ReleaseGateError("Workflow-run evidence file is unavailable or is a symlink")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ReleaseGateError("Workflow-run evidence contains a non-finite JSON value")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError("Workflow-run evidence is not valid UTF-8 JSON") from exc
    return find_verified_workflow_run(payload, release_sha)


def _require_ancestor(repo: Path, ancestor: str, descendant: str, label: str) -> None:
    result = _run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise ReleaseGateError(label)
    raise ReleaseGateError("Git could not evaluate release ancestry")


def _verify_origin_main(repo: Path, release_sha: str, origin_ref: str, require_tip: bool) -> None:
    current = _git_output(repo, "rev-parse", "--verify", origin_ref)
    if require_tip and current != release_sha:
        raise ReleaseGateError("origin/main advanced before the immutable release was tagged")
    if not require_tip:
        _require_ancestor(repo, release_sha, origin_ref, "release_sha is not in origin/main history")


def _run_dependency_gate(repo: Path) -> None:
    deps = _load_trusted_module(repo, "skills/ppt-master/scripts/check_deps_sync.py", "trusted_release_deps")
    root_deps = deps.parse_pyproject_deps(repo / "pyproject.toml")
    skill_deps = deps.parse_pyproject_deps(repo / "skills/ppt-master/pyproject.toml")
    requirements = deps.parse_requirements_txt(repo / "skills/ppt-master/requirements.txt")
    errors = deps.compare_pyproject_pair(root_deps, skill_deps)
    errors.extend(
        deps.compare_req_vs_pyproject(
            requirements,
            root_deps,
            req_label="requirements.txt",
            pyproject_label="root pyproject.toml",
        )
    )
    errors.extend(
        deps.compare_req_vs_pyproject(
            requirements,
            skill_deps,
            req_label="requirements.txt",
            pyproject_label="skills/ppt-master/pyproject.toml",
        )
    )
    root_lock = repo / "uv.lock"
    skill_lock = repo / "skills/ppt-master/uv.lock"
    if not root_lock.is_file() or not skill_lock.is_file():
        errors.append("Both uv.lock files must exist")
    elif root_lock.read_bytes() != skill_lock.read_bytes():
        errors.append("uv.lock files differ byte-for-byte")
    if errors:
        raise ReleaseGateError("Dependency gate failed: " + "; ".join(errors))


def _run_fork_python_gates(repo: Path) -> None:
    absolute_files: list[str] = []
    for relative in FORK_PYTHON_FILES:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            raise ReleaseGateError(f"Fork Python gate file is not a regular file: {relative}")
        absolute_files.append(str(path))
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    with tempfile.TemporaryDirectory(prefix="ppt-master-release-pycache-") as pycache:
        compile_result = subprocess.run(
            [
                sys.executable,
                "-I",
                f"-Xpycache_prefix={pycache}",
                "-m",
                "py_compile",
                *absolute_files,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        if compile_result.returncode != 0:
            raise ReleaseGateError("Fork Python py_compile gate failed")
    ruff_name = "ruff.exe" if os.name == "nt" else "ruff"
    ruff_executable = Path(sys.executable).with_name(ruff_name)
    if not ruff_executable.is_file():
        raise ReleaseGateError("Pinned Ruff executable is missing beside the gate interpreter")
    ruff_result = subprocess.run(
        [str(ruff_executable), "check", "--isolated", "--no-cache", "--select", "F821", *absolute_files],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
    if ruff_result.returncode != 0:
        raise ReleaseGateError("Fork Python Ruff F821 gate failed")


def _check_action_pins(path: Path) -> None:
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "uses:" not in line:
            continue
        match = ACTION_LINE_RE.match(line)
        if match is None or SHA_RE.fullmatch(match.group("sha")) is None:
            raise ReleaseGateError(f"Workflow action is not immutably pinned: {path}:{line_number}")
        expected = EXPECTED_ACTION_PINS.get(match.group("action"))
        if expected is None or (match.group("sha"), match.group("release")) != expected:
            raise ReleaseGateError(f"Workflow action pin provenance is not approved: {path}:{line_number}")


def _load_workflow(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise ReleaseGateError("PyYAML is unavailable for workflow gate validation")
    try:
        class UniqueKeyLoader(yaml.SafeLoader):
            pass

        def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise ValueError(f"duplicate YAML key: {key!r}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        UniqueKeyLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping,
        )
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise ReleaseGateError(f"Workflow YAML is invalid: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        raise ReleaseGateError(f"Workflow YAML has no jobs mapping: {path}")
    return data


def _workflow_on(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """Read the trigger mapping across YAML 1.1 and GitHub's YAML semantics."""
    if "on" in data:
        trigger = data["on"]
    elif True in data:  # PyYAML 1.1 parses the unquoted ``on`` key as bool True.
        trigger = next((value for key, value in data.items() if key is True), None)
    else:
        raise ReleaseGateError(f"Workflow has no trigger mapping: {path}")
    if not isinstance(trigger, dict):
        raise ReleaseGateError(f"Workflow trigger mapping is malformed: {path}")
    return trigger


def _require_permissions(
    value: Any,
    expected: dict[str, str],
    label: str,
    *,
    allow_missing: bool = False,
) -> None:
    if allow_missing and value is None:
        return
    if value != expected:
        raise ReleaseGateError(f"{label} permissions are not the required least-privilege mapping")


def _workflow_steps(job: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
        raise ReleaseGateError(f"{label} has no valid steps list")
    steps = job["steps"]
    if not all(isinstance(step, dict) for step in steps):
        raise ReleaseGateError(f"{label} contains a malformed step")
    return steps


def _step_named(steps: list[dict[str, Any]], name: str, label: str) -> tuple[int, dict[str, Any]]:
    matches = [(index, step) for index, step in enumerate(steps) if step.get("name") == name]
    if len(matches) != 1:
        raise ReleaseGateError(f"{label} must contain exactly one step named {name!r}")
    return matches[0]


def _check_setup_uv_pins(workflow: dict[str, Any], label: str) -> None:
    """Require every privileged setup-uv step to use the pinned exact release."""
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise ReleaseGateError(f"{label} has no jobs mapping for setup-uv validation")
    setup_steps = [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in _workflow_steps(job, label)
        if isinstance(step.get("uses"), str) and step["uses"].startswith("astral-sh/setup-uv@")
    ]
    if not setup_steps:
        raise ReleaseGateError(f"{label} must install uv through the pinned setup-uv action")
    for step in setup_steps:
        with_values = step.get("with")
        if (
            not isinstance(with_values, dict)
            or with_values.get("version") != EXPECTED_UV_VERSION
            or with_values.get("checksum") != EXPECTED_UV_CHECKSUM
        ):
            raise ReleaseGateError(
                f"{label} setup-uv must pin uv {EXPECTED_UV_VERSION} with its exact checksum"
            )


def _check_isolated_python_invocations(text: str, label: str) -> None:
    """Reject bare or non-isolated Python invocations of trusted scripts."""
    if re.search(r"(?m)^\s*(?:python3?|uv run)\s+(?:\.github|skills)/", text):
        raise ReleaseGateError(f"{label} must invoke trusted Python scripts with python -I")
    if "python3" in text:
        raise ReleaseGateError(f"{label} must not rely on the removed python3 alias")
    for line in text.splitlines():
        if "check_release_gates.py" in line and not any(
            marker in line for marker in ("python -I ", '"$GATE_PYTHON" -I ')
        ):
            raise ReleaseGateError(
                f"{label} must invoke the release gate with an isolated Python interpreter"
            )


def _normalize_run(value: object, label: str) -> str:
    """Normalize one run block for exact template comparison."""
    if not isinstance(value, str):
        raise ReleaseGateError(f"{label} must provide an approved run block")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.strip("\n").split("\n")]
    return "\n".join(lines).strip("\n")


def _require_run(value: object, expected: str, label: str) -> None:
    if _normalize_run(value, label) != expected:
        raise ReleaseGateError(f"{label} run block does not match the approved exact template")


def _require_keys(step: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unexpected = set(step) - allowed
    if unexpected:
        raise ReleaseGateError(f"{label} contains unapproved step keys: {sorted(unexpected)}")


def _require_env(step: dict[str, Any], expected: dict[str, Any] | None, label: str) -> None:
    actual = step.get("env")
    if expected is None:
        if actual is not None:
            raise ReleaseGateError(f"{label} must not declare env")
    elif actual != expected:
        raise ReleaseGateError(f"{label} env does not match the approved mapping")


def _require_with(step: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    if step.get("with") != expected:
        raise ReleaseGateError(f"{label} with mapping does not match the approved mapping")


def _require_workflow_envelope(workflow: dict[str, Any], label: str) -> None:
    """Whitelist workflow-level keys and reject workflow-wide shell injection scopes."""
    normalized = {("on" if key is True else key) for key in workflow}
    structural = normalized & JOB_STRUCTURAL_KEYS
    if structural:
        raise ReleaseGateError(f"{label} must not declare workflow-level {sorted(structural)}")
    unexpected = normalized - WORKFLOW_ALLOWED_KEYS
    if unexpected:
        raise ReleaseGateError(f"{label} contains unapproved workflow keys: {sorted(unexpected)}")


def _require_job_envelope(
    job: dict[str, Any],
    label: str,
    *,
    allowed_keys: frozenset[str],
    if_value: object = _UNSET,
    environment: object = _UNSET,
) -> None:
    """Whitelist job-level keys and pin or forbid job-level if/environment."""
    structural = set(job) & JOB_STRUCTURAL_KEYS
    if structural:
        raise ReleaseGateError(f"{label} must not declare job-level {sorted(structural)}")
    unexpected = set(job) - allowed_keys
    if unexpected:
        raise ReleaseGateError(f"{label} contains unapproved job keys: {sorted(unexpected)}")
    if if_value is _UNSET:
        if "if" in job:
            raise ReleaseGateError(f"{label} must not declare an unapproved job-level if")
    elif job.get("if") != if_value:
        raise ReleaseGateError(f"{label} job-level if does not match the approved condition")
    if environment is _UNSET:
        if "environment" in job:
            raise ReleaseGateError(f"{label} must not declare an unapproved environment")
    elif job.get("environment") != environment:
        raise ReleaseGateError(f"{label} environment does not match the approved mapping")


def _require_step_contracts(
    steps: list[dict[str, Any]],
    contracts: tuple[WorkflowStepContract, ...],
    label: str,
) -> None:
    """Require the complete approved step inventory, key set, and exact content."""
    if len(steps) != len(contracts):
        raise ReleaseGateError(f"{label} must contain exactly {len(contracts)} approved steps")
    if [step.get("name") for step in steps] != [contract.name for contract in contracts]:
        raise ReleaseGateError(f"{label} step inventory or order is not approved")
    for step, contract in zip(steps, contracts):
        step_label = f"{label} step {contract.name!r}"
        _require_keys(step, contract.keys, step_label)
        if contract.uses is None:
            if "uses" in step:
                raise ReleaseGateError(f"{step_label} must not use an action")
        else:
            if step.get("uses") != contract.uses:
                raise ReleaseGateError(f"{step_label} must use the approved pinned action")
            _require_with(step, contract.with_values or {}, step_label)
        _require_env(step, contract.env, step_label)
        if contract.run_template is not None:
            _require_run(step.get("run"), contract.run_template, step_label)
        elif "run" in step:
            raise ReleaseGateError(f"{step_label} must not provide a run block")
        if contract.step_id is not None and step.get("id") != contract.step_id:
            raise ReleaseGateError(f"{step_label} must keep its approved step id")


def _workflow_run_blocks(workflow: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return blocks
    for job in jobs.values():
        if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
            continue
        for step in job["steps"]:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                blocks.append(step["run"])
    return blocks


def _git_subcommands(text: str) -> list[str]:
    normalized = text.replace("\\\r\n", " ").replace("\\\n", " ")
    return [match.group("subcommand") for match in GIT_SUBCOMMAND_RE.finditer(normalized)]


def _check_git_write_surface(text: str, label: str, *, allowed_pushes: int) -> None:
    """Reject every forbidden git subcommand and any unapproved push command."""
    subcommands = _git_subcommands(text)
    forbidden = sorted(set(subcommands) & FORBIDDEN_GIT_SUBCOMMANDS)
    if forbidden:
        raise ReleaseGateError(f"{label} contains forbidden git subcommands: {forbidden}")
    pushes = [subcommand for subcommand in subcommands if subcommand == "push"]
    if len(pushes) != allowed_pushes:
        raise ReleaseGateError(
            f"{label} must contain exactly {allowed_pushes} approved git push command(s)"
        )


def _check_oidc_run_safety(steps: list[dict[str, Any]], label: str) -> None:
    """Reject any OIDC-job run that could install, import, or execute package code."""
    for step in steps:
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for pattern in OIDC_FORBIDDEN_RUN_PATTERNS:
            if re.search(pattern, run):
                raise ReleaseGateError(
                    f"{label} run block contains a forbidden OIDC publication command"
                )
    for step in steps:
        serialized = json.dumps(step)
        if (
            "WHEEL_PATH" in serialized or "SDIST_PATH" in serialized
        ) and step.get("name") not in {
            "Bind verified artifact hashes before OIDC",
            "Publish only statically verified paths through PyPI trusted publishing",
        }:
            raise ReleaseGateError(
                f"{label} must not handle WHEEL_PATH/SDIST_PATH outside the approved steps"
            )


def _check_migration_workflow(workflow: dict[str, Any], path: Path) -> None:
    """Require the exact fail-closed Check UVX Migration step contract."""
    text = path.read_text(encoding="utf-8")
    if "python3" in text:
        raise ReleaseGateError("Check UVX Migration must invoke Python through python -I")
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) != {"check"}:
        raise ReleaseGateError("Check UVX Migration must contain only the check job")
    job = jobs["check"]
    if not isinstance(job, dict):
        raise ReleaseGateError("Check UVX Migration check job is malformed")
    _require_job_envelope(
        job,
        "Check UVX Migration check job",
        allowed_keys=MIGRATION_JOB_ALLOWED_KEYS,
    )
    if job.get("runs-on") != "ubuntu-latest":
        raise ReleaseGateError("Check UVX Migration check job must run on ubuntu-latest")
    steps = _workflow_steps(job, "Check UVX Migration check job")
    _require_step_contracts(steps, MIGRATION_STEP_CONTRACTS, "Check UVX Migration check job")


def _check_tooling_install(run: object, label: str, *, require_build_backend: bool) -> None:
    """Require exact-pinned tooling installed into the explicit gate interpreter."""
    if (
        not isinstance(run, str)
        or 'uv venv --python 3.12 "$RUNNER_TEMP/ppt-master-gate-venv"' not in run
        or 'GATE_PYTHON="$RUNNER_TEMP/ppt-master-gate-venv/bin/python"' not in run
        or "uv pip sync" not in run
        or "--require-hashes" not in run
        or "--only-binary :all:" not in run
        or (
            ".github/release-build-requirements.txt"
            if require_build_backend
            else ".github/release-gate-requirements.txt"
        )
        not in run
        or "uv pip install" in run
        or "--system" in run
        or "--break-system-packages" in run
        or "--python" not in run
        or "GATE_PYTHON" not in run
        or ">=" in run
        or "~=" in run
    ):
        raise ReleaseGateError(f"{label} must install only exact pinned gate and build tooling")


def _check_release_tooling_lock(repo: Path) -> None:
    """Require each release tooling input and generated hash lock to remain exact."""
    for input_name, lock_name, expected_input, expected_digest in RELEASE_TOOLING_LOCKS:
        input_path = repo / input_name
        lock_path = repo / lock_name
        for path, label in ((input_path, "input"), (lock_path, "lock")):
            if path.is_symlink() or not path.is_file():
                raise ReleaseGateError(f"Release tooling {label} is not a regular file: {path.name}")
        if input_path.read_bytes() != expected_input:
            raise ReleaseGateError("Release tooling input does not match the approved exact pins")
        digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if digest != expected_digest:
            raise ReleaseGateError("Release tooling hash lock does not match the approved digest")


def _check_workflow_policy(repo: Path) -> None:
    _check_release_tooling_lock(repo)
    data: dict[str, dict[str, Any]] = {}
    for relative in PRIVILEGED_WORKFLOWS:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            raise ReleaseGateError(f"Privileged workflow is unavailable: {relative}")
        _check_action_pins(path)
        workflow = _load_workflow(path)
        _require_workflow_envelope(workflow, relative)
        data[relative] = workflow
    for relative in ADDITIONAL_PINNED_WORKFLOWS:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            raise ReleaseGateError(f"Pinned workflow is unavailable: {relative}")
        _check_action_pins(path)
    approved_push_counts = {
        ".github/workflows/auto-tag.yml": 1,
        ".github/workflows/check-uvx-migration.yml": 0,
        ".github/workflows/publish-pypi.yml": 0,
    }
    for relative, workflow in data.items():
        _check_git_write_surface(
            "\n".join(_workflow_run_blocks(workflow)),
            relative,
            allowed_pushes=approved_push_counts[relative],
        )

    auto_path = repo / ".github/workflows/auto-tag.yml"
    auto_text = auto_path.read_text(encoding="utf-8")
    auto_workflow = data[".github/workflows/auto-tag.yml"]
    _check_isolated_python_invocations(auto_text, "auto-tag")
    _check_setup_uv_pins(auto_workflow, "auto-tag")
    if "--require-origin-main-tip" in auto_text:
        raise ReleaseGateError("auto-tag must accept the immutable release SHA, not the moving main tip")
    auto_on = _workflow_on(auto_workflow, auto_path)
    if set(auto_on) != {"workflow_run"} or auto_on.get("workflow_run") != {
        "workflows": ["Check UVX Migration"],
        "types": ["completed"],
    }:
        raise ReleaseGateError("auto-tag must trigger only from a completed Check UVX Migration workflow_run")
    _require_permissions(auto_workflow.get("permissions"), AUTO_TAG_PERMISSIONS, "auto-tag workflow")
    auto_jobs = auto_workflow.get("jobs")
    if not isinstance(auto_jobs, dict) or set(auto_jobs) != {"verify-and-tag"}:
        raise ReleaseGateError("auto-tag must contain only the verify-and-tag job")
    auto_job = auto_jobs["verify-and-tag"]
    if not isinstance(auto_job, dict):
        raise ReleaseGateError("auto-tag verify-and-tag job is malformed")
    _require_job_envelope(
        auto_job,
        "auto-tag verify-and-tag job",
        allowed_keys=AUTO_TAG_JOB_ALLOWED_KEYS,
        if_value=AUTO_TAG_JOB_CONDITION,
    )
    if auto_job.get("runs-on") != "ubuntu-latest" or auto_job.get("timeout-minutes") != 30:
        raise ReleaseGateError("auto-tag job must keep its approved runner and timeout")
    _require_permissions(auto_job.get("permissions"), AUTO_TAG_PERMISSIONS, "auto-tag verify-and-tag job")
    if any(
        isinstance(scope, dict) and "PUSH_PAT" in scope
        for scope in (auto_workflow.get("env"), auto_job.get("env"))
    ):
        raise ReleaseGateError("auto-tag must not expose PUSH_PAT at workflow or job scope")
    condition = auto_job.get("if")
    required_conditions = (
        "github.event_name == 'workflow_run'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.repository.full_name == 'elvisw/ppt-master'",
    )
    if (
        not isinstance(condition, str)
        or "||" in condition
        or any(fragment not in condition for fragment in required_conditions)
    ):
        raise ReleaseGateError("auto-tag must restrict execution to the exact successful main push run")
    if "auto_fix_uvx.py" in auto_text or re.search(
        r"(?m)^\s*git\s+(?:add|commit|reset|rebase|merge|cherry-pick)\b",
        auto_text,
    ):
        raise ReleaseGateError("auto-tag contains forbidden auto-fix or main-push behavior")
    auto_steps = _workflow_steps(auto_job, "auto-tag verify-and-tag job")
    _require_step_contracts(auto_steps, AUTO_TAG_STEP_CONTRACTS, "auto-tag verify-and-tag job")
    if not auto_steps or auto_steps[-1].get("name") != "Push exact release tag":
        raise ReleaseGateError("auto-tag's final step must be the exact tag push")
    _, checkout_step = _step_named(
        auto_steps,
        "Checkout immutable workflow-run commit",
        "auto-tag verify-and-tag job",
    )
    checkout_with = checkout_step.get("with")
    if (
        not isinstance(checkout_with, dict)
        or checkout_with.get("ref") != "${{ github.event.workflow_run.head_sha }}"
        or checkout_with.get("fetch-depth") != 0
        or checkout_with.get("persist-credentials") is not False
    ):
        raise ReleaseGateError("auto-tag must checkout the immutable workflow_run head SHA")
    pat_steps = [
        step
        for step in auto_steps
        if isinstance(step.get("env"), dict) and "PUSH_PAT" in step["env"]
    ]
    if len(pat_steps) != 1 or pat_steps[0].get("name") != "Push exact release tag":
        raise ReleaseGateError("auto-tag must expose PUSH_PAT only to its final tag-push step")
    push_step = auto_steps[-1]
    push_env = push_step.get("env")
    if (
        not isinstance(push_env, dict)
        or push_env.get("PUSH_PAT") != "${{ secrets.PUSH_PAT }}"
        or json.dumps(auto_workflow).count("secrets.PUSH_PAT") != 1
    ):
        raise ReleaseGateError("auto-tag final tag-push step must be the only PUSH_PAT exposure")
    push_run = push_step.get("run", "")
    push_pattern = re.compile(r"(?m)^\s*(?:push\b|git\b[^\n]*\\\s*\n\s*push\b)")
    if (
        not isinstance(push_run, str)
        or "git -c credential.helper= -c core.hooksPath=/dev/null fetch --no-tags --prune origin main" not in push_run
        or "git -c credential.helper= -c core.hooksPath=/dev/null ls-remote --exit-code --heads origin refs/heads/main" not in push_run
        or "REMOTE_MAIN_SHA" not in push_run
        or "merge-base --is-ancestor" not in push_run
        or '"$RELEASE_SHA"' not in push_run
        or 'refs/tags/$RELEASE_TAG' not in push_run
        or "--force" in push_run
        or "--force-with-lease" in push_run
        or len(push_pattern.findall(push_run)) != 1
    ):
        raise ReleaseGateError("auto-tag must refresh main and push only the exact release tag")
    _, auto_gate_step = _step_named(
        auto_steps,
        "Run complete release gates for exact SHA",
        "auto-tag verify-and-tag job",
    )
    auto_gate_run = auto_gate_step.get("run")
    if (
        not isinstance(auto_gate_run, str)
        or "--workflow-runs-json" not in auto_gate_run
        or "--require-origin-main-tip" in auto_gate_run
    ):
        raise ReleaseGateError("auto-tag must verify the immutable SHA without requiring the main tip")
    _, auto_tooling_step = _step_named(
        auto_steps,
        "Install pinned trusted gate tooling",
        "auto-tag verify-and-tag job",
    )
    _check_tooling_install(
        auto_tooling_step.get("run"),
        "auto-tag",
        require_build_backend=False,
    )
    all_push_runs = [
        step for step in auto_steps if isinstance(step.get("run"), str) and push_pattern.search(step["run"])
    ]
    if len(all_push_runs) != 1 or all_push_runs[0] is not push_step:
        raise ReleaseGateError("auto-tag may push only from its final tag-push step")

    publish_path = repo / ".github/workflows/publish-pypi.yml"
    publish_text = publish_path.read_text(encoding="utf-8")
    if "uvx --from" in publish_text or re.search(r"python[^\n]*\.whl", publish_text):
        raise ReleaseGateError("publish workflow must not execute or import a wheel")
    publish_workflow = data[".github/workflows/publish-pypi.yml"]
    _check_isolated_python_invocations(publish_text, "publish")
    _check_setup_uv_pins(publish_workflow, "publish")
    concurrency = publish_workflow.get("concurrency")
    if not isinstance(concurrency, dict) or concurrency.get("cancel-in-progress") is not False:
        raise ReleaseGateError("publish concurrency must not cancel an in-flight publication")
    group = concurrency.get("group")
    if (
        not isinstance(group, str)
        or any(
            fragment not in group
            for fragment in (
                "inputs.release_tag",
                "github.ref_name",
                "inputs.release_sha",
                "github.sha",
            )
        )
        or "github.event_name" in group
    ):
        raise ReleaseGateError("publish concurrency must bind the canonical tag and release SHA only")
    publish_on = _workflow_on(publish_workflow, publish_path)
    if set(publish_on) != {"push", "workflow_dispatch"}:
        raise ReleaseGateError("publish must expose only tag-push and recovery workflow_dispatch triggers")
    push_trigger = publish_on.get("push")
    if not isinstance(push_trigger, dict) or push_trigger.get("tags") != ["v*"]:
        raise ReleaseGateError("publish push trigger must be limited to v* tags")
    dispatch_trigger = publish_on.get("workflow_dispatch")
    dispatch_inputs = dispatch_trigger.get("inputs") if isinstance(dispatch_trigger, dict) else None
    if not isinstance(dispatch_inputs, dict) or set(dispatch_inputs) != {
        "release_sha",
        "release_tag",
    }:
        raise ReleaseGateError("publish recovery dispatch must require release_sha and release_tag inputs")
    for input_name, input_spec in dispatch_inputs.items():
        if (
            not isinstance(input_spec, dict)
            or input_spec.get("required") is not True
            or input_spec.get("type") != "string"
        ):
            raise ReleaseGateError(f"publish dispatch input {input_name} must be a required string")
    _require_permissions(publish_workflow.get("permissions"), PUBLISH_BUILD_PERMISSIONS, "publish workflow")
    publish_jobs = publish_workflow.get("jobs")
    if not isinstance(publish_jobs, dict) or set(publish_jobs) != {
        "build-and-gate",
        "verify-artifact",
        "publish",
    }:
        raise ReleaseGateError("publish must contain exactly build-and-gate, verify-artifact, and publish jobs")
    for name, allowed_keys, timeout in (
        ("build-and-gate", PUBLISH_BUILD_JOB_ALLOWED_KEYS, 45),
        ("verify-artifact", PUBLISH_VERIFY_JOB_ALLOWED_KEYS, 15),
    ):
        job = publish_jobs[name]
        if not isinstance(job, dict):
            raise ReleaseGateError(f"publish {name} job is malformed")
        _require_job_envelope(job, f"publish {name} job", allowed_keys=allowed_keys)
        if job.get("runs-on") != "ubuntu-latest" or job.get("timeout-minutes") != timeout:
            raise ReleaseGateError(f"publish {name} job must keep its approved runner and timeout")
        _require_permissions(job.get("permissions"), PUBLISH_BUILD_PERMISSIONS, f"publish {name} job")
        if "secrets." in json.dumps(job):
            raise ReleaseGateError(f"{name} job must not receive publication secrets")
    publish_job = publish_jobs.get("publish")
    if not isinstance(publish_job, dict):
        raise ReleaseGateError("publish workflow is missing publish job")
    _require_job_envelope(
        publish_job,
        "publish OIDC job",
        allowed_keys=PUBLISH_OIDC_JOB_ALLOWED_KEYS,
        environment={"name": "pypi"},
    )
    if publish_job.get("runs-on") != "ubuntu-latest" or publish_job.get("timeout-minutes") != 15:
        raise ReleaseGateError("publish OIDC job must keep its approved runner and timeout")
    _require_permissions(
        publish_job.get("permissions"),
        PUBLISH_PERMISSIONS,
        "publish job",
    )
    environment = publish_job.get("environment")
    if not isinstance(environment, dict) or environment.get("name") != "pypi":
        raise ReleaseGateError("publish job must use the pypi environment")

    verify_job = publish_jobs["verify-artifact"]
    if not isinstance(verify_job, dict):
        raise ReleaseGateError("publish verify-artifact job is malformed")
    if verify_job.get("needs") != "build-and-gate":
        raise ReleaseGateError("verify-artifact must depend on build-and-gate")
    verify_outputs = verify_job.get("outputs")
    if verify_outputs != {
        "artifact_name": "${{ needs.build-and-gate.outputs.artifact_name }}",
        "wheel_name": "${{ steps.verify.outputs.wheel_name }}",
        "sdist_name": "${{ steps.verify.outputs.sdist_name }}",
        "wheel_sha256": "${{ steps.verify.outputs.wheel_sha256 }}",
        "sdist_sha256": "${{ steps.verify.outputs.sdist_sha256 }}",
        "manifest_sha256": "${{ steps.verify.outputs.manifest_sha256 }}",
    }:
        raise ReleaseGateError("verify-artifact must expose the verified artifact identity outputs")
    verify_steps = _workflow_steps(verify_job, "publish verify-artifact job")
    _require_step_contracts(
        verify_steps,
        PUBLISH_VERIFY_STEP_CONTRACTS,
        "publish verify-artifact job",
    )
    _, verify_step = _step_named(
        verify_steps,
        "Run verify_artifact_manifest for artifact and attribution",
        "publish verify-artifact job",
    )
    verify_run = verify_step.get("run", "")
    if (
        not isinstance(verify_run, str)
        or "--verify-artifact-manifest" not in verify_run
        or "--emit-artifact-env" not in verify_run
    ):
        raise ReleaseGateError("verify-artifact must statically verify and emit the artifact binding")

    build_job = publish_jobs["build-and-gate"]
    if not isinstance(build_job, dict):
        raise ReleaseGateError("publish build-and-gate job is malformed")
    if build_job.get("outputs") != {
        "artifact_name": "release-distributions-${{ github.run_id }}-${{ github.run_attempt }}"
    }:
        raise ReleaseGateError("publish build job must expose only its run-scoped artifact name")
    build_steps = _workflow_steps(build_job, "publish build-and-gate job")
    _require_step_contracts(
        build_steps,
        PUBLISH_BUILD_STEP_CONTRACTS,
        "publish build-and-gate job",
    )
    _, build_checkout = _step_named(
        build_steps,
        "Checkout immutable release SHA",
        "publish build-and-gate job",
    )
    build_checkout_with = build_checkout.get("with")
    if (
        not isinstance(build_checkout_with, dict)
        or build_checkout_with.get("ref")
        != "${{ github.event_name == 'push' && github.sha || inputs.release_sha }}"
        or build_checkout_with.get("fetch-depth") != 0
        or build_checkout_with.get("persist-credentials") is not False
    ):
        raise ReleaseGateError("publish build job must checkout the immutable release SHA")
    _, verify_checkout = _step_named(
        verify_steps,
        "Checkout immutable release SHA for static verifier",
        "publish verify-artifact job",
    )
    verify_checkout_with = verify_checkout.get("with")
    if (
        not isinstance(verify_checkout_with, dict)
        or verify_checkout_with.get("ref")
        != "${{ github.event_name == 'push' && github.sha || inputs.release_sha }}"
        or verify_checkout_with.get("fetch-depth") != 1
        or verify_checkout_with.get("persist-credentials") is not False
    ):
        raise ReleaseGateError("publish verifier must checkout the immutable release SHA")
    validate_index, validate_step = _step_named(
        build_steps,
        "Validate canonical manual or tag identity",
        "publish build-and-gate job",
    )
    fetch_index, fetch_step = _step_named(
        build_steps,
        "Fetch and bind exact release refs",
        "publish build-and-gate job",
    )
    if validate_index >= fetch_index:
        raise ReleaseGateError("publish must validate input identity before fetching the exact tag")
    validate_run = validate_step.get("run", "")
    if (
        not isinstance(validate_run, str)
        or "--validate-only" not in validate_run
        or 'GITHUB_REF" != "refs/tags/$RELEASE_TAG"' not in validate_run
    ):
        raise ReleaseGateError("publish must validate the canonical release identity before other gates")
    fetch_run = fetch_step.get("run", "")
    if (
        not isinstance(fetch_run, str)
        or "git fetch --no-tags --prune origin main" not in fetch_run
        or 'refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG' not in fetch_run
        or 'refs/tags/$RELEASE_TAG^{commit}' not in fetch_run
    ):
        raise ReleaseGateError("publish must fetch and bind the exact release tag ref")
    gate_index, gate_step = _step_named(
        build_steps,
        "Rerun complete release gates for exact tag and SHA",
        "publish build-and-gate job",
    )
    build_index, build_step = _step_named(
        build_steps,
        "Build distributions without OIDC",
        "publish build-and-gate job",
    )
    if not fetch_index < gate_index < build_index:
        raise ReleaseGateError("publish must run complete source gates before building distributions")
    build_run = build_step.get("run", "")
    if (
        not isinstance(build_run, str)
        or "uv build" not in build_run
        or "--no-build-isolation" not in build_run
        or '--python "$GATE_PYTHON"' not in build_run
        or "git diff --quiet" not in build_run
        or "git diff --cached --quiet" not in build_run
    ):
        raise ReleaseGateError("publish must build with the locked build backend without isolation")
    _, tooling_step = _step_named(
        build_steps,
        "Install pinned trusted gate tooling",
        "publish build-and-gate job",
    )
    _check_tooling_install(tooling_step.get("run"), "publish", require_build_backend=True)
    gate_run = gate_step.get("run", "")
    if (
        not isinstance(gate_run, str)
        or "--workflow-runs-json" not in gate_run
        or "--upstream-ref upstream/main" not in gate_run
        or "--require-existing-tag" not in gate_run
    ):
        raise ReleaseGateError("publish must rerun the complete immutable release gate set")
    _, upload_step = _step_named(
        build_steps,
        "Upload only verified distributions and manifest",
        "publish build-and-gate job",
    )
    upload_with = upload_step.get("with")
    if (
        not isinstance(upload_with, dict)
        or upload_with.get("name") != "release-distributions-${{ github.run_id }}-${{ github.run_attempt }}"
        or upload_with.get("path") != "${{ runner.temp }}/release-artifact"
        or upload_with.get("if-no-files-found") != "error"
    ):
        raise ReleaseGateError("publish must upload only the verified run-scoped artifact directory")

    if publish_job.get("needs") != "verify-artifact":
        raise ReleaseGateError("publish must depend on the fresh static artifact verifier")
    publish_steps = _workflow_steps(publish_job, "publish OIDC job")
    _require_step_contracts(publish_steps, PUBLISH_OIDC_STEP_CONTRACTS, "publish OIDC job")
    _check_oidc_run_safety(publish_steps, "publish OIDC job")
    download_index, download_step = _step_named(
        publish_steps,
        "Download verified artifact to fresh runner temp",
        "publish OIDC job",
    )
    bind_index, bind_step = _step_named(
        publish_steps,
        "Bind verified artifact hashes before OIDC",
        "publish OIDC job",
    )
    setup_index, _ = _step_named(publish_steps, "Setup uv for OIDC publication", "publish OIDC job")
    publish_index, publish_step = _step_named(
        publish_steps,
        "Publish only statically verified paths through PyPI trusted publishing",
        "publish OIDC job",
    )
    if not download_index < bind_index < setup_index < publish_index:
        raise ReleaseGateError("publish OIDC steps must download, bind, set up, then publish in order")
    if "needs.verify-artifact.outputs.artifact_name" not in json.dumps(download_step):
        raise ReleaseGateError("publish must download the artifact named by the verified job output")
    bind_run = bind_step.get("run", "")
    if (
        not isinstance(bind_run, str)
        or "sha256sum" not in bind_run
        or "needs.verify-artifact.outputs" not in json.dumps(bind_step)
        or "VERIFIED_MANIFEST_SHA256" not in json.dumps(bind_step)
    ):
        raise ReleaseGateError("publish must hash-check the exact verifier outputs before OIDC")
    if (
        not isinstance(publish_step.get("run"), str)
        or "uv publish" not in publish_step["run"]
        or "--trusted-publishing always" not in publish_step["run"]
        or "WHEEL_PATH" not in publish_step["run"]
        or "SDIST_PATH" not in publish_step["run"]
    ):
        raise ReleaseGateError("publish OIDC job must call uv publish")

    migration = data[".github/workflows/check-uvx-migration.yml"]
    migration_path = repo / ".github/workflows/check-uvx-migration.yml"
    migration_on = _workflow_on(migration, migration_path)
    if set(migration_on) != {"push"} or migration_on.get("push") != {"branches": ["main"]}:
        raise ReleaseGateError("Check UVX Migration must run only on pushes to main")
    _require_permissions(migration.get("permissions"), MIGRATION_PERMISSIONS, "Check UVX Migration")
    migration_jobs = migration.get("jobs")
    if not isinstance(migration_jobs, dict) or set(migration_jobs) != {"check"}:
        raise ReleaseGateError("Check UVX Migration must contain only the check job")
    if not isinstance(migration_jobs["check"], dict):
        raise ReleaseGateError("Check UVX Migration check job is malformed")
    _require_permissions(
        migration_jobs["check"].get("permissions"),
        MIGRATION_PERMISSIONS,
        "Check UVX Migration check job",
        allow_missing=True,
    )
    _check_migration_workflow(migration, migration_path)


def _run_migration_gate(repo: Path, release_sha: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(repo / "skills/ppt-master/scripts/check_uvx_migration.py"),
            release_sha,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode not in (0, 2):
        raise ReleaseGateError("uvx migration gate failed")
    if result.returncode == 2:
        print("Notice: migration checker returned exit 2; exact successful workflow evidence was verified separately.")


def run_release_gates(
    repo: Path,
    *,
    release_sha: str,
    release_tag: str,
    upstream_ref: str,
    workflow_runs_json: Path,
    origin_ref: str = "refs/remotes/origin/main",
    require_origin_main_tip: bool = False,
    require_existing_tag: bool = False,
) -> int:
    """Run every source and immutable release gate without modifying the repo."""
    repo = repo.resolve()
    run_id = verify_workflow_runs_file(workflow_runs_json, release_sha)
    _ensure_release_head(repo, release_sha)
    _ensure_clean_checkout(repo)
    version = validate_release_identity(repo, release_sha, release_tag)
    if require_existing_tag:
        verify_exact_tag_ref(repo, release_tag, release_sha)
    _verify_origin_main(repo, release_sha, origin_ref, require_origin_main_tip)

    ancestry = _load_trusted_module(repo, ".github/scripts/check_upstream_ancestry.py", "trusted_release_ancestry")
    ancestry.verify_overlay_policy(repo, release_sha)
    ancestry.verify_head_commit(repo, release_sha, upstream_ref)

    cli = _load_trusted_module(repo, "skills/ppt-master/scripts/check_cli_sync.py", "trusted_release_cli")
    cli_errors = cli.collect_cli_sync_errors(repo)
    if cli_errors:
        raise ReleaseGateError("CLI synchronization gate failed: " + "; ".join(cli_errors))
    _run_dependency_gate(repo)

    candidate = _load_trusted_module(repo, ".github/scripts/check_sync_candidate.py", "trusted_release_candidate")
    try:
        candidate.check_candidate(repo, repo, base_sha=None, candidate_ref=None, head_sha=None)
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ReleaseGateError(f"Attribution/manifest/fork/YAML gate failed: {exc}") from exc

    scanner = _load_trusted_module(
        repo,
        "skills/ppt-master/scripts/check_uvx_repository.py",
        "trusted_release_uvx_repository",
    )
    violations = scanner.scan_repository(repo)
    if violations:
        first = violations[0]
        raise ReleaseGateError(
            f"Repository UVX scan failed at {first.path}:{first.line} ({first.rule})"
        )
    _check_workflow_policy(repo)
    _run_migration_gate(repo, release_sha)
    _run_fork_python_gates(repo)
    _ensure_clean_checkout(repo)
    print(f"Release gates passed for {release_tag} ({version}) at {release_sha}; workflow run {run_id} verified.")
    return run_id


def _safe_distribution_name(name: str) -> None:
    if not name or Path(name).name != name or "\\" in name or "/" in name:
        raise ReleaseGateError("Distribution filename is not a safe basename")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ReleaseGateError("Distribution filename contains unsafe characters")


def _validate_distribution_filename(name: str, version: str) -> str:
    _safe_distribution_name(name)
    sdist_name = f"ppt_master-{version}.tar.gz"
    if re.fullmatch(rf"ppt_master-{re.escape(version)}-[A-Za-z0-9][A-Za-z0-9_.-]*\.whl", name):
        return "wheel"
    if name == sdist_name:
        return "sdist"
    raise ReleaseGateError("Distribution filename is not bound to the release version")


def _validate_archive_member_name(name: str, label: str) -> None:
    normalized = name.replace("\\", "/")
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized) is not None
        or ".." in normalized.split("/")
    ):
        raise ReleaseGateError(f"{label} contains an unsafe archive member")


def _normalized_text_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink() or not stat.S_ISREG(os.lstat(path).st_mode):
            raise ReleaseGateError(f"Attribution source file is unavailable or is a symlink: {path}")
        data = path.read_bytes()
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseGateError(f"Attribution source file is unavailable or not UTF-8: {path}") from exc


def _source_attribution_path(source_root: Path, archive_suffix: str) -> Path:
    relative = archive_suffix.replace("skills/ppt_master/", "skills/ppt-master/", 1)
    return source_root / relative


def _source_attribution_bytes(source_root: Path, relative: str, source_sha: str | None) -> bytes:
    if source_sha is None:
        return _normalized_text_bytes(_source_attribution_path(source_root, relative))
    _validate_sha(source_sha, "source_sha")
    result = _run_git(source_root, "show", f"{source_sha}:{relative}")
    if result.returncode != 0:
        raise ReleaseGateError(f"Attribution source file is unavailable in release_sha: {relative}")
    return _normalized_text_bytes_from_bytes(result.stdout.encode("utf-8"), relative)


def _validate_core_metadata(data: bytes, version: str, label: str) -> None:
    """Parse Core Metadata headers with ASCII case-insensitive field merging."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseGateError(f"{label} metadata is not valid UTF-8") from exc
    folded: dict[str, list[tuple[str, str]]] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        if line[:1] in (" ", "\t"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if field != field.strip():
            raise ReleaseGateError(
                f"{label} metadata field name must not contain surrounding whitespace"
            )
        name = field
        if not name.isascii():
            raise ReleaseGateError(f"{label} metadata field name is not ASCII")
        folded.setdefault(name.lower(), []).append((name, value.strip()))

    def _require(field: str, expected: str) -> None:
        entries = folded.get(field.lower(), [])
        if len(entries) != 1:
            raise ReleaseGateError(f"{label} metadata must contain exactly one {field} header")
        original, value = entries[0]
        if original != field:
            raise ReleaseGateError(
                f"{label} metadata field {original!r} is not canonical {field!r}"
            )
        if value != expected:
            raise ReleaseGateError(f"{label} metadata must bind {field}: {expected}")

    _require("Name", "ppt-master")
    _require("Version", version)


def _validate_wheel_static(
    path: Path,
    version: str,
    source_root: Path | None = None,
    source_sha: str | None = None,
) -> None:
    try:
        if path.is_symlink() or not stat.S_ISREG(os.lstat(path).st_mode):
            raise ReleaseGateError("Wheel is not a regular file")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for name in names:
                _validate_archive_member_name(name, "Wheel")
            for info in archive.infolist():
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ReleaseGateError("Wheel contains a non-regular archive member")
            metadata_name = f"ppt_master-{version}.dist-info/METADATA"
            if names.count(metadata_name) != 1:
                raise ReleaseGateError(
                    f"Wheel must contain exactly one {metadata_name} file"
                )
            other_metadata = [
                name for name in names if name.endswith("/METADATA") and name != metadata_name
            ]
            if other_metadata:
                raise ReleaseGateError("Wheel contains an unexpected dist-info METADATA file")
            metadata_dir = f"ppt_master-{version}.dist-info"
            for companion in ("WHEEL", "RECORD"):
                if f"{metadata_dir}/{companion}" not in names:
                    raise ReleaseGateError(f"Wheel metadata directory is missing {companion}")
            metadata = archive.read(metadata_name)
            _validate_core_metadata(metadata, version, "Wheel")
            wheel_attribution: dict[str, str] = {}
            for basename, candidates in WHEEL_ATTRIBUTION_LOCATIONS.items():
                matches = [name for name in names if name in candidates]
                if len(matches) != 1:
                    raise ReleaseGateError(f"Wheel is missing static attribution file: {basename}")
                wheel_attribution[basename] = matches[0]
            skill = archive.read(wheel_attribution["SKILL.md"]).decode("utf-8")
            if skill.count("uvx ppt-master attribution-guard") != 1:
                raise ReleaseGateError("Wheel Skill attribution marker is missing or duplicated")
            for basename in WHEEL_ATTRIBUTION_BASENAMES[1:]:
                if not archive.read(wheel_attribution[basename]).strip():
                    raise ReleaseGateError(f"Wheel attribution file is empty: {basename}")
            if source_root is not None:
                for basename in WHEEL_ATTRIBUTION_BASENAMES:
                    suffix = wheel_attribution[basename]
                    source_relative = f"skills/ppt-master/{basename}"
                    if _source_attribution_bytes(source_root, source_relative, source_sha) != _normalized_text_bytes_from_bytes(
                        archive.read(suffix), suffix
                    ):
                        raise ReleaseGateError(f"Wheel attribution file differs from source: {basename}")
    except ReleaseGateError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ReleaseGateError("Wheel static inspection failed") from exc


def _normalized_text_bytes_from_bytes(data: bytes, label: str) -> bytes:
    try:
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseGateError(f"Archive attribution file is not UTF-8: {label}") from exc


def _validate_sdist_static(
    path: Path,
    version: str,
    source_root: Path | None = None,
    source_sha: str | None = None,
) -> None:
    try:
        if path.is_symlink() or not stat.S_ISREG(os.lstat(path).st_mode):
            raise ReleaseGateError("Source distribution is not a regular file")
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                _validate_archive_member_name(member.name, "Source distribution")
                if not member.isdir() and not member.isreg():
                    raise ReleaseGateError("Source distribution contains a non-regular archive member")
            metadata = [
                member
                for member in members
                if member.name.replace("\\", "/")
                in (f"ppt_master-{version}/PKG-INFO", f"ppt-master-{version}/PKG-INFO")
            ]
            if len(metadata) != 1:
                raise ReleaseGateError("Source distribution must contain one root PKG-INFO")
            extracted = archive.extractfile(metadata[0])
            if extracted is None:
                raise ReleaseGateError("Source distribution PKG-INFO is unreadable")
            text = extracted.read()
            _validate_core_metadata(text, version, "Source distribution")
            if source_root is not None:
                for relative in REQUIRED_SOURCE_ATTRIBUTION_RELATIVES:
                    matching = [
                        member
                        for member in members
                        if member.name.replace("\\", "/").endswith(f"/{relative}")
                    ]
                    if len(matching) != 1:
                        raise ReleaseGateError(f"Source distribution is missing static attribution file: {relative}")
                    source_file = archive.extractfile(matching[0])
                    if source_file is None or _normalized_text_bytes_from_bytes(
                        source_file.read(), relative
                    ) != _source_attribution_bytes(source_root, relative, source_sha):
                        raise ReleaseGateError(f"Source distribution attribution differs from source: {relative}")
    except ReleaseGateError:
        raise
    except (OSError, UnicodeError, tarfile.TarError) as exc:
        raise ReleaseGateError("Source distribution static inspection failed") from exc


def _parse_artifact_manifest(path: Path) -> dict[str, Any]:
    try:
        is_regular = stat.S_ISREG(os.lstat(path).st_mode)
    except OSError as exc:
        raise ReleaseGateError("Artifact manifest is unavailable or is a symlink") from exc
    if not is_regular:
        raise ReleaseGateError("Artifact manifest is unavailable or is a symlink")
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ReleaseGateError("Artifact manifest contains a non-finite JSON value")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError("Artifact manifest is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict) or set(data) != {"release_sha", "release_tag", "version", "distributions"}:
        raise ReleaseGateError("Artifact manifest has an unexpected key set")
    return data


def verify_artifact_manifest(
    artifact_dir: Path,
    manifest_path: Path,
    *,
    release_sha: str,
    release_tag: str,
    version: str,
    source_root: Path | None = None,
    source_sha: str | None = None,
) -> None:
    """Verify exact distribution files and inspect their contents without execution."""
    artifact_input = Path(artifact_dir)
    manifest_input = Path(manifest_path)
    if artifact_input.is_symlink() or not artifact_input.is_dir():
        raise ReleaseGateError("Artifact directory is unavailable or is a symlink")
    if manifest_input.is_symlink():
        raise ReleaseGateError("Artifact manifest is unavailable or is a symlink")
    artifact_dir = artifact_dir.resolve()
    manifest_path = manifest_path.resolve()
    if not artifact_dir.is_dir():
        raise ReleaseGateError("Artifact directory is unavailable or is a symlink")
    if manifest_path.parent != artifact_dir:
        raise ReleaseGateError("Artifact manifest must be inside the artifact directory")
    data = _parse_artifact_manifest(manifest_path)
    validate_version_tag(version, release_tag)
    _validate_sha(release_sha, "release_sha")
    if source_sha is not None:
        if source_root is None:
            raise ReleaseGateError("Artifact source attribution SHA requires a source root")
        _validate_sha(source_sha, "source_sha")
        if source_sha != release_sha:
            raise ReleaseGateError("Artifact source attribution SHA is not the release SHA")
    if data["release_sha"] != release_sha or data["release_tag"] != release_tag or data["version"] != version:
        raise ReleaseGateError("Artifact manifest is not bound to the release identity")
    distributions = data["distributions"]
    if not isinstance(distributions, list) or len(distributions) != 2:
        raise ReleaseGateError("Artifact manifest must contain exactly one wheel and one sdist")
    expected: dict[str, str] = {}
    for entry in distributions:
        if not isinstance(entry, dict) or set(entry) != {"filename", "sha256"}:
            raise ReleaseGateError("Artifact distribution entry has an unexpected shape")
        name = entry["filename"]
        digest = entry["sha256"]
        if not isinstance(name, str) or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ReleaseGateError("Artifact distribution entry has an invalid filename or SHA-256")
        kind = _validate_distribution_filename(name, version)
        if kind in expected.values() or name in expected:
            raise ReleaseGateError("Artifact manifest contains duplicate distributions")
        expected[name] = kind
    if set(expected.values()) != {"wheel", "sdist"}:
        raise ReleaseGateError("Artifact manifest must contain one wheel and one sdist")

    children = list(artifact_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in children):
        raise ReleaseGateError("Artifact directory contains a non-regular file")
    actual_files = children
    actual_names = {path.name for path in actual_files}
    allowed_names = set(expected) | {manifest_path.name}
    if actual_names != allowed_names:
        raise ReleaseGateError("Artifact directory contains missing or unexpected files")
    for name, kind in expected.items():
        path = artifact_dir / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_digest = next(entry["sha256"] for entry in distributions if entry["filename"] == name)
        if digest != manifest_digest:
            raise ReleaseGateError(f"SHA-256 mismatch for distribution {name}")
        if kind == "wheel":
            _validate_wheel_static(path, version, source_root, source_sha)
        else:
            _validate_sdist_static(path, version, source_root, source_sha)


def write_artifact_manifest(
    artifact_dir: Path,
    manifest_path: Path,
    *,
    release_sha: str,
    release_tag: str,
    version: str,
    source_root: Path | None = None,
    source_sha: str | None = None,
) -> None:
    """Create the strict distribution/hash manifest after a trusted build."""
    validate_version_tag(version, release_tag)
    _validate_sha(release_sha, "release_sha")
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        raise ReleaseGateError("Build artifact directory is unavailable or is a symlink")
    if manifest_path.is_symlink():
        raise ReleaseGateError("Build artifact manifest path is a symlink")
    artifact_dir = artifact_dir.resolve()
    manifest_path = manifest_path.resolve()
    if manifest_path.parent != artifact_dir:
        raise ReleaseGateError("Build artifact manifest must be inside the artifact directory")
    entries: list[dict[str, str]] = []
    for path in sorted(artifact_dir.iterdir()):
        if path.name == manifest_path.name:
            continue
        if path.is_symlink() or not path.is_file():
            raise ReleaseGateError("Build artifact directory contains a non-regular file")
        kind = _validate_distribution_filename(path.name, version)
        entries.append({"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        if kind not in {"wheel", "sdist"}:
            raise ReleaseGateError("Unexpected build distribution kind")
    if len(entries) != 2:
        raise ReleaseGateError("Build must produce exactly one wheel and one sdist")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "release_sha": release_sha,
                "release_tag": release_tag,
                "version": version,
                "distributions": entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    verify_artifact_manifest(
        artifact_dir,
        manifest_path,
        release_sha=release_sha,
        release_tag=release_tag,
        version=version,
        source_root=source_root,
        source_sha=source_sha,
    )


def emit_artifact_environment(
    artifact_dir: Path,
    manifest_path: Path,
    env_file: Path,
    *,
    release_sha: str,
    release_tag: str,
    version: str,
    source_root: Path | None = None,
    source_sha: str | None = None,
) -> None:
    """Write statically verified paths and digests to GITHUB_ENV or GITHUB_OUTPUT."""
    verify_artifact_manifest(
        artifact_dir,
        manifest_path,
        release_sha=release_sha,
        release_tag=release_tag,
        version=version,
        source_root=source_root,
        source_sha=source_sha,
    )
    data = _parse_artifact_manifest(manifest_path)
    wheel_name = next(
        entry["filename"] for entry in data["distributions"]
        if _validate_distribution_filename(entry["filename"], version) == "wheel"
    )
    sdist_name = next(
        entry["filename"] for entry in data["distributions"]
        if _validate_distribution_filename(entry["filename"], version) == "sdist"
    )
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    digests = {
        entry["filename"]: entry["sha256"]
        for entry in data["distributions"]
    }
    with env_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"WHEEL_PATH={artifact_dir.resolve() / wheel_name}\n")
        handle.write(f"SDIST_PATH={artifact_dir.resolve() / sdist_name}\n")
        handle.write(f"wheel_name={wheel_name}\n")
        handle.write(f"sdist_name={sdist_name}\n")
        handle.write(f"wheel_sha256={digests[wheel_name]}\n")
        handle.write(f"sdist_sha256={digests[sdist_name]}\n")
        handle.write(f"manifest_sha256={manifest_digest}\n")


def emit_release_environment(repo: Path, release_sha: str, env_file: Path) -> str:
    """Parse and validate both package manifests, then write safe release env values."""
    _validate_sha(release_sha, "release_sha")
    _ensure_release_head(repo, release_sha)
    root_version = _read_version(repo, "pyproject.toml", "root", release_sha)
    skill_version = _read_version(repo, "skills/ppt-master/pyproject.toml", "Skill", release_sha)
    if root_version != skill_version:
        raise ReleaseGateError("Root and Skill package versions differ")
    release_tag = f"v{root_version}"
    validate_version_tag(root_version, release_tag)
    with env_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"RELEASE_SHA={release_sha}\n")
        handle.write(f"RELEASE_VERSION={root_version}\n")
        handle.write(f"RELEASE_TAG={release_tag}\n")
    return release_tag


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run immutable PPT Master release gates.")
    parser.add_argument("--repo", default=".", help="Trusted repository root")
    parser.add_argument("--release-sha")
    parser.add_argument("--release-tag")
    parser.add_argument("--upstream-ref", default="upstream/main")
    parser.add_argument("--workflow-runs-json")
    parser.add_argument("--origin-ref", default="refs/remotes/origin/main")
    parser.add_argument("--require-origin-main-tip", action="store_true")
    parser.add_argument("--require-existing-tag", action="store_true")
    parser.add_argument("--write-artifact-manifest")
    parser.add_argument("--verify-artifact-manifest")
    parser.add_argument("--emit-artifact-env")
    parser.add_argument("--emit-release-env", action="store_true")
    parser.add_argument("--env-file")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--artifact-dir")
    return parser


def _configure_utf8_stdio() -> None:
    """Use UTF-8 replacement streams so Windows legacy consoles cannot crash the gate."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        _validate_trusted_script_directory(Path(args.repo))
        modes = sum(
            bool(value)
            for value in (
                args.emit_release_env,
                args.validate_only,
                args.verify_artifact_manifest,
                args.write_artifact_manifest,
            )
        )
        if modes > 1:
            raise ReleaseGateError("Release gate modes are mutually exclusive")
        if args.emit_release_env:
            if not args.release_sha:
                raise ReleaseGateError("Release environment emission requires --release-sha")
            env_file_value = args.env_file or os.environ.get("GITHUB_ENV")
            if not env_file_value:
                raise ReleaseGateError("Release environment emission requires --env-file or GITHUB_ENV")
            emit_release_environment(Path(args.repo), args.release_sha, Path(env_file_value))
            return 0
        if args.validate_only:
            if not args.release_sha or not args.release_tag:
                raise ReleaseGateError("Identity validation requires --release-sha and --release-tag")
            validate_release_identity(Path(args.repo), args.release_sha, args.release_tag)
            return 0
        if args.verify_artifact_manifest:
            if not args.artifact_dir or not args.release_sha or not args.release_tag:
                raise ReleaseGateError("Artifact verification requires artifact directory and release identity")
            version = validate_release_identity(Path(args.repo), args.release_sha, args.release_tag)
            verify_artifact_manifest(
                Path(args.artifact_dir),
                Path(args.verify_artifact_manifest),
                release_sha=args.release_sha,
                release_tag=args.release_tag,
                version=version,
                source_root=Path(args.repo),
                source_sha=args.release_sha,
            )
            if args.emit_artifact_env:
                emit_artifact_environment(
                    Path(args.artifact_dir),
                    Path(args.verify_artifact_manifest),
                    Path(args.emit_artifact_env),
                    release_sha=args.release_sha,
                    release_tag=args.release_tag,
                    version=version,
                    source_root=Path(args.repo),
                    source_sha=args.release_sha,
                )
            print("Static release artifact verification passed.")
            return 0
        if not args.release_sha or not args.release_tag:
            raise ReleaseGateError("Release gate mode requires --release-sha and --release-tag")
        if args.write_artifact_manifest:
            version = validate_release_identity(Path(args.repo), args.release_sha, args.release_tag)
            if not args.artifact_dir:
                raise ReleaseGateError("Artifact manifest creation requires --artifact-dir")
            write_artifact_manifest(
                Path(args.artifact_dir),
                Path(args.write_artifact_manifest),
                release_sha=args.release_sha,
                release_tag=args.release_tag,
                version=version,
                source_root=Path(args.repo),
                source_sha=args.release_sha,
            )
            print("Static build artifact manifest created and verified.")
            return 0
        if not args.workflow_runs_json:
            raise ReleaseGateError("Release gate mode requires immutable workflow-run evidence")
        run_release_gates(
            Path(args.repo),
            release_sha=args.release_sha,
            release_tag=args.release_tag,
            upstream_ref=args.upstream_ref,
            workflow_runs_json=Path(args.workflow_runs_json),
            origin_ref=args.origin_ref,
            require_origin_main_tip=args.require_origin_main_tip,
            require_existing_tag=args.require_existing_tag,
        )
        return 0
    except (OSError, ReleaseGateError, tomllib.TOMLDecodeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
