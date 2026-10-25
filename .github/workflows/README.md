# CI tiers

The org is on GitHub's free Actions plan, so concurrent runners are scarce.
Macs have the tightest limit. Every pull request competes with every other
pull request for runners. To keep pull requests fast, CI runs in three tiers:

- **PR tier** (`pull_request`): one fast Linux lane. That's one Python
  version (3.12) and only the packages a change touches. It gives a quick
  signal while you iterate.
- **Queue tier** (`merge_group`): everything else that must pass before a
  change reaches main. That covers macOS and Windows jobs, older Python
  versions, native installers and the macOS Intel audit. The merge queue
  tests each group on top of main and merges only a group that passes.
- **Main** (`push` to `main`, `workflow_dispatch`, schedules): release and
  monitoring workflows. When the merge queue pushes the commit its group just
  tested (actor `github-merge-queue[bot]`), the Python, Linux-matrix and
  capture macOS/Windows jobs run only the PR-tier lane, so the heavy jobs run
  once per batch, in the queue. A push by anyone else runs the full queue
  tier. Desktop's jobs always run in full on main, because
  `desktop-release.yml` reads its release evidence from the Packages push run.

**Drafts run nothing.** Every job in a workflow that runs on pull requests
checks `github.event.pull_request.draft != true`, and `ready_for_review` is a
trigger, so marking a draft ready starts CI. Required checks don't report on a
draft, and a draft can't merge anyway.

Nothing that used to run before merge was dropped: every job removed from the
PR tier runs in the queue tier.

## Required checks

Branch protection requires these five checks, both on pull requests and in the
merge queue:

| Check | Workflow | On a PR | In the queue |
|---|---|---|---|
| `gitleaks` | secret-scan.yml | runs | runs |
| `dependency-review` | dependency-review.yml | runs | runs |
| `Analyze (python)` | codeql.yml | reports; analyzes when Python files change | reports; analyzes when the group changes Python files |
| `run-ci (ubuntu-latest, 3.12)` | main.yml | runs (skips the suite for package-only changes) | runs |
| `packages-gate` | packages.yml | runs | runs |

`packages-gate` needs every job in packages.yml. It fails when a job fails. It
also fails when a job the change selects was skipped, except on the tier where
that job doesn't run:

- Queue-only jobs (`capture-macos`, `capture-windows`) may be skipped on a pull
  request but must run and pass in the queue.
- `desktop-release-intent` runs on pull requests only.

Jobs in other workflows that run only in the queue, such as main.yml's macOS
and older-Python legs and the macOS Intel audit, are not required checks. They
run in the queue and on main but don't block a merge. Adding their check names
to branch protection would make them blocking.

## Jobs by tier

| Workflow | Job | PR tier | Queue and main |
|---|---|---|---|
| main.yml | `run-ci` | ubuntu-latest, Python 3.12 | ubuntu-latest and macos-latest, Python 3.10 to 3.12 |
| main.yml | `unsupported-python-guard` | no | Python 3.13 and 3.14 |
| packages.yml | `types`, `agent` | Python 3.12 | Python 3.10 and 3.12 |
| packages.yml | `flow-lint`, `flow-critical` | yes | yes |
| packages.yml | `flow-unit` | 2 shards | 4 shards |
| packages.yml | `capture-linux` | Python 3.12 | Python 3.10 (ubuntu-22.04), 3.11, 3.12 |
| packages.yml | `capture-macos`, `capture-windows` | no | yes |
| packages.yml | `capture-contract`, `capture-core-record` | yes | yes |
| packages.yml | `privacy` | Python 3.12 | Python 3.10 to 3.12 |
| packages.yml | `privacy-presidio`, `privacy-contract` | yes | yes |
| packages.yml | `tray` | Linux, Python 3.12 | Linux 3.10 to 3.12, macOS and Windows 3.12 |
| packages.yml | `tray-contract`, `tray-desktop-ipc`, `program-graph`, `install-weight` | yes | yes |
| packages.yml | `desktop-release-intent` | yes | no |
| desktop-test.yml | `test-python` | ubuntu-latest, Python 3.12 | Linux, macOS and Windows, Python 3.11 and 3.12 |
| desktop-test.yml | `qualification-contract` | yes | yes |
| desktop-build.yml | `frontend`, `python-distribution` | yes | yes |
| desktop-build.yml | `python-sidecar` | ubuntu-22.04 | Linux, macOS 14 and Windows |
| desktop-build.yml | `native-installers` | no | macOS arm64, macOS Intel, Windows, Linux bundles |
| desktop-dependency-audit.yml | `python`, `npm`, `rust` | when Desktop changes | when Desktop changes, and weekly |
| desktop-dependency-audit.yml | `python-macos-intel` | no | when Desktop changes, and weekly |
| codeql.yml | `Analyze` (Python, JavaScript/TypeScript, Rust) | per changed language | per changed language; all languages on main and weekly |
| secret-scan.yml, dependency-review.yml, platform-manifest.yml | all | yes | yes |
| docs.yml, link-check.yml (internal), production-lifecycle.yml, release-health.yml (self-test) | all | yes | main only |

packages.yml jobs run only for the packages a change touches. The `scope` job
decides from the PR diff, or from the queue group's diff against its base
commit.

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

pip jobs use `actions/setup-python` with `cache: pip`. Every `setup-uv` step
sets `enable-cache: true` and keys the cache on its package's own `uv.lock`.
npm uses `cache: npm`. The Tauri builds use `Swatinem/rust-cache`, and the
Rust audit caches its `cargo-audit` binary. The release workflows keep caching
off on purpose: release builds start from a clean cache.

## Merge queue settings

How many queue runs happen per merged pull request is a repository setting,
not a workflow one. With "Maximum pull requests to build" at 3, the queue
builds up to three overlapping groups at once ([A], [A,B], [A,B,C]), and each
runs the full queue tier. To run the heavy jobs once per batch, build one
group at a time and let it collect several pull requests, for example: build
1, merge at least 1 and at most 10, and wait 5 minutes to fill a group. Those
are branch-protection settings, so an admin changes them. How GitHub fills a
group at those values hasn't been tested here; check the result on the next
few queue runs.

## Dependabot

Each ecosystem and directory opens at most one grouped version-update pull
request a week (`.github/dependabot.yml`). Security updates are not grouped.
