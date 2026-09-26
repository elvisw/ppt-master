# CI Online Settings Backup

Snapshot of the GitHub-side settings that are **not** stored in git but the
`ancestry-gate` deployment depends on. If the online configuration is ever
lost or drifted, use the files here to recreate it.

Exported: 2026-09-26. Refresh with the commands in "Refresh" below.

## Files

- `ruleset-main-protection.json` — the `main-protection` branch ruleset
  (id 22998489): targets `refs/heads/main`, blocks deletion and
  non-fast-forward pushes, requires the `ancestry-gate` status check.
  `bypass_actors` is empty: nobody can push to `main` directly.
- `labels.json` — repository labels, including `ci-maintenance-approved`
  (the protected-path exemption switch for maintenance PRs).
- `environment-pypi.json` — the `pypi` environment backing the manual
  approval step of `publish-pypi.yml`.
- `secrets.json` — secret **names and timestamps only**. Values can never
  be exported through the API by design.
- `variables.json` — repo variables with values (currently only
  `OPENCODE_MODEL`, not sensitive).

## Safety

This directory contains no secret values and is safe to commit:
`secrets.json` carries names only; `variables.json` holds a public model
ID. If a sensitive variable is ever added, exclude its value here.

## Restore

Recreate a missing ruleset from the snapshot:

```
gh api repos/elvisw/ppt-master/rulesets -X POST --input .github/settings-backup/ruleset-main-protection.json
```

Recreate labels / environment / variables with `gh label create`,
the environments API, and `gh variable set`. Secret **values**
(`PUSH_PAT`, `DEEPSEEK_API_KEY`) must be re-entered by the maintainer;
they cannot be recovered from any backup.

## Refresh

```
gh api repos/elvisw/ppt-master/rulesets/22998489 > .github/settings-backup/ruleset-main-protection.json
gh label list --repo elvisw/ppt-master --limit 100 --json name,color,description > .github/settings-backup/labels.json
gh api repos/elvisw/ppt-master/environments/pypi > .github/settings-backup/environment-pypi.json
gh secret list --repo elvisw/ppt-master --json name,updatedAt > .github/settings-backup/secrets.json
gh variable list --repo elvisw/ppt-master --json name,value,updatedAt > .github/settings-backup/variables.json
```
