# Results: generated verifiers vs hand-written verifiers under planted faults

Protocol: [PROTOCOL.md](PROTOCOL.md). It was committed before any trial
(42415a612). Each deviation was committed before the run it affects:

- c4a3be060: the `hand-written+complete` baseline;
- 79df8b96c: the second run, with the fixed generator and the new
  `Z-net-probed` arm;
- bedb622b9: the third run, which refuses a probed read that can't see the
  whole save;
- a4e8ffdea, f484a9933 and ae4ecc75e: the fourth run, in which
  `--probe-reads` looks for a read that covers the whole save before it
  refuses (#2131), with two review fixes.

This file reports the **fourth run**, made on 2026-11-10 (UTC) at commit
ae4ecc75e (#2147). The run had 69 trials (36 MockMed, 33 MockLoan), used
headless Chromium on localhost only, made no model calls and used synthetic
data only. Raw data is in [results.json](results.json), and the generated
files are in [generated/](generated/). Earlier runs are kept and summarized
at the end:

- [results-run1.json](results-run1.json): the generator as it was on `main`;
- [results-run2.json](results-run2.json): the fixed generator, before the
  refusal;
- [results-run3.json](results-run3.json): the refusal, before the search for
  a covering read.

## Verdict

**Unchanged from the third run. The protocol's rule is met on these fixtures
only through an assisted arm whose file the generator labels a draft.**

- `Z-net-read` passes all three kill thresholds on both apps. Its
  demonstration also loads the store's record list (`GET /api/db`) before
  and after the save. Neither app makes that read by itself, so this is an
  assisted demonstration.
- The generator labels the resulting file a draft. It runs only because the
  protocol deletes the `needs_operator_confirmation` lines.
- With the app's own traffic alone (`Z-net`, `Z-net-probed`), no zero-config
  arm is ready on either app.
- The only unassisted pass is `Z-db` and `Z-db-gate` on MockLoan, whose store
  is a file the generator can read.

`Z-net-probed` uses only the app's own traffic. It still has **no runnable
verifier on either app, and so no false accepts**:

- **MockMed: read unavailable.** The app has no JSON read route for the
  collection it writes, so all 4 probes returned 404:
  - `GET /api/encounter` and `GET /api/encounters`;
  - `GET /api/encounter/1` and `GET /api/encounters/1`.

  The generator wrote the TODO draft and no check. The search for a covering
  read runs only after a read is confirmed, so it did not run here.
- **MockLoan (held out): refused, with the covering search recorded.** The
  probe confirmed `GET /api/disbursements`, which returns the record with all
  four written fields. The fault test showed that a write outside that
  collection would pass (`collateral_unaudited`). The generator then looked
  for a read that covers the whole save:
  - the app read no other collection under `/api`, so there was no sibling
    to probe;
  - `GET /api` and `GET /api/` both returned 404.

  Nothing covered the save, so the refusal stands. `read_discovery.json` and
  the proof record all 4 probes, the status `refused`, and this reason:
  "No read covering the whole save was found: GET /api: no read here (status
  404); GET /api/: no read here (status 404); the app read no other
  collection under /api."

  The store-wide read (`/api/db`) exists, but the app never requests it and
  it is not under a name the generator could derive. The generator does not
  guess route names.

On MockLoan, the database arms (`Z-db`, `Z-db-gate`) also pass.

Every arm that produced a verifier returns a definite verdict: 0
INDETERMINATE in 69 trials. The ground truth agreed with each fault mode's
design label on all 69 trials, and no verdict carried an error.

One baseline changed: the committed hand-written files now carry
`records_complete: true` (added by #1784 after the third run's commit). So
`hand-written` (as committed) now matches `hand-written+complete`: 0 false
rejects instead of 12 of 12. The thresholds still compare against
`hand-written+complete`, as the protocol says.

## Summary table

- **FA** = false accepts (a wrong write was accepted).
- **FR** = false rejects (a correct write was rejected).
- `INDETERMINATE` counts as a reject. The thresholds compare each arm against
  `hand-written+complete`.

| App | Verifier | Class | FA / wrong runs | FR / correct runs | Kill check |
|---|---|---|---|---|---|
| MockMed | `hand-written` (as committed) | baseline | 0 / 24 | 0 / 12 | (not used) |
| MockMed | `hand-written+complete` | baseline | 0 / 24 | 0 / 12 | (threshold baseline) |
| MockMed | `Z-net` | zero-config | no runnable verifier (TODO draft) | | not ready |
| MockMed | `Z-net-probed` | zero-config | no runnable verifier (read unavailable: no read route) | | not ready |
| MockMed | `Z-net-read` | zero-config (assisted read) | 0 / 24 | 0 / 12 | **ready** |
| MockMed | `Z-db`, `Z-db-gate` | zero-config | not applicable (store in memory) | | not ready |
| MockMed | `D-net-read-complete` | declared | 0 / 24 | 0 / 12 | ready |
| MockMed | `P-record-verify` | read path given | 0 / 24 | 0 / 12 | ready |
| MockMed | `P-scaffold-verifier` | read path given | 0 / 24 | 0 / 12 | ready |
| MockLoan | `hand-written` (as committed) | baseline | 0 / 21 | 0 / 12 | (not used) |
| MockLoan | `hand-written+complete` | baseline | 0 / 21 | 0 / 12 | (threshold baseline) |
| MockLoan | `Z-net` | zero-config | no runnable verifier (TODO draft) | | not ready |
| MockLoan | `Z-net-probed` | zero-config | no runnable verifier (refused: no read covers the whole save; `GET /api` and `GET /api/` return 404, no sibling read) | | not ready |
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
  - `Z-net-read` and `P-scaffold-verifier`. Each missed the
    write-outside-the-read fault, because neither reads the whole store by
    itself. `Z-net-probed` refuses in that case instead of writing a draft
    check.
  - `Z-db-gate`. Two of its fault-gate classes could not be tested on the
    copy.

  A draft does not mean the file fails the thresholds. `Z-net-read` reads
  `/api/db`, which on these apps happens to be the whole store, so it catches
  that fault in practice.

## Per fault class

Printed by `report.py` from `results.json`.

### MockMed

| fault | truth | `hand-written` | `hand-written+complete` | `D-net-read-complete` | `P-record-verify` | `P-scaffold-verifier` | `Z-net-read` |
|---|---|---|---|---|---|---|---|
| ok | correct | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 |
| idempotent | correct | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| timeout | correct | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| partial | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| optimistic | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| session | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| duplicate | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| double | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| stale | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| no_persist | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| wrong_record | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| **total** | | **0 FA / 24, 0 FR / 12** | **0 FA / 24, 0 FR / 12** | **0 FA / 24, 0 FR / 12** | **0 FA / 24, 0 FR / 12** | **0 FA / 24, 0 FR / 12** | **0 FA / 24, 0 FR / 12** |
| indeterminate | | 0 | 0 | 0 | 0 | 0 | 0 |

### MockLoan (held out)

| fault | truth | `hand-written` | `hand-written+complete` | `D-net-read-complete` | `P-record-verify` | `P-scaffold-verifier` | `Z-db` | `Z-db-gate` | `Z-net-read` |
|---|---|---|---|---|---|---|---|---|---|
| ok | correct | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 |
| idempotent | correct | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| timeout | correct | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| partial | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| optimistic | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| session | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| duplicate | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| double | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| stale | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| collateral | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| **total** | | **0 FA / 21, 0 FR / 12** | **0 FA / 21, 0 FR / 12** | **0 FA / 21, 0 FR / 12** | **0 FA / 21, 0 FR / 12** | **0 FA / 21, 0 FR / 12** | **0 FA / 21, 0 FR / 12** | **0 FA / 21, 0 FR / 12** | **0 FA / 21, 0 FR / 12** |
| indeterminate | | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

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
3. **Read-path discovery (works as specified; fails closed where it can't
   see everything):**
   - **MockLoan:** the probe confirmed the disbursements list. It is the right
     record read, but not the whole store, so the generator refused to make
     it runnable.
   - **MockMed:** no read route exists, so the read stays unavailable, and
     nothing was made up.

   Every probe was a `GET` to the local fault server, sent right after the
   demonstration's save. The probe list is in `results.json` under
   `apps.<app>.arms.Z-net-probed.read_discovery`.
4. **A read that covers the whole save (implemented; nothing to find on
   these apps).** Before it refuses, `--probe-reads` now probes the sibling
   collections the app read and the prefix root. It uses the root only when
   all of these hold:
   - the root holds the written record and every record of the collection
     and each sibling;
   - it is one unpaged response, and every captured write went under the
     prefix;
   - every top-level value is a list of objects or a scalar.

   The check then reads every list, empty ones included, plus any list that
   appears later. On MockLoan the root returns 404 and the app reads no
   sibling, so nothing was promoted. The promotion path is covered by tests
   on separate local apps (#2147), not by this benchmark.

## Gaps that remain

- **A probed read sees one collection, not the store.** On a multi-surface
  store, the generator refuses instead of emitting a check that could hide a
  write elsewhere. It now looks for a covering read first, but finds one only
  when the app reads a sibling collection or serves a JSON prefix root.
  MockLoan does neither, so `Z-net-probed` produces nothing runnable there
  unless one of these is given:
  - `--complete-read`;
  - `--db`;
  - a fuller read.
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

## Third run (the refusal, before the covering search)

That run is in [results-run3.json](results-run3.json), made at cfa11472b.
Every generated arm matched this run. `Z-net-probed` was refused on MockLoan
for the same reason, without the covering search. The one difference is the
`hand-written` (as committed) baseline, which returned INDETERMINATE on all
69 trials (12 of 12 false rejects on each app), because the committed files
then lacked `records_complete: true`.

## Second run (fixed generator, before the refusal)

That run is in [results-run2.json](results-run2.json), made at 0fa5f2b35.
Every generated arm matched this run except `Z-net-probed` on MockLoan. There, the
generator emitted a draft check on the probed `/api/disbursements` read. With
the confirmation flags deleted, it gave 3 false accepts in 21 wrong runs (all
`collateral`) and 0 false rejects in 12 correct runs, which is not ready.

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

In the first three runs, the committed
`environments/openenv/{mockmed,mockloan}/verify.yaml` files lacked
`records_complete: true` and returned INDETERMINATE on every run. #1784 added
the line, and in this run they return the same verdicts as
`hand-written+complete`.

## Reproduce

```bash
cd packages/flow
uv run --frozen --extra dev --extra browser python -m benchmark.zero_config_verify.run
uv run --frozen python -m benchmark.zero_config_verify.report
```

Each run takes about 40 minutes: about 16 minutes of MockMed trials,
23 minutes of MockLoan trials, plus generation.
