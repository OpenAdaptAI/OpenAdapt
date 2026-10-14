# Workflow drafts for the monorepo

The files in this directory are drafts. GitHub runs workflows only from
`.github/workflows/`, so nothing here runs, and nothing here has been tried on
GitHub. The `.github/` directories inside `packages/*/` came along with each
package's history. GitHub ignores them too.

| File | Purpose |
|---|---|
| `monorepo-ci.yml` | Tests the packages a change touches, plus every package that depends on them. |
| `release-package.yml` | Releases one workspace package to its existing PyPI name. |
| `package-artifact-gates.sh` | Artifact checks from each package's standalone release workflow. |

Both workflows call `scripts/monorepo/workspace_graph.py` (CI planning and the
internal version-range check) or `scripts/monorepo/verify_pypi_files.py`
(immutable PyPI file check). The launcher keeps `release-and-publish.yml` and
its `v*` tags unchanged.

## CI: how a change selects lanes

`workspace_graph.py plan` reads every `pyproject.toml` in the workspace and
builds the dependency graph between workspace packages, including optional
extras. A changed path selects:

- the package whose directory contains it,
- every package that depends on that package, directly or through another
  package,
- every package, for changes to `uv.lock`, the root `pyproject.toml`, or the
  planner itself,
- `openadapt-viewer`, for changes under `packages/capture/examples/captures/`,
  because the viewer's tests read those recordings.

Each lane copies the sync and pytest command from the package's standalone CI.
The `workspace` job also runs `uv lock --check`, the internal version-range
check, and the source-boundary check over the whole tree.

Lanes the standalone repositories run that are not ported yet:

- openadapt-capture: Linux AT-SPI integration test under Xvfb, the `slow`
  performance test, FFmpeg pin check, live and production qualification.
- openadapt-privacy: the Presidio lane with the spaCy model.
- openadapt-agent: the dependency-floor lanes (`openadapt-flow==1.26.0`,
  `mcp==1.28.0`, `mcp==2.0.0`) and the sandbox end-to-end test with Chromium.
- openadapt-evals: the Prime environment lane, the heavy-import guard, and the
  published-evidence manifest check.
- The launcher: the Python 3.13 and 3.14 install guard and the release
  artifact checks in `main.yml`. `main.yml` itself still runs unchanged.

## Release: gates kept from each standalone workflow

Every package gets the gates the launcher, openadapt-flow, and openadapt-capture
already use: dispatch from protected main only, a reviewed version that must
already be in `packages/<pkg>/pyproject.toml`, a stale-dispatch check, a tag
that must be new and newer than the last tag for that package, green Monorepo
CI on the exact commit, an annotated tag created only by the release App,
rebuild from the exact tag, Sigstore build attestation, Trusted Publishing,
refusal of conflicting PyPI files, and byte verification after upload.

| Package | Standalone release trigger | Package-specific gates kept in the draft |
|---|---|---|
| openadapt-types | push to main, semantic-release picks the version | `check_source_boundary.py --require-dist` |
| openadapt-privacy | push to main, semantic-release picks the version | `check_source_boundary.py --require-dist` |
| openadapt-viewer | manual dispatch, semantic-release picks the version | none beyond the build |
| openadapt-capture | dispatch with reviewed version, release App tag | `check_changelog.py`, changelog entry, `check_release_ci.py`, `verify_distribution.py`, `check_source_boundary.py --require-dist`, publication check |
| openadapt-agent | `v*` tag or GitHub Release | `tests/test_distribution.py` version consistency, `check_release_artifacts.py`, `check_dist.py`, `check_source_boundary.py --require-dist`, `twine check`, MCPB validate and pack, MCP registry publish through GitHub OIDC |
| openadapt-evals | dispatch with version and commit, release App tag | lifecycle App refused, first changelog heading, `verify_release_ci.py`, `verify_release_lock.py`, `check_source_boundary.py --require-dist`, `check_third_party_notices.py --require-dist`, PyPI immutability check before and after upload |
| openadapt-flow (phase 2) | dispatch with reviewed version, release App tag | `check_release_consistency.py` (also `--require-dist`), changelog entry, `validate_claims.py --check --structure-only`, `check_release_ci.py` with a 90-minute wait |

For openadapt-types, openadapt-privacy, and openadapt-viewer this is a change of
process: a reviewed pull request sets the version, instead of
python-semantic-release committing a version bump to main with `ADMIN_TOKEN`.
That adds gates and removes none, but the founder should approve it.

Not carried over: openadapt-flow's `backfill_github_release` recovery path.
Port it with flow.

## Before enabling (none of this is done)

Settings, outside this repository:

1. On each PyPI project, add a trusted publisher: owner `OpenAdaptAI`,
   repository `OpenAdapt`, workflow `release-package.yml`, environment
   `pypi-<pkg>` (for example `pypi-types`). Keep the old publishers until the
   first monorepo release of that package succeeds.
2. Create the environments `pypi-types`, `pypi-privacy`, `pypi-capture`,
   `pypi-agent`, `pypi-evals`, `pypi-viewer` in OpenAdaptAI/OpenAdapt with the
   same reviewers and branch rules the old repositories' `pypi` or `release`
   environments have.
3. Make `Monorepo CI result` a required status check on `main`.
4. Turn on secret scanning and push protection for OpenAdaptAI/OpenAdapt.

Code, in this repository:

1. Point capture's and flow's `check_release_ci.py` and evals'
   `verify_release_ci.py` at the monorepo workflow names. Until then they fail
   closed.
2. Point evals' and types' `verify_release_lock.py` at the root `uv.lock`, then
   delete the per-package `uv.lock` files, which uv no longer reads.
3. Pin an exact `mcp-publisher` release and its SHA-256 for the agent's
   registry publish.
4. Remove the `.github/` directories under `packages/*/` and port the tests
   that read them (types, privacy, capture, agent, and evals have tests that
   read their own workflow files).
5. Move both drafts to `.github/workflows/` in one pull request, and retire
   the old repositories' release workflows in the same week.

For openadapt-flow (phase 2), also: add `flow` to the dispatch choices, the tag
patterns, and the tag regex; and change `verify-flow-admission` in
`release-and-publish.yml`, which expects the repository
`OpenAdaptAI/openadapt-flow` (ID 1291376938), together with the central
verifier in `OpenAdaptAI/.github`.
