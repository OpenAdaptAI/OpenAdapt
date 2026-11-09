# Results: generated verifiers vs hand-written verifiers under planted faults

Protocol: [PROTOCOL.md](PROTOCOL.md), committed before any trial (42415a612),
with one deviation also committed before any trial (c4a3be060). Raw data:
[results.json](results.json). Generated files, with the local port and temp
paths replaced by placeholders: [generated/](generated/). One measured run,
2026-11-09, 69 trials (36 MockMed, 33 MockLoan), headless Chromium, localhost
only, no model calls, synthetic data only.

## Verdict

**Not ready.** No zero-config arm passes the kill thresholds on either app.

- MockMed: the one zero-config arm that produces a runnable verifier
  (`Z-net-read`) rejects all 12 correct runs (`INDETERMINATE`), so it fails
  the false-reject threshold (12 > 2 x 0). It has 0 false accepts in 24 wrong
  runs.
- MockLoan (held out): every zero-config arm is refused before generation, so
  there is no verifier to measure.
- With only the read path given by a person (`P-*` arms), both generators
  match the corrected hand-written verifier exactly on both apps: 0 false
  accepts and 0 false rejects.

The ground truth agreed with every fault mode's design label on all 69
trials. No verdict carried an error.

## Summary table

FA = false accepts (wrong write accepted), FR = false rejects (correct write
rejected). `INDETERMINATE` counts as a reject.

| App | Verifier | Class | FA / wrong runs | FR / correct runs | Kill check |
|---|---|---|---|---|---|
| MockMed | `hand-written` (as committed) | baseline | 0 / 24 | 12 / 12 | (baseline, not used) |
| MockMed | `hand-written+complete` | baseline | 0 / 24 | 0 / 12 | (threshold baseline) |
| MockMed | `Z-net` | zero-config | no runnable verifier (TODO draft) | | not ready |
| MockMed | `Z-net-read` | zero-config | 0 / 24 | 12 / 12 | not ready |
| MockMed | `Z-db`, `Z-db-gate` | zero-config | not applicable (store in memory) | | not ready |
| MockMed | `D-net-read-complete` | declared | 0 / 24 | 12 / 12 | not ready |
| MockMed | `P-record-verify` | read path given | 0 / 24 | 0 / 12 | ready |
| MockMed | `P-scaffold-verifier` | read path given | 0 / 24 | 0 / 12 | ready |
| MockLoan | `hand-written` (as committed) | baseline | 0 / 21 | 12 / 12 | (baseline, not used) |
| MockLoan | `hand-written+complete` | baseline | 0 / 21 | 0 / 12 | (threshold baseline) |
| MockLoan | `Z-net`, `Z-net-read`, `Z-db`, `Z-db-gate` | zero-config | no runnable verifier (refused) | | not ready |
| MockLoan | `D-net-read-complete` | declared | no runnable verifier (refused) | | not ready |
| MockLoan | `P-record-verify` | read path given | 0 / 21 | 0 / 12 | ready |
| MockLoan | `P-scaffold-verifier` | read path given | 0 / 21 | 0 / 12 | ready |

## Per fault class

### MockMed

| fault | truth | `hand-written` | `hand-written+complete` | `Z-net-read` | `D-net-read-complete` | `P-record-verify` | `P-scaffold-verifier` |
|---|---|---|---|---|---|---|---|
| ok | correct | 6 FR / 6 | 0 FR / 6 | 6 FR / 6 | 6 FR / 6 | 0 FR / 6 | 0 FR / 6 |
| idempotent | correct | 3 FR / 3 | 0 FR / 3 | 3 FR / 3 | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| timeout | correct | 3 FR / 3 | 0 FR / 3 | 3 FR / 3 | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| partial | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| optimistic | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| session | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| duplicate | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| double | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| stale | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| no_persist | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| wrong_record | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| indeterminate | | 36 | 0 | 36 | 36 | 0 | 0 |

The 0 false accepts of `hand-written`, `Z-net-read` and `D-net-read-complete`
mean nothing on their own: those verifiers return `INDETERMINATE` on every
run, right or wrong.

### MockLoan (held out)

| fault | truth | `hand-written` | `hand-written+complete` | `P-record-verify` | `P-scaffold-verifier` |
|---|---|---|---|---|---|
| ok | correct | 6 FR / 6 | 0 FR / 6 | 0 FR / 6 | 0 FR / 6 |
| idempotent | correct | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| timeout | correct | 3 FR / 3 | 0 FR / 3 | 0 FR / 3 | 0 FR / 3 |
| partial | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| optimistic | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| session | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| duplicate | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| double | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| stale | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| collateral | wrong | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 | 0 FA / 3 |
| indeterminate | | 33 | 0 | 0 | 0 |

## Why each zero-config arm failed

