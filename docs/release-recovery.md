# Recovering a release after Actions were disabled

This guide applies to the release workflows in `elvisw/ppt-master`.
Workflow files and release gates remain the source of truth.

## Identify the missing stage

The normal chain is:

```text
push main -> Check UVX Migration -> Auto-tag verified release
          -> push release tag -> Publish to PyPI -> pypi environment approval
```

Before retrying, check the release commit, the synchronized version in both
`pyproject.toml` files, the exact release tag, workflow runs, and PyPI.
Do not assume that enabling Actions recreated a missing run.

- The migration workflow accepts pushes to `main`, not manual dispatches.
- Auto-tag runs after a successful migration workflow and requires its event
  to be `push` on `main` in this repository.
- Both tagging and publication require successful migration-run evidence for
  the exact release SHA. A local check or another commit's run is insufficient.
- Manual publication must be dispatched from the exact release tag, with
  matching `release_sha` and `release_tag` inputs; selecting `main` is rejected.

## Recover when the main push has no migration run

After confirming all required workflows are enabled, create a normal PR from
the current `origin/main` with a useful recovery-documentation change. Merge
it through the required checks using a merge commit. This creates a new
`push main` event for a commit containing the previously merged changes.

Keep the existing package version only if it represents pending functional
changes and neither its release tag nor its PyPI release exists. Do not bump
an already-unpublished version merely to retry, and never reuse a published
version. The resulting release SHA is the new merge commit, not the commit
whose push event was missed.

Follow migration, auto-tag, build, and artifact verification before approving
the `pypi` environment. Confirm the published version and distributions on
PyPI before declaring recovery complete.

Do not move `main` backwards, recreate existing tags, manufacture workflow
evidence, or relax a gate to recover a missed event. Re-running an older
workflow retains its original SHA and ref, so it does not validate the new
commit. If the original commit has no migration run, manual publication or
manual tag creation alone cannot supply that missing evidence.
