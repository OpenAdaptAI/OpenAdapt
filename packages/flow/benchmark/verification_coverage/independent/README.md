# Verification coverage against an independent specification

What OpenAdapt's record checks actually catch, measured against a
specification and faults written without looking at those checks.

An external review said that a check built from the same contract as the save
inherits the contract's blind spots. This suite tests that claim: write down,
independently, everything that must and must not be true after one correct
referral intake in OpenEMR; derive faults from that; run them through
OpenAdapt's checks; count what each check catches, misses and flags wrongly.

## How it was made

1. `SPEC.md`, the specification, was committed first (ad4bdbfe2), before any
   run and without reading `verify.yaml` files, synthesized checks or
   `fault_gate.py`'s fault classes. Sources: the task definition, the referral
   format and OpenEMR's schema and behavior.
2. `faults.yaml`, 17 cases derived from the spec, was committed next
   (40a474bc1): wrong record (2), unrelated changes by the bot to another
   chart or table (7), wrong values saved faithfully from a misread source
   (3), a missing side record, a duplicate save, and two legitimate concurrent
   updates by another user that must not be flagged.
3. `WITNESS.md` records what OpenEMR's own save path writes, as amendments,
   without editing the spec.

## The checks under test

| name | what it is |
|---|---|
| `shipped` | the OpenEMR referral task's record contract (`scoreboard/claims_openemr.py` `VERIFY_YAML`): the whole patient table, `verify-run`'s default frame |
| `framed` | the same, plus a frame with OpenEMR's audit log for attribution (as `verification_coverage/openemr_run.py` declares it) and provenance with the referral document, so `verify-run` does the round trip (#1446) |
| `synthesized` | what `scaffold-verifier --db --fault-gate` proposes from a demonstration on its own copy |
| pre-save | `provenance.cross_check`: a second extraction of every labeled referral field, compared before the save (scored only for the misread cases) |

The fault gate (#1445) also runs on each check, so its own coverage claim can
be set beside the independent result.

## Where it runs

- **Stand-in** (`run.py`, `standin.py`): an SQLite copy of the pinned OpenEMR
  fixture's real baseline dump. Every table, column, default and installer
  row is OpenEMR's (283 tables, 256,044 rows). The writes are emulated: the
  New Patient save writes the rows `WITNESS.md` lists, and each named user's
  write adds the audit row OpenEMR's `EventAuditLogger` writes. `--slice`
  keeps only the 24 tables the spec names plus bookkeeping.
- **Live** (`live.py`): the same cases on the pinned OpenEMR fixture. The bot's
  New Patient save goes through OpenEMR's form in a browser. The bot's other
  writes and clinician2's go through the REST API with each user's own token,
  so OpenEMR writes its own audit rows. **Not run yet:** it waits for the
  shared fixture slot.

## Results: stand-in, clinical slice (2026-10-24)

Per class, for a fault: caught or missed. For a clean case: ok or false alarm.
`results/standin-slice/` has every row, receipt part and gate record.

| class | cases | shipped | framed | synthesized |
|---|---|---|---|---|
| clean | 1 | ok | ok | **false alarm** |
| concurrent update by another user | 2 | **2 false alarms** | 2/2 ok | **2 false alarms** |
| wrong record | 2 | 2/2 caught | 2/2 caught | (2/2) |
| unrelated change by the bot | 7 | 3/7, **4 missed** | 6/7, **1 missed** | (7/7) |
| misread source, saved faithfully | 3 | 0/3, **3 missed** | 2/3, **1 missed** | (3/3) |
| missing side record | 1 | **missed** | **missed** | (1/1) |
| duplicate save | 1 | caught | caught | (1/1) |

The pre-save cross-check held all 3 misread cases.

The synthesized check's catches are in parentheses because they carry no
information: it refuted the clean save too, so it refutes everything.

What the fault gate said about the same checks:

| check | gate claims | gate result |
|---|---|---|
| `shipped` | delta, frame | 18/18 killed, clean passes (admitted) |
| `framed` | delta, frame | 18/18 killed, clean passes (admitted) |
| `synthesized` | delta, frame | 7/7 killed, **clean fails** (not admitted) |

## What it found

Fail-open (a bad save reported `verified`), ranked by risk:

1. **A frame with audit attribution excuses any write the bot makes for the
   saved patient, in any table.** An extra insurance row on the new chart
   verified under `framed`. #1505
2. **The gate's "frame" coverage is the one table the check reads.** It
   admitted `shipped` at 18/18 while the independent cases show 9 of 15
   faults missed: edits to another chart's history, a note and an allergy on
   another patient, the extra insurance row, the missing history row, and
   three faithful saves of misread inputs. #1506
3. **Provenance checks only the task's parameters.** The referral names a
   provider; the task has no provider parameter, so a wrong provider verified
   under `framed`. A coded field (provider id, state code) can't be
   round-tripped by value. #1507
4. **Required side records are never checked.** A chart whose `history_data`
   row is missing verified under both contracts. OpenEMR's own REST save
   writes no such row. #1508

Fail-closed (no wrong `verified`, but the check can't be used):

5. **`verify-run` crashed on any SQL read with a binary column** (OpenEMR's
   `uuid binary(16)`), so no `SELECT *` check on OpenEMR could run. Fixed in
   #1503 (merged into this branch for these measurements).
6. **`scaffold-verifier --db` can't produce a working check on the real
   schema.** It takes coincidental value matches in 21 reference tables as
   the saved row, the gate then fails on the wrong table, and the default
   frame flags OpenEMR's own log rows on every correct save. On the full
   schema, synthesis and the gate ran for more than 30 minutes. #1509
7. **The shipped contract flags every legitimate concurrent update** (no
   attribution). `framed` handles both concurrent cases correctly.

What held: wrong record and duplicate saves were caught by every check. Edits
to another chart's own row were caught by every check. A stray edit by the bot
was still caught when another user's legitimate edit happened in the same
window. The round trip caught the misread DOB and the swapped names.

## Limits

- The stand-in's audit rows are emulated. `framed` catches 3 of its 6
  unrelated-change catches only through the bot's audit entries naming another
  patient. Whether OpenEMR writes `patient_id` on those entries for API writes
  is what the live run checks.
- One referral, one app, one run per case. The counts are cases, not rates.
- The full-schema stand-in run is slower (about 10 minutes a case, mostly
  `verify-run` polling the synthesized check's 580 effects). Its rows are added
  to `results/standin-full/` when it finishes.

## Run it

```bash
python -m verification_coverage.independent.run \
  --baseline-sql "$OPENEMR_FIXTURE_STATE/baseline.sql" --work WORKDIR --out OUT [--slice]
python -m verification_coverage.independent.summarize OUT/rows.json
# with the fixture lease held:
python -m verification_coverage.independent.live --out OUT
```

No model calls in any of these.
