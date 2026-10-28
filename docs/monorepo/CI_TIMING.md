# CI cost per pull request

`scripts/ci/measure_pr_ci.py` measures each merged pull request's CI from the
GitHub API:

- **Wall time** runs from the first run's start on the PR's final commit to
  its last job's finish.
- **Job-minutes** sum each job's own run time, which is the runner time the
  PR used.

Raw rows are in `ci-timing/`.

## Before: five required checks, `packages.yml` and `main.yml` on pull requests

The last 25 merged pull requests, measured 2026-10-28
(`ci-timing/before-2026-10-28.json`):

| | median | p90 | max |
|---|--:|--:|--:|
| PR wall time | 33.2 min | 70.5 min | 135.6 min |
| PR job-minutes | 63.0 | — | 97.7 |

The merge queue (ruleset 25486718) has been off since 2026-10-25. One of the
25 went through it, at 192 min wall and 331 job-minutes.

The PR lane's long poles were Flow's two unit shards (16 to 22 min each) and
`flow-critical` (14 min, three serial pytest steps). Every other package job
took under 5 minutes.

## Flow test profile (CI hardware, 2026-10-28)

Per-test times came from a JUnit run of the whole Flow suite on
`ubuntu-latest` with 4 xdist workers (branch `claude/profile-test-durations`).

- **Unit suite:** 8,092 cases in 3,993 test-seconds. Cases over 5 s are 1.2%
  of the tests and more than half the time.
- **`nightly_only.txt`:** lists 131 tests (2,127 s) that left the PR lane.
  The safety realism tests stay whatever they take: identity, record check,
  wire check, rollback and approval.
- **Fast lane:** 1,866 test-seconds.
- **`tests/e2e`:** 165 cases in 797 s. Before this change, CI ran only
  `flow-critical`'s eight e2e files, serially.

## After

Measured on this change's own pull-request runs; see the PR description.