1. **The apps never read the saved record back (`Z-net`, MockMed).** The
   MockMed page sends `POST /api/encounter` and shows the result from page
   memory; it makes no JSON read after the save. `scaffold-verifier` finds no
   read and falls back to the TODO `effect_contract.yaml`, which
   `verify-run`'s parser rejects (it is a draft for a person, not a
   `verify.yaml`). The synthesis test fixture
   (`tests/_record_check_fixtures.py::mockmed_recording`) assumes the app reads
   `/api/db` after saving; the real page does not.
2. **A generated REST read never declares itself complete (`Z-net-read`,
   `D-net-read-complete`, MockMed).** When the demonstration does load
   `/api/db`, `scaffold-verifier` finds it and builds a check with the right
   bindings (`patient_id`, `type`, `note` bound to run parameters) plus a
   content guard. But the emitted read has no `records_complete`, and since
   #2041 such a read is unreachable, so every run is `INDETERMINATE`. Passing
   `--complete-read` records the declaration in the proof but still does not
   emit `records_complete: true` on the read, so the declared arm behaves
   the same. The generator does flag it ("REST read completeness is not
   declared") and labels the file a draft.
3. **The Save control is not recognised as a write (all MockLoan arms).**
   `scaffold-verifier` first looks for a write-shaped step. MockLoan's button
   reads "Authorize Disbursement", and `risk.is_write_shaped` matches neither
   word, so it refuses ("no consequential (write-shaped) step") before it
   looks at the network capture or the `--db` file. The `POST
   /api/disbursement` in `network.jsonl` is not used as evidence of a write.
   The `P-scaffold-verifier` arm gets past this only because `record --verify`
   puts `sor_before`/`sor_after` on each event, and one new record in that
   delta counts as a write.
4. **No database file on MockMed (`Z-db`, `Z-db-gate`).** MockMed keeps its
   store in memory, so `--db` has nothing to read. On MockLoan the SQLite
   ledger exists, but gap 3 stops generation first.

## What the read-path arms show

Given one thing a person writes, the `verifier:` read path (with
`records_complete: true`), both generators produce verifiers that match the
corrected hand-written ones on all 69 trials:

- `P-record-verify` (`record --verify`) proposes one `record_written` effect
  that binds every task field to a run parameter and also pins
  `source: replay` (and `surface: disbursements` on MockLoan). It does not
  propose a guard for existing records, yet the `stale` and `collateral` runs
  were still refuted: in a manual check of one `collateral` run, the effect
  itself was `CONFIRMED` and the run was `REFUTED` by verify-run's own frame
  over the complete read. Its correctness on those classes depends on that
  runtime frame, not on the proposed effect.
- `P-scaffold-verifier` labels its output a draft (it misses the
  `collateral_unaudited` recorded-state fault because it reads only one
  surface), but on these apps `/api/db` is the whole store, so the stray fee
  row is visible and refuted.

These arms are not zero-config. They show the gap is narrow: the read path
and its completeness, plus write detection for unfamiliar button labels.

## A finding outside the generated verifiers

The committed `environments/openenv/mockmed/verify.yaml` and
`environments/openenv/mockloan/verify.yaml` lack `records_complete: true`. On
this commit they return `INDETERMINATE` for every run (69 of 69 here), so the
RL environments built on them pay 0 reward on every episode. The fix is one
line per file; it is not part of this change.

## Limits

- Two synthetic apps, one store each, one record per save, 3 cases per fault
  (6 for `ok`). The counts are small, and zero false accepts in 3 runs of a
  class is not a rate.
- One demonstration per app. Generation was not repeated with other
  demonstrations.
- Generated files are used with the `needs_operator_confirmation` lines
  deleted mechanically. That is what an operator who accepts unread would
  get; the generators themselves never approve anything.
- `Z-net-read` adds a page read of `/api/db` to the demonstration. A real app
  that shows the saved record from a JSON read would produce that traffic on
  its own; these two apps do not.
- The `P-scaffold-verifier` demonstration emulates `record --verify`'s
  per-event snapshots in the harness (same reader, same keys) rather than
  running the interactive recorder.
- The fault suite is the fault servers' own modes. It has no wrong value on
  the right record beyond `partial`, no late write, and no second store.
- Not measured: the Chrome extension's network-save capture (#2014). It
  records writes without read responses, and nothing in `scaffold-verifier`
  or `record_check_synthesis` reads `extension_evidence.jsonl`, so it cannot
  feed this generator today. `benchmark/contract_autogen` targets FHIR
  `ensure` contracts on Medplum, not these apps. No "glass-to-wire" or "pack
  factory" code exists on `main` to run.

## Reproduce

```bash
cd packages/flow
uv run --frozen --extra dev --extra browser python -m benchmark.zero_config_verify.run
uv run --frozen python -m benchmark.zero_config_verify.report
```

The run takes about 35 minutes, mostly verify-run's 5-second wait before a
refutation or an unreachable read is final. `results.json` records
`git_head` c4a3be060, the protocol commit; the harness files were committed
right after the run. The only harness edits after the run were formatting
and scrubbing temp paths out of one stored parse error.
