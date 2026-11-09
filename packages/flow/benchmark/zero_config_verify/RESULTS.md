# Results: generated verifiers vs hand-written verifiers under planted faults

Protocol: [PROTOCOL.md](PROTOCOL.md). It was committed before any trial
(42415a612). Two deviations, each committed before the run it affects:
c4a3be060 (the `hand-written+complete` baseline) and 79df8b96c (the second
run: the fixed generator and a new `Z-net-probed` arm).

This file reports the **second run**, made on 2026-11-09 at commit 0fa5f2b35.
That commit is this branch with the engine change from #2129
(`feat/zero-config-read-discovery`) merged in. The run had 69 trials (36
MockMed, 33 MockLoan), used headless Chromium on localhost only, made no model
calls and used synthetic data only. Raw data is in
[results.json](results.json), and the generated files are in
[generated/](generated/). The first run, made before the fixes, is kept in
[results-run1.json](results-run1.json) and summarized at the end.

## Verdict

**The protocol's rule is met, with a caveat that matters.** On both apps, at
least one zero-config (`Z-*`) arm passes all three kill thresholds. The rule
asks for at least one, so this counts as ready on these fixtures.

The arm that passes on both apps is `Z-net-read`. Its demonstration also
loads the store's record list (`GET /api/db`) before and after the save. No
person writes any verifier text for it. However, neither app makes that read
by itself, so it is an assisted demonstration.

The new arm, `Z-net-probed`, uses only the app's own traffic. It is **not
ready on either app**:

- **MockMed: no runnable verifier.** The app has no JSON read route for the
  collection it writes, so all 4 probes returned 404:
  - `GET /api/encounter` and `GET /api/encounters`;
  - `GET /api/encounter/1` and `GET /api/encounters/1`.

  The generator left the read UNAVAILABLE and marked it as needing a person,
  as designed. It wrote the TODO draft and no check.
- **MockLoan (held out): 3 false accepts in 21 wrong runs (all 3 are
  `collateral`), 0 false rejects in 12 correct runs.** The probe found and
  confirmed `GET /api/disbursements`, which returns the record with all four
  written fields. That read covers the disbursements surface only. The
  `collateral` fault writes a stray fee row on another surface, which this
  read cannot see. The generator said so itself: it labelled the file a
  draft, with the miss "collateral_unaudited: a stray row written to a table
  this check does not read". The protocol uses the file with the
  confirmation flags deleted, which is how those runs were accepted.

On MockLoan, the database arms (`Z-db`, `Z-db-gate`) also pass. They were
blocked before generation in the first run.

Every arm that produced a verifier now returns a definite verdict:
0 INDETERMINATE in 69 trials. In the first run, every generated REST check
returned INDETERMINATE. The ground truth agreed with each fault mode's design
label on all 69 trials, and no verdict carried an error.

## Summary table

- **FA** = false accepts (a wrong write was accepted).
- **FR** = false rejects (a correct write was rejected).
- `INDETERMINATE` counts as a reject. The thresholds compare each arm against
  `hand-written+complete`.

| App | Verifier | Class | FA / wrong runs | FR / correct runs | Kill check |
|---|---|---|---|---|---|
| MockMed | `hand-written` (as committed) | baseline | 0 / 24 | 12 / 12 | (not used) |
| MockMed | `hand-written+complete` | baseline | 0 / 24 | 0 / 12 | (threshold baseline) |
| MockMed | `Z-net` | zero-config | no runnable verifier (TODO draft) | | not ready |
| MockMed | `Z-net-probed` | zero-config | no runnable verifier (read unavailable: no read route) | | not ready |
| MockMed | `Z-net-read` | zero-config (assisted read) | 0 / 24 | 0 / 12 | **ready** |
| MockMed | `Z-db`, `Z-db-gate` | zero-config | not applicable (store in memory) | | not ready |
| MockMed | `D-net-read-complete` | declared | 0 / 24 | 0 / 12 | ready |
| MockMed | `P-record-verify` | read path given | 0 / 24 | 0 / 12 | ready |
| MockMed | `P-scaffold-verifier` | read path given | 0 / 24 | 0 / 12 | ready |
| MockLoan | `hand-written` (as committed) | baseline | 0 / 21 | 12 / 12 | (not used) |
| MockLoan | `hand-written+complete` | baseline | 0 / 21 | 0 / 12 | (threshold baseline) |
| MockLoan | `Z-net` | zero-config | no runnable verifier (TODO draft) | | not ready |
| MockLoan | `Z-net-probed` | zero-config | **3 / 21** | 0 / 12 | not ready (FA 3 > 0) |
| MockLoan | `Z-net-read` | zero-config (assisted read) | 0 / 21 | 0 / 12 | **ready** |
| MockLoan | `Z-db` | zero-config | 0 / 21 | 0 / 12 | **ready** |
| MockLoan | `Z-db-gate` | zero-config | 0 / 21 | 0 / 12 | **ready** |
| MockLoan | `D-net-read-complete` | declared | 0 / 21 | 0 / 12 | ready |
| MockLoan | `P-record-verify` | read path given | 0 / 21 | 0 / 12 | ready |
| MockLoan | `P-scaffold-verifier` | read path given | 0 / 21 | 0 / 12 | ready |

What the generator said about each file it wrote (recorded in
`results.json`):

- **Labelled working:** `D-net-read-complete` on both apps, and `Z-db` on
  MockLoan.
