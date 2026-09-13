# Lean Controlled AI Upstream Merge

**Status:** Approved scope correction

## Problem

The scheduled upstream sync aborts whenever `git merge --no-ff --no-commit`
reports conflicts. The AI therefore never receives an owned merge state to
resolve. The trusted checker also requires every non-policy resolution to equal
upstream byte-for-byte, so a newly overlapping fork adaptation has no automatic
authorization path.

## Existing Safety Boundary

The implementation on `origin/main` already provides the required trust split:

- the AI job has no `PUSH_PAT` and only produces a Git bundle;
- a fresh runner verifies the immutable base, target, candidate, merge parents,
  protected paths, content boundary, version bump, and repository gates;
- the fresh runner pushes only the verified candidate and opens a PR;
- `ancestry-gate` and human merge approval remain required.

`check_upstream_ancestry.py::_verify_content_boundary()` already computes the
upstream changed set `U` from immutable Git objects. Extend it with only the fork
changed set `F` over the same merge base and derive `D = U intersect F`:

- a path in `U - D` must still match upstream exactly;
- a path in `D` may contain an AI resolution without a static `merge` policy,
  but must not silently equal the first parent;
- an explicit `retain-base` policy remains authoritative;
- existing exact `merge` policy entries remain valid;
- protected-path and post-merge drift checks remain unchanged.

## Change

Change the content-boundary calculation plus the conflict path in
`.opencode/command/sync-upstream.md`:

1. Run `git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"` and capture its
   exit code.
2. Exit code `0` continues through the existing clean-merge path.
3. Exit code `1` may continue only when all ownership checks hold:
   `HEAD == ORIGINAL_HEAD`, `ORIG_HEAD == ORIGINAL_HEAD`,
   `MERGE_HEAD == EXPECTED_UPSTREAM_SHA`, and `git ls-files -u` is non-empty.
4. In that owned-conflict state, leave the merge open and continue into the
   existing Step 3 conflict-resolution guidance.
5. Any other exit or malformed state invokes the existing ownership-checked
   cleanup and fails. Cleanup aborts only this run's owned merge; it refuses to
   mutate foreign merge state.
6. The common Step 2 tail still verifies HEAD, ORIG_HEAD and MERGE_HEAD, then
   writes/stages the marker before handing control to Step 3. The existing
   completion block remains authoritative: no unresolved index, unchanged first
   parent, all local gates, and a real two-parent merge commit.

The AI must not resolve conflicts in protected CI or release-policy files. Such
a run is rejected authoritatively by the trusted checker and requires a normal
maintenance PR; the command does not copy the protected-path list into a second
owner.

## Files

- `.opencode/command/sync-upstream.md`
- `.github/scripts/check_upstream_ancestry.py`
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
3. Foreign `MERGE_HEAD` or `ORIG_HEAD` is preserved and fails; an owned malformed
   exit-1 state is safely aborted.
4. The completion block rejects any unresolved entry, while a fully resolved
   conflict reaches Step 6 and creates the required two-parent merge.
5. A path changed by both fork and upstream accepts a merged resolution without
   a static policy entry, but rejects silently retaining the fork version.
6. Existing clean-merge, ancestry, protected-path, version, and credential
   tests remain green.

## Rollout

Create one protected maintenance PR with `ci-maintenance-approved`, because the
command file and checker are protected. After merge, first verify in a throwaway
local merge that current upstream has at least one real conflict; otherwise the
run can prove only the clean path. Then run one real `workflow_dispatch` against
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
