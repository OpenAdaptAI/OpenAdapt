# Monorepo preview (dry run, do not merge)

This branch tests one way to move the core OpenAdapt packages into this
repository: a uv workspace under `packages/`, with each package still published
to its own PyPI name. It is a preview. Nothing outside this branch was changed:
no default branch, repository setting, archive, tag, PyPI project, or workflow
run.

Results below were measured on 2026-10-14 on macOS (arm64) with Python 3.12.13
and uv 0.11.29. `results.json` holds the numbers and `tag-map.tsv` the tag plan.

## What the branch contains

1. **`legacy/` leaves the tree.** The frozen pre-1.0 monolith is removed from
   `main`. The plan is to keep it on an archive branch `archive/legacy` and an
   annotated tag `legacy-final`, both at
   `60938ae0e8d52badaa557ac71b1b8abb60e97053` (the `main` commit this branch
   starts from). Neither ref is pushed. A separate archive repository built
   with `git subtree split --prefix=legacy` would hold only 5 commits, because
   `legacy/` was created by a move, so the branch keeps the full history and
   the split does not.
2. **Six packages imported with full history**, using
   `git subtree add --prefix=packages/<name> <url> main` without `--squash`.
   Their commits keep their original hashes.

   | Directory | PyPI name | Version in tree | Source commit | Commits |
   |---|---|---|---|---|
   | `packages/types` | openadapt-types | 0.19.0 | `c9113b82b7` | 69 |
   | `packages/privacy` | openadapt-privacy | 1.1.1 | `8f887cfb65` | 35 |
   | `packages/capture` | openadapt-capture | 1.3.0 | `5d54530af3` | 143 |
   | `packages/agent` | openadapt-agent | 2.0.2 | `4b0b9bb780` | 54 |
   | `packages/evals` | openadapt-evals | 0.97.1 | `d40b8509f6` | 562 |
   | `packages/viewer` | openadapt-viewer | 0.2.0 | `6cfad96af7` | 49 |

3. **Two tree cleanups after import.** Three generated evals comparison pages
   (54 MB of HTML with inline base64 screenshots, linked from nowhere) are
   left out of the tree because random letters in the base64 data trip the
   pre-push name guard. Five organization planning files are dropped from
   `packages/viewer`. Both sets stay in the imported history.
4. **The root becomes a uv workspace** (`[tool.uv.workspace] members =
   ["packages/*"]`). The root still builds the `openadapt` launcher. Workspace
   sources are declared once at the root, so no member `pyproject.toml`
   changed. One `uv.lock` covers the root and all members.
5. **Fixes the first workspace run called for:** root `.gitignore` patterns
   anchored to the root, a SQLAlchemy constraint for capture's fixture
   builder, and a source-boundary rule that accepts exact copies of the
   rendered policy inside packages.
6. **Tooling:** `scripts/monorepo/workspace_graph.py` (CI planning and a
   check of internal version ranges), `scripts/monorepo/verify_pypi_files.py`
   (PyPI immutability check for any package), and tests for both.
7. **Workflow drafts** in `.github/workflow-drafts/`, which GitHub does not
   run: a path-filtered CI and a per-package release workflow. Their README
   lists the gates kept from each package and what must happen before they
   are enabled.

openadapt-flow is not imported. The import audit found files in its history
that cannot enter this MIT repository, so its history must be rewritten before
it joins.

## Builds

`uv build --package <name>` in the workspace, compared with `uv build` in a
fresh clone of each standalone repository at the same commit, and with the
METADATA file PyPI serves for the published wheel.

| Package | Wheel same as standalone | Sdist same as standalone | METADATA vs PyPI |
|---|---|---|---|
| openadapt-types 0.19.0 | yes, and byte-identical to the PyPI wheel | yes | identical |
| openadapt-privacy 1.1.1 | yes, and byte-identical to the PyPI wheel | yes | identical |
| openadapt-capture 1.3.0 | yes | yes | 1.3.0 is not on PyPI; differs from 1.2.2 as expected |
| openadapt-agent 2.0.2 | yes | yes | 2.0.2 is not on PyPI; differs from 2.0.1 as expected |
| openadapt-evals 0.97.1 | yes | no: the three removed HTML pages | README text changed on `main` after the release |
| openadapt-viewer 0.2.0 | yes | no: the five removed planning files | `main` changed the ruff pin and README after the release |
| openadapt 1.16.0 (root) | METADATA differs only in the README paragraph about `legacy/` | differs in the files this branch edits: `README.md`, `.gitignore`, `pyproject.toml`, and `PKG-INFO` | `main` changed after 1.16.0 was published |