- **Labelled draft:**
  - `Z-net-read`, `Z-net-probed` and `P-scaffold-verifier`. Each missed the
    write-outside-the-read fault, because none of them reads the whole store
    by itself.
  - `Z-db-gate`. Two of its fault-gate classes could not be tested on the
    copy.

  A draft does not mean the file fails the thresholds. `Z-net-read` reads
  `/api/db`, which on these apps happens to be the whole store, so it catches
  that fault in practice.

## Per fault class

### MockMed

| fault | truth | `hand-written` | `hand-written+complete` | `Z-net-read` | `D-net-read-complete` | `P-record-verify` | `P-scaffold-verifier` |
|---|---|---|---|---|---|---|---|
| ok | correct | 6 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 |
| idempotent | correct | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| timeout | correct | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| partial | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| optimistic | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| session | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| duplicate | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| double | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| stale | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| no_persist | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| wrong_record | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| indeterminate | | 36 | 0 | 0 | 0 | 0 | 0 |

### MockLoan (held out)

| fault | truth | `hand-written` | `hand-written+complete` | `Z-net-probed` | `Z-net-read` | `Z-db` | `Z-db-gate` | `D-net-read-complete` | `P-record-verify` | `P-scaffold-verifier` |
|---|---|---|---|---|---|---|---|---|---|---|
| ok | correct | 6 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 |
| idempotent | correct | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| timeout | correct | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| partial | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| optimistic | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| session | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| duplicate | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| double | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| stale | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| collateral | wrong | 0 FA / 3 | 0 FA / 3 | **3 FA / 3** | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| indeterminate | | 33 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## What the fixes did, gap by gap

1. **`records_complete` (fixed).** Every `Z-net-read` read now carries
   `records_complete: true` on the `observed` basis. One unpaged response
   held every record that was read before the save, plus the saved record.
   The `D-net-read-complete` reads carry it on the `declared` basis. Both
   arms went from 12/12 false rejects to 0/12 on MockMed. A read that says it
   is paged still gets no `records_complete`, whether it was observed or
   declared (tests in #2129).
2. **Write detection (fixed).** On MockLoan, "Authorize Disbursement" is now
   detected as a write from its `POST /api/disbursement`. Every MockLoan arm
   now generates; in the first run, all were refused.
3. **Read-path discovery (works as specified, and not enough on its own):**
   - **MockLoan:** the probe confirmed the disbursements list. It is the right
     record read, but not the whole store.
   - **MockMed:** no read route exists, so the read stays unavailable, and
     nothing was made up.

   Every probe was a `GET` to the local fault server, sent right after the
   demonstration's save. The probe list is in `results.json` under
   `apps.<app>.arms.Z-net-probed.read_discovery`.

## Gaps that remain

- **A probed read sees one collection, not the store.** On MockLoan it
  misses a write to a second surface. The generator already labels this
  case a draft. An operator who accepts the file unread still gets a check
  that passes a stray write elsewhere. Possible fixes:
  - refuse to emit a runnable check while `collateral_unaudited` is
    unresolved;
  - have the probe look for a sibling read that covers more surfaces.

  This run tested neither.
- **Apps with no JSON read route.** MockMed shows the saved record only as
  server-rendered HTML (`/chart/<patient>`). Discovery reads JSON only, so
  the read stays unavailable there. Reading an HTML record page is not
  implemented.
- **The passing network arm is assisted.** `Z-net-read` depends on the
  demonstration loading `/api/db`. A real app that shows the saved record
  from a JSON read produces that traffic by itself; these two apps do not.
- **`observed` completeness is an inference.** The rule is: no paging signs,
  everything read before the save still present, and a record count that is
  not a common page size. It cannot rule out a server that silently caps an
  unpaged list. The proof says `observed`, not `declared`, and every effect
  still needs a person's confirmation.
- **`--probe-reads` needs a live local copy of the app** in the state right
  after the save. Here that was the demonstration's own fault server. On a
  real deployment, it would be a sandbox given with `--probe-base`.
- The limits of the first run still apply:
  - two synthetic apps, with 3 cases per fault (6 for `ok`);
  - one demonstration per app;
  - confirmation flags deleted mechanically;
  - the `P-scaffold-verifier` snapshots are emulated by the harness;
  - no wrong-value-on-the-right-record fault beyond `partial`, no late
    write, and no second store;
  - the Chrome extension's capture and `benchmark/contract_autogen` were not
    measured.

## First run (before the fixes)

That run is in [results-run1.json](results-run1.json), made at c4a3be060 with
the generator as it was on `main`. No zero-config arm passed on either app:

- **MockMed:**
  - `Z-net` wrote only a TODO draft;
  - `Z-net-read` and `D-net-read-complete` had 0/24 false accepts but 12/12
    false rejects. Every run was INDETERMINATE, because no generated REST
    read carried `records_complete`.
- **MockLoan:** every `Z-*` and `D-*` arm was refused before generation,
  because the "Authorize Disbursement" label is not write-shaped.

With the read path given (`P-*`), both apps matched the corrected
hand-written verifiers exactly: 0 false accepts and 0 false rejects.

## A finding outside the generated verifiers

The committed `environments/openenv/{mockmed,mockloan}/verify.yaml` files
still lack `records_complete: true`. They return INDETERMINATE on every run
(69 of 69 here). #1784 adds the line.

## Reproduce

```bash
cd packages/flow
uv run --frozen --extra dev --extra browser python -m benchmark.zero_config_verify.run
uv run --frozen python -m benchmark.zero_config_verify.report
```

The second run took about 40 minutes: 16 minutes of MockMed trials, 23
minutes of MockLoan trials, plus generation.
