# Lean Controlled AI Upstream Merge

**Status:** Approved scope correction

## Problem

The scheduled upstream sync aborts whenever `git merge --no-ff --no-commit`
reports conflicts. The AI therefore never receives an owned merge state to
resolve, even though the trusted checker already permits reviewed content on
paths changed by both fork and upstream.

## Existing Safety Boundary

The implementation on `origin/main` already provides the required trust split:

- the AI job has no `PUSH_PAT` and only produces a Git bundle;
- a fresh runner verifies the immutable base, target, candidate, merge parents,
  protected paths, content boundary, version bump, and repository gates;
- the fresh runner pushes only the verified candidate and opens a PR;
- `ancestry-gate` and human merge approval remain required.

`check_upstream_ancestry.py::_verify_content_boundary()` already computes fork
divergence from immutable Git objects. A path changed by both sides is accepted
without a static `merge` policy entry, while an upstream-only path must match
upstream exactly. This behavior needs a focused regression test, not a rewrite.

## Change

Change only Step 2 of `.opencode/command/sync-upstream.md`:

1. Run `git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"` and capture its
   exit code.
2. Exit code `0` continues through the existing clean-merge path.
3. Exit code `1` may continue only when all ownership checks hold:
   `HEAD == ORIGINAL_HEAD`, `ORIG_HEAD == ORIGINAL_HEAD`,
   `MERGE_HEAD == EXPECTED_UPSTREAM_SHA`, and `git ls-files -u` is non-empty.
4. In that owned-conflict state, leave the merge open and instruct the AI to
   resolve every conflict while preserving both upstream behavior and fork uvx
   adaptations.
5. Any other exit or malformed state runs the existing ownership-checked abort
   path and fails.
6. The existing completion block remains authoritative: no unresolved index,
   unchanged first parent, exact target in `MERGE_HEAD`, marker update only after
   resolution, all local gates, and a real two-parent merge commit.

The AI must not resolve conflicts in protected CI or release-policy files. Such
a run fails closed and requires a normal maintenance PR.

## Files

- `.opencode/command/sync-upstream.md`
- `docs/zh/upstream-sync.md`
- `skills/ppt-master/scripts/tests/test_sync_upstream_ownership.py`
- `skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py`

No workflow, credential, release-gate, package-installation, or runner topology
changes are in scope.

## Tests

Use six focused behaviors:

1. An ordinary content conflict remains open with the expected `MERGE_HEAD` and
   unresolved index for AI resolution.
2. Merge exit `1` without a valid owned conflict aborts and fails.
3. Wrong `MERGE_HEAD` or changed `ORIG_HEAD` aborts and fails.
4. The completion block rejects any unresolved entry.
5. A path changed by both fork and upstream accepts a merged resolution without
   a static policy entry, but rejects silently retaining the fork version.
6. Existing clean-merge, ancestry, protected-path, version, and credential
   tests remain green.

## Rollout

Create one protected maintenance PR with `ci-maintenance-approved`, because the
command file is protected. After merge, run one real `workflow_dispatch` against
the immutable current upstream target. Success requires logs proving that the AI
entered the conflict-resolution path, created a real two-parent merge plus one
version commit, passed the fresh-runner gates, and opened a sync PR. Human review
and merge remain mandatory.

## Non-Goals

- treating the AI as a hostile multi-tenant workload;
- adding another runner, test receipt protocol, or workflow digest owner;
- replacing npm installation or changing release supply-chain policy;
- proving arbitrary AI semantic choices mechanically;
- expanding the static overlay policy for every future overlap.

The archived `design/controlled-ai-upstream-merge` branch contains the rejected
high-assurance experiment and is not an implementation dependency.
