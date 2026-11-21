# Post-merge batching: accept a later commit's green run (design only)

Status: proposal for the coordinator. Nothing here is implemented. The release
rule described under "Today" stays in force until a separate pull request
changes it.

## Problem

On 2026-11-20, 39% of OpenAdapt's runner minutes went to Packages push runs on
`main`. Every merge starts its own full push run, and none is ever cancelled.
When the merge queue lands five pull requests in ten minutes, five full runs
test five trees that are almost the same.

Push runs are never cancelled because of the release rule. `desktop-release.yml`,
`scripts/ci/newest_green.py` and `scripts/ci/release_plan.py` (which calls
`newest_green.py`) all release a package only from **the push run of the
newest first-parent commit that changed the package**. If that run is
cancelled, the package cannot release until someone re-runs it. Desktop 0.23.0
and 0.24.1 were stranded this way (`tests/test_packages_concurrency.py`).

## Today

For a package P with paths `paths(P)` and the tip `T` that a release builds:

1. `S = git log -1 --first-parent T -- paths(P)`, the subject.
2. Require a Packages run with `head_sha == S`, `event == push`,
   `head_branch == main`, `conclusion == success`, and this repository.
3. Require P's own jobs in that run: each prefix has at least one passed job
   and none failed (`job_problems`).
4. Build `T`. Its `paths(P)` tree equals `S`'s, because nothing after `S`
   changed P.

## Proposal

Two changes, shipped together, plus a third that ends the waste.

### A. Accept a green run on a later commit

Replace step 2 with: require a run like the one in step 2 whose `head_sha` is
any commit `C` such that:

- `C` is on `T`'s first-parent history and `S` is an ancestor of `C`
  (`git merge-base --is-ancestor S C` and `C` is in
  `git rev-list --first-parent S^..T`);
- `git diff --quiet S C -- paths(P)` holds. This already follows from the
  definition of `S`; check it explicitly anyway, so a wrong `S` fails closed;
- the run has P's jobs, each passed (step 3, unchanged).

If several runs qualify, take the newest `C`, which is the one closest to `T`.

In `newest_green.py`:

```python
def candidates(tip: str, subject: str) -> list[str]:
    """First-parent commits from subject to tip, newest first."""
    out = git("rev-list", "--first-parent", f"{subject}^..{tip}")
    return [c for c in out.split() if is_ancestor(subject, c)]

def select_run(runs_by_sha, shas, repo, paths, subject):
    for sha in shas:                      # newest first
        if not tree_equal(subject, sha, paths):
            raise SystemExit("refusing: package tree changed after its subject")
        run = newest_success(runs_by_sha.get(sha, []), repo)   # today's filter
        if run is not None:
            return sha, run
    return None
```

`main()` fetches runs with `branch=main&event=push&status=success` once, not
per SHA, and passes the green run's SHA on as the `sha` output. The
`run_id` output keeps its meaning.

`desktop-release.yml` gets the same change in its "Require that commit's
Packages run…" step. Its `jq` filter on `head_sha == env.SUBJECT` becomes a
membership test over the candidate list computed with `git rev-list`, followed
by the same `git diff --quiet` check. `require_desktop_jobs` is unchanged,
including the py3.11 jobs required by name.

### B. Make a later run test P when it should

A later run is only useful if it ran P's jobs. Today the `scope` job in
`packages.yml` diffs a push against `github.event.before`, the previous tip.
A run for `C` therefore skips P whenever P changed in an earlier commit. With
A alone, a later run is never a candidate for a package it did not change.

So on a push to `main`, `scope` diffs against **the newest first-parent commit
whose Packages push run completed with `conclusion == success`**. If none can
be found within 50 first-parent commits, or the API call fails, it runs
`all`. On a busy day the later run then includes every package changed since
the last green run, which is the union a batch must test.

```bash
# packages.yml, scope job, push branch of the case statement (sketch)
base="$(python3 scripts/ci/last_green_push.py --tip HEAD --max 50)" || all
changed="$(git diff --name-only "$base" HEAD)"
```

`last_green_push.py` lists the successful push runs on `main` for this workflow
(one paginated API call). It returns the first first-parent ancestor of `HEAD`
in that set. The job needs `actions: read`.

### C. Cancel superseded push runs

With A and B in place, change `packages.yml`'s concurrency so a newer push to
`main` cancels an older push run that is still queued or running:

```yaml
concurrency:
  group: packages-${{ github.event.pull_request.number || (github.event_name == 'push' && github.ref) || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' || github.event_name == 'push' }}
```

Merge groups and dispatches keep groups of their own. A new push cancels the
group's running or pending run, so a burst of N merges completes one full push
run (the last), plus whatever the cancelled runs used before they stopped, not
N full runs. The nightly run (`nightly.yml`, `workflow_call`) has its own group and
is unaffected. `tests/test_packages_concurrency.py` must change in the same
pull request. Today it asserts that a push is never cancelled.

## Why this is safe

- **The tested package tree is the same.** The release builds `T`, and
  `paths(P)` at `T` equals `paths(P)` at `S` equals `paths(P)` at `C`. A and
  the release both check this with `git diff --quiet`, not just by
  definition.
- **The evidence is better, not weaker.** A run on `C` tests P against the
  siblings and dependencies at `C`, which is closer to the `T` that is
  actually built than `S` was. Today a release can ship P built at `T`
  against a Flow or Types that its evidence run never saw.
- **P's own jobs must still have run and passed.** Step 3 is unchanged. A
  green run that skipped P (scope false) has no passed job with P's prefixes,
  so it is refused. B is what makes such runs include P. Without B the rule
  is safe but useless.
- **No run on another branch or fork counts.** The `event`, `head_branch` and
  repository filters stay. Ancestry on first-parent `main` rules out a commit
  that `main` does not contain.
- **Cancellation cannot strand a release.** A cancelled run on `S` is no
  longer needed if any later run on `S..T` passes with P's jobs. B guarantees
  that the next push run includes P, and the three-hourly scheduled release
  run picks it up. A burst that ends leaves its last run uncancelled.
- **It fails closed.** No qualifying run means no release, as today. A failed
  lookup in B means `all`, which runs more jobs, not fewer. A tree mismatch
  refuses the release.

## What it saves

In the 2026-11-20 sample (evidence in openadapt-internal#252), push runs
took 5,359 of 13,570 runner minutes on OpenAdapt (39%). Packages push runs
took 4,007 of those, across 33 commits on `main`. Most push runs land in
bursts behind the merge queue. With C, each burst costs about one full run, so
roughly half to two thirds of the push-run minutes go away on a busy day. The
exact figure depends on burst sizes, which the current data does not
separate. Measure it with the job data from `fetch.sh` before deciding.

## Order of changes

1. A and B together in one pull request. This changes the release rule, so it
   needs the coordinator's and the founder's decision. B alone makes push
   runs test more packages, which costs minutes, so do not ship it alone.
2. C in a second pull request, once A and B have released at least one
   package from a later commit's run.

## Tests to add

- `tests/test_newest_green.py`: a later green run with P's jobs is accepted. A
  later green run without P's jobs is refused. A run on a commit off the
  first-parent path is refused. A tree change after the subject is refused.
- `tests/test_desktop_release_subject.py`: the same cases for the workflow's
  step text.
- `tests/test_packages_concurrency.py`: pushes to `main` share one group and
  cancel. A merge group, a dispatch and the nightly run never do.
- A `last_green_push.py` unit test with a fake run list, including the
  API-failure path that falls back to `all`.