Every PyPI name and version is unchanged. Each package's own artifact checks
(`check_source_boundary.py --require-dist`, `verify_distribution.py`,
`check_release_artifacts.py`, `check_dist.py`, `check_third_party_notices.py`)
pass on the workspace builds. `twine check` and the agent's MCPB packing were
not run locally.

## Tests

Each package's own test command from its standalone CI, run in a fresh clone of
that repository (with its own lock) and again inside the workspace (with the
shared lock). The launcher's standalone run installs dependencies the way its
CI does, from PyPI.

| Package | Standalone | Workspace, first run | Workspace, after fixes |
|---|---|---|---|
| openadapt-types | 653 passed, 1 skipped | same | same |
| openadapt-privacy | 120 passed, 2 skipped | same | same |
| openadapt-capture | 846 passed, 4 skipped | 840 passed, 10 skipped | 846 passed, 4 skipped |
| openadapt-agent | 335 passed, 6 skipped | same | same |
| openadapt-evals | 2715 passed, 57 skipped | 2684 passed, 19 failed, 12 errors | unchanged (see finding 1) |
| openadapt-viewer | 193 passed, 45 skipped | same | same |
| openadapt (root) | 347 passed, 1 skipped | 346 passed, 1 failed | all pass, plus the new tests |

## Findings

1. **openadapt-evals breaks against the in-tree openadapt-types.** evals
   declares `openadapt-types>=0.17.0,<0.18.0`; the tree has 0.19.0. All 31
   failures are one error: `score() missing 1 required keyword-only argument:
   'contract'`. With openadapt-types 0.17.1 installed in the same environment,
   the 61 tests in the affected files pass. A workspace has one version of each
   package, so evals has to move to types 0.19 before it can join, or types has
   to stay behind. This needs a decision and a change in evals.
2. **uv does not enforce a declared range for a workspace source.** It resolved
   evals to types 0.19.0 even though evals excludes it. Published wheels keep
   the declared range, so a package could be tested against a sibling version
   its users can never install. `workspace_graph.py check-pins` reports this;
   today it fails on exactly the evals range above.
3. **The launcher's development environment gets openadapt-flow 1.34.0, not
   1.35.1.** Flow 1.35.1 requires `openadapt-types<0.18.0`, which the
   workspace excludes. This stays true until flow joins the workspace or
   publishes a release that accepts types 0.19.
4. **One lock changes versions for every package.** Compared with each
   standalone environment, between 1 and 42 installed dependencies have a
   different version per package. Apart from finding 1, one change affected a
   result: capture's fixture byte checks run only with
   SQLAlchemy 2.0.52, so six of them skipped until the root pinned it.
5. **The root `.gitignore` hid package files.** Unanchored patterns such as
   `src`, `data`, `*.db`, and `*.txt` would have made git ignore new files under
   `packages/agent/src/`, `packages/viewer/src/`, and capture's example
   recordings. They are now anchored to the root.
6. **The source-boundary check flagged each package's copy of the rendered
   policy.** The copies are byte-identical to the root policy and are now
   accepted only while they stay identical. An edited copy is still scanned.
7. **Python range.** agent caps Python below 3.13, so the shared lock resolves
   for `>=3.10,<3.13`. The root still publishes `Requires-Python >=3.10`; the
   3.13 and 3.14 install guard in `main.yml` builds with pip and is not
   affected.
8. **Tags.** All 257 tags of the six packages (256 version tags and one evals
   preregistration tag) point at commits that are now in this history, so each can be recreated here with a prefix
   (`v0.19.0` becomes `types-v0.19.0`). `tag-map.tsv` lists them. No tag was
   created or pushed.

## Not done in this preview

- openadapt-flow is not imported (history rewrite first), and neither are
  desktop or tray.
- No tag, archive branch, or release was created on GitHub. No repository was
  archived or renamed, no issue was transferred, and no setting or PyPI
  publisher was changed.
- The workflow drafts are not enabled and have not run on GitHub. Lanes not
  yet ported and the script changes they need are listed in
  `.github/workflow-drafts/README.md`.
- The `.github/` directories and `uv.lock` files inside `packages/*/` are left
  as imported. GitHub and uv ignore them; several package tests still read
  them.
- The six per-package copies of `source-policy.public.json` are not yet
  collapsed into the root copy.
- evals is not fixed for openadapt-types 0.19 (finding 1).

## Reproduce

```bash
uv lock --check
uv run --no-project --with packaging python scripts/monorepo/workspace_graph.py check-pins
UV_PROJECT_ENVIRONMENT=.venv-types uv sync --locked --package openadapt-types --extra dev
(cd packages/types && ../../.venv-types/bin/python -m pytest -q)
uv build --package openadapt-types --out-dir dist/openadapt-types
```

The other packages use the commands in `scripts/monorepo/workspace_graph.py`
(`LANES`).
