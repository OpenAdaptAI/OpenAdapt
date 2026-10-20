# Folded repositories

Each package under `packages/` was a separate repository. It was imported here
with its full history, so `git log` and `git blame` under `packages/<name>/`
reach back past the fold.

| Package | Source repository | Freeze commit | Import PR | Commit map |
|---|---|---|---|---|
| `packages/flow` | OpenAdaptAI/openadapt-flow | `eced8929` | #1178 | [openadapt-flow-commit-map.tsv](openadapt-flow-commit-map.tsv) |
| `packages/types` | OpenAdaptAI/openadapt-types | `dd1e3db0` | #1178 | [openadapt-types-commit-map.tsv](openadapt-types-commit-map.tsv) |
| `packages/agent` | OpenAdaptAI/openadapt-agent | `f5ef1d7d` | #1178 | [openadapt-agent-commit-map.tsv](openadapt-agent-commit-map.tsv) |
| `packages/capture` | OpenAdaptAI/openadapt-capture | `612412c4` | #1205 | [openadapt-capture-commit-map.tsv](openadapt-capture-commit-map.tsv) |
| `packages/privacy` | OpenAdaptAI/openadapt-privacy | `4938a8e7` | #1266 | [openadapt-privacy-commit-map.tsv](openadapt-privacy-commit-map.tsv) |
| `packages/desktop` | OpenAdaptAI/openadapt-desktop | `9447a0a1` | #1267 | [openadapt-desktop-commit-map.tsv](openadapt-desktop-commit-map.tsv) |

## How a fold is done

1. Fetch the source repository's `main` into a new, empty scratch repository
   (`git init` and `git fetch`, no tags). For a repository whose history was
   rewritten, fetch from the current remote, never from an old local clone.
2. Rewrite it with
   `git filter-repo --to-subdirectory-filter packages/<name> --preserve-commit-hashes`.
   Commit hashes change; commit messages keep the old hashes they cite.
3. Scan the rewritten history: gitleaks with this repository's
   `.gitleaks.toml`, and the private name guard.
4. Join it here with one `git merge --allow-unrelated-histories --no-ff`. No
   existing history in this repository is rewritten.
5. Write `docs/monorepo/<repo>-commit-map.tsv` from filter-repo's
   `.git/filter-repo/commit-map` (old hash, new hash).
6. Wire the package into `.github/workflows/packages.yml` (a path-filtered
   scope plus jobs listed in `packages-gate`) and into
   `.github/workflows/release-package.yml`.
7. Merge the PR with **Create a merge commit**. Squash or rebase drops the
   imported history.

## Releases and archiving

A folded package releases from here: run the `Release package` workflow from
`main`. It publishes through the package's `pypi-<name>` environment and tags
`<pypi-project>-v<version>`. Tags from before the fold stay in the source
repository.

openadapt-desktop releases differently, because its native installers belong in
the same release: when a reviewed version bump merges and its Packages run on
main succeeds, `desktop-release.yml` builds the wheel, the sdist and every
installer, then publishes one immutable `openadapt-desktop-vX.Y.Z` release and
the PyPI package (see `packages/desktop/RELEASES.md`). Its Production channel and FFmpeg runtime
lanes have not moved.

A source repository is archived, never deleted, after its package has
published one release from this repository. Its commits are the "old" side of
the commit map.
