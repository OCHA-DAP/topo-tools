---
name: publishing
description: Use when cutting a topo-tools PyPI release, rehearsing a release on TestPyPI, or checking the release/publish workflow setup (GitHub Environment approval gate, PyPI trusted publisher, Homebrew tap, conda-forge feedstock).
---

# Publishing to PyPI

## One-time setup (already done)

- GitHub Environment `pypi` on this repo, with a required-reviewer protection rule (repo Settings → Environments). Approval is required before the `publish` job in `.github/workflows/publish.yml` can run, even after a Release is published.
- Pending trusted publisher registered at <https://pypi.org/manage/account/publishing/>: project `topo-tools`, owner `OCHA-DAP`, repo `topo-tools`, workflow `publish.yml`, environment `pypi`. This authorizes GitHub Actions to publish via OIDC, no long-lived API token stored anywhere.
- The publisher's owner and repo must match the GitHub repo exactly. After a repo rename or transfer, update the publisher at the link above before the next release, or the publish job's OIDC exchange fails with `invalid-publisher`.
- `.github/workflows/publish.yml` triggers only on `release: published`, never on push/tag alone.
- `HOMEBREW_TAP_TOKEN` repository secret: a fine-grained PAT scoped to `OCHA-DAP/homebrew-topo-tools` (`Contents: Read and write`, `Pull requests: Read and write`), used by `.github/workflows/homebrew-tap.yml` to open formula-bump PRs. Expires 2027-08-27; renew it before then or that job starts failing.

None of the above needs to be redone for future releases. It's specific to real PyPI; TestPyPI (below) is a fully separate service with its own account/tokens and isn't wired into this workflow.

## Cutting a real release

1. Bump `version` in `pyproject.toml`.
2. Move `CHANGELOG.md`'s `[Unreleased]` entries under a new `## [x.y.z] - YYYY-MM-DD` heading, and add a matching link reference at the bottom of the file.
3. Merge to `main` (release-triggered workflows run from the default branch).
4. GitHub → Releases → Draft a new release → tag it (e.g. `v0.1.1`) → Publish release.
5. Approve the `publish` job in the Actions run (the required-reviewer gate from the environment setup above).
6. `.github/workflows/homebrew-tap.yml` picks up the new version on its first daily run after the PyPI upload is more than 24h old, and opens a formula-bump PR against `OCHA-DAP/homebrew-topo-tools` if the tap is behind. No manual step needed here.
7. conda-forge's autotick bot opens a version-bump PR on `conda-forge/topo-tools-feedstock` after the PyPI upload, and automerges it once CI passes. No manual step needed here; check the feedstock only if a release doesn't appear on conda-forge.

Version numbers, once uploaded, are permanent: PyPI never allows re-uploading the same filename again, even after deletion. Staying in `0.x` (SemVer's "no compatibility promises yet" range) means there's no expectation of a steady cadence or of never breaking the CLI/API between releases.

## Rehearsing on TestPyPI

TestPyPI (test.pypi.org) is a separate index for exactly this: testing packaging/publishing mechanics without touching the real project. Same permanent-filename rule applies there too, so each rehearsal attempt needs its own version bump; use `.devN` suffixes (e.g. `0.1.1.dev1`, `0.1.1.dev2`, ...) to keep these visually distinct from real release versions.

Requires a `.env` file (gitignored) in the repo root with `UV_PUBLISH_TOKEN=pypi-...`, generated at <https://test.pypi.org/manage/account/token/> (account-scoped, since the project doesn't exist there yet on first use).

```bash
# 1. bump version in pyproject.toml, e.g. to 0.1.1.dev1
# 2. build
rm -rf dist && uv build
# 3. publish
set -a; source .env; set +a
uv publish --publish-url https://test.pypi.org/legacy/
# 4. verify install + CLI actually works
rm -rf /tmp/testpypi-check
uv venv /tmp/testpypi-check
uv pip install --python /tmp/testpypi-check \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  topo-tools==<version>
/tmp/testpypi-check/bin/topo-tools edge-extend --help
```

The `--extra-index-url` fallback is required: TestPyPI doesn't mirror `click`/`duckdb`/`psutil`, only whatever you've published there yourself. Full version history: <https://test.pypi.org/project/topo-tools/#history>.
