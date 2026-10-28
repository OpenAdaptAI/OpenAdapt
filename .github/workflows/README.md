# CI tiers

The org is on GitHub's free Actions plan: 20 concurrent jobs, 5 of them
macOS. Every pull request and every queue group competes for those runners.
To keep pull requests fast, CI runs in three tiers:

- **PR tier** (`pull_request`): one fast Linux lane. That's one Python
  version (3.12) and only the packages a change touches, including Flow's
  unit suite. The checkout is the pull request merged with main, so this tier
  already tests the change on top of main.
- **Queue tier** (`merge_group`), when the merge queue is on (ruleset
  25486718; it is off as of 2026-10-25, so a pull request merges once its
  PR-tier checks pass and the PR tier is the only gate before main): a lean re-check of the group on top of the
  newest main. It runs the five required checks and a cheap but meaningful
  set: Flow lint and the critical golden path (VERIFIED, zero model calls),
  each touched package's Linux suite, and Desktop's frontend and Linux
  Python tests. It skips what the pull request already passed and what only
  differs by platform: Flow's unit shards, the Linux sidecar, macOS,
  Windows, older Pythons and the native installers.
- **Post-merge tier** (`push` to `main`, `workflow_dispatch`, schedules):
  everything. That covers Flow's unit shards, macOS and Windows jobs, older
  Python versions, the native installers and the macOS Intel audit. A failure
  here lands on main, so fix or revert it at once. `desktop-release.yml`
  starts only from a passing push-to-main Packages run whose desktop jobs
  (including the native installers) ran, so no Desktop release ships without
  this tier.

- **Nightly** (`nightly.yml`, daily at 07:23 UTC and on dispatch): calls
  Packages (with the Desktop test and build workflows) and the launcher CI as
  a scheduled run, so every package's full suite runs against main on every
  platform its CI covers, whatever changed. The merge queue's push and a pull
  request run only the packages a change touches; this catches a package
  nobody touched breaking on main. When a job fails, the run opens the
  `main is red` issue (label `main-is-red`), or updates it if one is open. A
  passing run closes it. The nightly run is not a required check.

**Drafts run nothing.** Every job in a workflow that runs on pull requests
checks `github.event.pull_request.draft != true`, and `ready_for_review` is a
trigger, so marking a draft ready starts CI. `packages-gate` fails on a draft
with "draft PR: CI runs when marked ready for review", so a draft never shows
a stale green. A draft can't merge anyway.

Nothing that used to run before merge was dropped: every job removed from the
PR and queue tiers runs in the post-merge tier.

## Required checks

Branch protection requires these five checks, both on pull requests and in the
merge queue:

| Check | Workflow | On a PR | In the queue |
|---|---|---|---|
| `gitleaks` | secret-scan.yml | runs | runs |
| `dependency-review` | dependency-review.yml | runs | runs |
| `Analyze (python)` | codeql.yml | reports; analyzes when Python files change | reports; analyzes when the group changes Python files |
| `run-ci (ubuntu-latest, 3.12)` | main.yml | runs (skips the suite for package-only changes) | runs |
| `packages-gate` | packages.yml | runs; fails on a draft | runs |

`packages-gate` needs every job in packages.yml. It fails when a job fails. It
also fails when a job the change selects was skipped, except on a tier where
that job doesn't run:

- Post-merge-only jobs (`capture-macos`, `capture-windows`) may be skipped on
  a pull request and in the queue, and must run and pass on main and on
  dispatch.
- `flow-unit` may be skipped in the queue only. A pull request can't enter
  the queue until `packages-gate` passed with it green.
- `desktop-release-intent` runs on pull requests only.

Inside the reusable desktop workflows, the native installers and the macOS and
Windows legs run only post-merge. The gate sees each reusable workflow as one
result, so a tier-skipped job inside it is never "skipped although selected".

Jobs in other workflows that run only post-merge, such as main.yml's macOS
and older-Python legs and the macOS Intel audit, are not required checks.
Adding their check names to branch protection would block every pull request,
because they never report there.

## Jobs by tier

| Workflow | Job | PR tier | Queue tier | Post-merge tier |
|---|---|---|---|---|
| main.yml | `run-ci` | ubuntu-latest, Python 3.12 | ubuntu-latest, Python 3.12 | ubuntu-latest and macos-latest, Python 3.10 to 3.12 |
| main.yml | `unsupported-python-guard` | no | no | Python 3.13 and 3.14 |
| packages.yml | `types`, `agent` | Python 3.12 | Python 3.12 | Python 3.10 and 3.12 |
| packages.yml | `flow-lint`, `flow-critical` | yes | yes | yes |
| packages.yml | `flow-unit` | 2 xdist shards | no | 2 xdist shards |
| packages.yml | `capture-linux` | Python 3.12 | Python 3.12 | Python 3.10 (ubuntu-22.04), 3.11, 3.12 |
| packages.yml | `capture-macos`, `capture-windows` | no | no | yes |
| packages.yml | `capture-contract`, `capture-core-record` | yes | yes | yes |
| packages.yml | `privacy` | Python 3.12 | Python 3.12 | Python 3.10 to 3.12 |
| packages.yml | `privacy-presidio`, `privacy-contract` | yes | yes | yes |
| packages.yml | `tray` | Linux, Python 3.12 | Linux, Python 3.12 | Linux 3.10 to 3.12, macOS and Windows 3.12 |
| packages.yml | `tray-contract`, `tray-desktop-ipc`, `program-graph`, `install-weight` | yes | yes | yes |
| packages.yml | `desktop-release-intent` | yes | no | no |
| desktop-test.yml | `test-python` | ubuntu-latest, Python 3.12 | ubuntu-latest, Python 3.12 | Linux, macOS and Windows, Python 3.11 and 3.12 |
| desktop-test.yml | `qualification-contract` | yes | yes | yes |
| desktop-build.yml | `frontend`, `python-distribution` | yes | yes | yes |
| desktop-build.yml | `python-sidecar` | ubuntu-22.04 | no | Linux, macOS 14 and Windows |
| desktop-build.yml | `native-installers` | no | no | macOS arm64, macOS Intel, Windows, Linux bundles |
| desktop-dependency-audit.yml | `python`, `npm`, `rust` | when Desktop changes | no | when Desktop changes, and weekly |
| desktop-dependency-audit.yml | `python-macos-intel` | no | no | when Desktop changes, and weekly |
| codeql.yml | `Analyze` (Python, JavaScript/TypeScript, Rust) | per changed language | per changed language | all languages on main and weekly |
| secret-scan.yml, dependency-review.yml, platform-manifest.yml | all | yes | yes | yes |
| docs.yml, link-check.yml (internal), production-lifecycle.yml, release-health.yml (self-test) | all | yes | no | main only |

packages.yml jobs run only for the packages a change touches. The `scope` job
decides from the PR diff, the queue group's diff against its base commit, or
the push's diff against the previous main.

Flow's unit shards run `pytest -n auto --dist loadfile`: four xdist workers
per runner, one test file per worker at a time, each worker with its own
`--basetemp` directory. A test file that can't share the runner with others
should get its own serial step rather than turning xdist off.

## Concurrency

Every workflow that runs on pull requests uses one rule. A newer push to a
pull request cancels the older run. Every other event gets a group keyed by
its run ID, so a queue group, a push to main, a schedule or a dispatch is
never cancelled or replaced:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

Three kinds of workflow keep their own groups:

- The release workflows use a fixed group and never cancel, so releases run
  one at a time.
- `desktop-release.yml` follows the same pattern.
- `packages.yml` has its own rule (#1398). A push to main that doesn't touch
  Desktop may cancel an older one, because `desktop-release.yml` waits only
  for runs on commits that touch Desktop. Queue groups and dispatches are never
  cancelled.

## Caches

The Flow and agent jobs install with `uv pip install --system` into the
`actions/setup-python` interpreter, with the uv cache keyed on the pyproject
files they resolve; uv takes seconds where pip took two to three minutes. The
other pip jobs use `cache: pip`. Every other `setup-uv` step sets
`enable-cache: true` and keys the cache on its package's own `uv.lock`.
npm uses `cache: npm`. The Tauri builds use `Swatinem/rust-cache`, and the
Rust audit caches its `cargo-audit` binary. The release workflows keep caching
off on purpose: release builds start from a clean cache.

## Merge queue settings

How many queue runs happen per merged pull request is a repository setting,
not a workflow one. With "Maximum pull requests to build" at 3, the queue
builds up to three overlapping groups at once ([A], [A,B], [A,B,C]), and each
runs the queue tier. The queue tier is lean, so overlap costs less than it
used to. Each merge then runs the post-merge tier once per push to main, so
batching several pull requests into one group saves that too. To batch, build one
group at a time and let it collect several pull requests, for example: build
1, merge at least 1 and at most 10, and wait 5 minutes to fill a group. Those
are branch-protection settings, so an admin changes them. How GitHub fills a
group at those values hasn't been tested here; check the result on the next
few queue runs.

## Dependabot

Each ecosystem and directory opens at most one grouped version-update pull
request a week (`.github/dependabot.yml`). Security updates are not grouped.
