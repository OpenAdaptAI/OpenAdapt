# The OpenEMR referral record check against independent faults

This folder measures what OpenAdapt's record check catches on one task: a
bot registers a referred person in OpenEMR 8 through the New Patient form.
The faults are written from a specification, not from the check, so the
measurement can find what the check misses.

It brings together two draft pull requests: #1452 (the specification, the
first fault suite and its results) and #1580 (the blind held-out suites and
the `phase0 --suite` harness). Both branches stay on GitHub, so every commit
named here can be opened.

## Pre-registration

The order below is what makes the results count. Each step is a commit that
was pushed before the next one.

| step | file | commit | sha256 of the file in this folder |
|---|---|---|---|
| specification, before any run | `SPEC.md` | ad4bdbfe2 (#1452) | see below |
| 17 cases from the spec, before any run | `faults.yaml` | 40a474bc1 (#1452) | see below |
| two review fixes, recorded inside `SPEC.md` | `SPEC.md`, `faults.yaml` | f819d1f77 (#1452) | `d044c09b…` and `05ee57ea…` |
| held-out 1, frozen before any run | `heldout.yaml` | 666f0c0cc (#1580) | `b1bd44ae…` |
| check for held-out 2 recorded | | c3987d90c (#1580) | |
| held-out 2, opened after that | `heldout2.yaml` | b90903a79 (#1580) | `63b44803…` |
| check for held-out 3 recorded | | 6d0b1525d (#1580) | |
| held-out 3 | `heldout3.yaml` | private, 7f9f7be4 | `a00983b9…` |
| check for held-out 4 recorded | | 1821c79c8 (#1580) | |
| held-out 4 | `heldout4.yaml` | private, f6eed8b9 | `b2f3ccb6…` |
| check for held-out 5 recorded | | 784fc15e4 (#1580); private 06f16df5 | |
| held-out 5 | `heldout5.yaml` | private, 0dd04300 | `01104c9e…` |

`SPEC.md` and `faults.yaml` here are byte-identical to the tips of both
branches. The originals at 40a474bc1 differ only by the two fixes that
`SPEC.md` lists under "Changes before freezing" (P1's wording, and the
wrong-provider case's `told` key, `provider` to `providerID`):

```
d044c09b5953e2473e1067b9e444821a5a08d43274bd04b99efbae31a64a07d7  SPEC.md      (40a474bc1: d84411c5…)
05ee57ea8fada862d3f2e1d03bcc4709f0005f43aa3eb490a2de10fbdfcbf3ee  faults.yaml  (40a474bc1: f2d4f053…)
```

The held-out suites are private. A public suite stops being blind, and a
suite the checker was tuned against becomes an operating-point corpus
(`source-policy.yaml`). They are in `openadapt-internal`, branch
`verification/heldout-suites` (OpenAdaptAI/openadapt-internal#76), under
`qualification/heldout-faults/openemr-referral/`, with their briefs, their
operation maps and every run's rows. Held-out 1 and 2 were also committed to
#1580's branch before the move (666f0c0cc and b90903a79), so their freezing
is provable from public history. This folder keeps each suite's hash
(`heldout*.yaml.sha256`). All five files were hashed again on 2026-11-03,
from the private branch and, for held-out 1 and 2, from those public
commits; all match.

## The contracts under test

| name | file | what it reads |
|---|---|---|
| production | `lifecycle_proof/verify.yaml` | the patient table, with `verify-run`'s default frame; no attribution, no provenance |
| read-list candidate (`plus`) | `lifecycle_proof/verify.frame.yaml` | the patient table plus the side tables a stray write could touch, a required history row, the provider, audit attribution by `pid`, and the provenance round trip |
| whole database (`whole`) | `lifecycle_proof/verify.database.yaml` | every table and column, with a reviewed allow-list (#1582) |

#1452 measured two older contracts, `shipped` (the task's own contract) and
`framed` (with audit attribution and the round trip), and the check that
`scaffold-verifier --db --fault-gate` proposes (`synthesized`).

A fault case is *caught* when the check does not return `verified`. A clean
case is *ok* when it does. "Right" counts both, "faults passed" counts
fault cases that got `verified`, and "false alarms" counts clean cases that
did not.

## Results

Gate (#1580): at least 15 of 17 right, and no fault passed as verified.
A result *counts* only when the suite was frozen before the check it ran
against was fixed. A result whose suite shaped the checker is kept and
marked as not counting.

Where a row cites a private file, the path is under
`qualification/heldout-faults/openemr-referral/` on the private branch.

### `faults.yaml` (public sample)

The first suite. Its cases were known to the authors of every later fix, so
from the read-list candidate on, no result on it counts.

| date | where | check | commit | right | faults passed | false alarms | counted | results file |
|---|---|---|---|---|---|---|---|---|
| 2026-10-24 | stand-in, 24 tables | shipped | dd8e99c05 | 7/17 | 8 | 2 | yes | `results/standin-slice/` |
| 2026-10-24 | stand-in, 24 tables | framed | dd8e99c05 | 14/17 | 3 | 0 | yes | `results/standin-slice/` |
| 2026-10-24 | stand-in, 24 tables | synthesized | dd8e99c05 | 14/17 | 0 | 3 | yes | `results/standin-slice/` |
| 2026-10-24 | stand-in, 283 tables | shipped, framed, synthesized | 2673f0ef4 | same as the slice, per case | | | yes | `results/standin-full/` |
| 2026-10-24 | live OpenEMR 8.0.0.3 | shipped | 2673f0ef4 | 7/17 | 8 | 2 | yes | `results/live-2026-10-24/` |
| 2026-10-24 | live OpenEMR 8.0.0.3 | framed | 2673f0ef4 | 9/17 | 6 | 2 | yes | `results/live-2026-10-24/` |
| 2026-10-25 | stand-in | production | 666f0c0cc | 7/17 | 8 | 2 | no | `results/phase0/666f0c0cc/faults-standin-production.rows.json` |
| 2026-10-25 | stand-in | read-list candidate | 666f0c0cc | 17/17 | 0 | 0 | no | `results/phase0/666f0c0cc/faults-standin-plus.rows.json` |
| 2026-10-25 | stand-in | whole database | e0728d03a | 17/17 | 0 | 0 | no | `results/phase0/e0728d03a/faults-standin-whole.rows.json` |
| 2026-10-28 | stand-in | whole database | 1821c79c8 | 17/17 | 0 | 0 | no | `results/phase0/1821c79c8/faults-standin-whole.rows.json` |
| 2026-11-03 | stand-in | production, on current main | c526ab686 | 14/17 | 0 | 3 | no; every clean save was refuted | `results/phase0/c526ab686/faults-standin-production.rows.json` |
| 2026-11-03 | stand-in | read-list candidate, whole database | c526ab686 | not run | | | | main refuses the contract (`record.surface`, #1576) |

The synthesized check refutes the clean save too, so its catches carry no
information.

The live run's raw rows, evidence and audit rows are private, in
`openadapt-corpus` (`reliability_recipes/verification_coverage/live-2026-10-24/`,
OpenAdaptAI/openadapt-corpus#18). `results/live-2026-10-24/RESULTS.md` has
the verdict per case.

### Held-out 1 (`b1bd44ae…`)

17 cases: wrong record 2, unrelated change 4, misread source 3, missing side
record 2, duplicate 2, concurrent legitimate 2 (clean), mixed 2.

| date | check | commit | right | faults passed | false alarms | counted | results file (private) |
|---|---|---|---|---|---|---|---|
| 2026-10-25 | production | 666f0c0cc | 4/17 | 11 | 2 | yes | `runs/666f0c0cc/heldout-standin-production.rows.json` |
| 2026-10-25 | read-list candidate | 666f0c0cc | **14/17** | 3 | 0 | yes: gate not met | `runs/666f0c0cc/heldout-standin-plus.rows.json` |
| 2026-10-25 | whole database | e0728d03a | 17/17 | 0 | 0 | no: this set's misses shaped the design | `runs/e0728d03a/heldout-standin-whole.rows.json` |
| 2026-10-28 | whole database | 6d0b1525d | 17/17 | 0 | 0 | no | `runs/6d0b1525d/heldout-standin-whole.rows.json` |
| 2026-10-28 | whole database | 1821c79c8 | 17/17 | 0 | 0 | no | `runs/1821c79c8/heldout-standin-whole.rows.json` |
| 2026-11-03 | production, on current main | c526ab686 | 15/17 | 0 | 2 | yes, but every clean save was refuted | `runs/c526ab686/heldout-standin-production.rows.json` |
| 2026-11-03 | read-list candidate, whole database | c526ab686 | not run | | | main refuses the contract (`record.surface`, #1576) | `runs/c526ab686/README.md` |

Correction: #1580's `HELDOUT.md` gave 9 faults passed for production here.
The committed rows have 11 (`verified` on 11 of the 15 fault cases).

The read-list candidate passed three bot writes outside the tables it reads.
That result led to the whole-database frame.

### Held-out 2 (`63b44803…`)

Written blind after the whole-database frame (#1582). The check commit,
c3987d90c, was pushed before the file was opened. 17 cases: wrong record 2,
unrelated change 3, misread source 3, missing side record 2, duplicate 2,
concurrent legitimate 3 (clean), mixed 2.

| date | check | commit | right | faults passed | false alarms | counted | results file (private) |
|---|---|---|---|---|---|---|---|
| 2026-10-25 | production | b90903a79 | 6/17 | 9 | 2 | yes | `runs/b90903a79/heldout2-standin-production.rows.json` |
| 2026-10-25 | read-list candidate | b90903a79 | 15/17 | 1 | 1 | yes: gate not met | `runs/b90903a79/heldout2-standin-plus.rows.json` |
| 2026-10-25 | whole database | b90903a79 | **16/17** | 0 | 1 | yes: **gate met** | `runs/b90903a79/heldout2-standin-whole.rows.json` |
| 2026-10-28 | whole database, after #1586 | 6d0b1525d | 17/17 | 0 | 0 | no: this set shaped the fix | `runs/6d0b1525d/heldout2-standin-whole.rows.json` |
| 2026-10-28 | whole database | 1821c79c8 | 17/17 | 0 | 0 | no | `runs/1821c79c8/heldout2-standin-whole.rows.json` |
| 2026-10-28 | whole database | 0c9287b85 | 17/17 | 0 | 0 | no | `runs/0c9287b85/heldout2-standin-whole.rows.json` |
| 2026-11-03 | production, on current main | c526ab686 | 14/17 | 0 | 3 | yes, but every clean save was refuted | `runs/c526ab686/heldout2-standin-production.rows.json` |
| 2026-11-03 | read-list candidate, whole database | c526ab686 | not run | | | main refuses the contract (`record.surface`, #1576) | `runs/c526ab686/README.md` |

b90903a79 changes only the harness after c3987d90c. The whole-database
contract's one false alarm was a clean save: OpenEMR stores street and city
in capitals, and the check compared them with the referral's mixed case
(#1524). #1586 lets a contract declare the stored form.

### Held-out 3 (`a00983b9…`)

The check was fixed at 6d0b1525d before the suite was written; the run is at
harness commit 5efb2986d. 17 cases: concurrent legitimate 5 (clean), mixed 3,
misread source 3, unrelated change 3, wrong record 1, missing side record 1,
duplicate 1.

| date | check | commit | right | faults passed | false alarms | counted | results file (private) |
|---|---|---|---|---|---|---|---|
| 2026-10-28 | production | 5efb2986d | 12/17 | 0 | 5 | yes | `runs/5efb2986d/heldout3-standin-production.rows.json` |
| 2026-10-28 | read-list candidate | 5efb2986d | 12/17 | 0 | 5 | yes | `runs/5efb2986d/heldout3-standin-plus.rows.json` |
| 2026-10-28 | whole database | 5efb2986d | **14/17** | 0 | 3 | yes: gate not met | `runs/5efb2986d/heldout3-standin-whole.rows.json` |
| 2026-10-28 | whole database | 1821c79c8 | 17/17 | 0 | 0 | no: this set shaped the fixes | `runs/1821c79c8/heldout3-standin-whole.rows.json` |
| 2026-10-28 | whole database | 0c9287b85 | 17/17 | 0 | 0 | no | `runs/0c9287b85/heldout3-standin-whole.rows.json` |
| 2026-11-03 | production, on current main | c526ab686 | 12/17 | 0 | 5 | yes, but every clean save was refuted | `runs/c526ab686/heldout3-standin-production.rows.json` |
| 2026-11-03 | read-list candidate, whole database | c526ab686 | not run | | | main refuses the contract (`record.surface`, #1576) | `runs/c526ab686/README.md` |

The whole-database contract's three false alarms came from two gaps: values
compared as text where the spec compares digits or calendar dates, and
another user's change to the bot's new chart never being excused.

### Held-out 4 (`b2f3ccb6…`)

The check was fixed at 1821c79c8 (after the review fixes in #1582, #1586,
#1597 and #1571) before the suite was written; the run is at harness commit
e299dd69c. 17 cases: wrong record 2, unrelated change 3, misread source 3,
missing side record 2, duplicate 2, concurrent legitimate 3 (clean), mixed 2.

| date | check | commit | right | faults passed | false alarms | counted | results file (private) |
|---|---|---|---|---|---|---|---|
| 2026-10-28 | production | e299dd69c | 14/17 | 0 | 3 | yes | `runs/e299dd69c/heldout4-standin-production.rows.json` |
| 2026-10-28 | read-list candidate | e299dd69c | 14/17 | 0 | 3 | yes | `runs/e299dd69c/heldout4-standin-plus.rows.json` |
| 2026-10-28 | whole database | e299dd69c | **17/17** | 0 | 0 | yes: **gate met** | `runs/e299dd69c/heldout4-standin-whole.rows.json` |
| 2026-10-28 | whole database | 0c9287b85 | 17/17 | 0 | 0 | no: later than the recorded check | `runs/0c9287b85/heldout4-standin-whole.rows.json` |
| 2026-11-03 | production, on current main | c526ab686 | 14/17 | 0 | 3 | yes, but every clean save was refuted | `runs/c526ab686/heldout4-standin-production.rows.json` |
| 2026-11-03 | read-list candidate, whole database | c526ab686 | not run | | | main refuses the contract (`record.surface`, #1576) | `runs/c526ab686/README.md` |

### Held-out 5 (`01104c9e…`)

The check was fixed at 784fc15e4, with the contract pinning the OpenEMR
profile `openemr-frame-profile/2026-10-29.2`, and pushed before the suite was
opened; the run is at harness commit 3cf03e43c. 17 cases: wrong record 2,
unrelated change 4, misread source 3 (one a clean control), missing side
record 1, duplicate 1, concurrent legitimate 3 (clean), mixed 3. Two cases
needed a row placed before the window opens (an existing insurance row, a
colleague's setting), as their steps assume one exists.

| date | check | commit | right | faults passed | false alarms | counted | results file (private) |
|---|---|---|---|---|---|---|---|
| 2026-10-28 | production | 3cf03e43c | 14/17 | 0 | 3 | yes | `runs/3cf03e43c/heldout5-standin-production.rows.json` |
| 2026-10-28 | read-list candidate | 3cf03e43c | 13/17 | 0 | 4 | yes | `runs/3cf03e43c/heldout5-standin-plus.rows.json` |
| 2026-10-28 | whole database | 3cf03e43c | **17/17** | 0 | 0 | yes: **gate met** | `runs/3cf03e43c/heldout5-standin-whole.rows.json` |
| 2026-11-03 | production, on current main | c526ab686 | 14/17 | 0 | 3 | yes; 3 of the 4 clean saves were refuted | `runs/c526ab686/heldout5-standin-production.rows.json` |
| 2026-11-03 | read-list candidate, whole database | c526ab686 | not run | | | main refuses the contract (`record.surface`, #1576) | `runs/c526ab686/README.md` |

### Re-run on current main (2026-11-03)

Commit c526ab686 is main at 7291dc284 plus this folder's harness, with no
change under `openadapt_flow/`. The stand-in used the pinned `baseline.sql`
(sha256 `a1b14e00…`). The rows above dated 2026-11-03 are new; none replaces
an older row. The held-out rows are in the private branch at c59e30c5.

- **Production** passes no fault as verified on any suite, but it also
  refutes the clean saves: 19 of the 20 clean cases across the six
  suites. The stand-in now saves street and city in capitals, as OpenEMR's
  form does (since 6d0b1525d), and `lifecycle_proof/verify.yaml` compares
  them exactly. A check that refutes every save catches every fault, so
  these catches carry no information. This is the gap #1586 closes for a
  contract that declares the stored form.
- **Read-list candidate and whole database** don't load on main. Both
  contracts declare `record.surface`, which is in #1576 (open), so
  `verify-run` stops with `RecordConfig surface: Extra inputs are not
  permitted`. They need #1576 on main, or a contract rewritten without it,
  before they can be re-run here.

### Live OpenEMR

| suite | production | read-list candidate | whole database |
|---|---|---|---|
| `faults.yaml` | pending a fixture lease | pending a fixture lease | pending a fixture lease |
| held-out 1 to 5 | pending a fixture lease | pending a fixture lease | pending a fixture lease |

Only #1452's two older contracts have run live (above). The held-out suites
have run only on the stand-in. The whole-database snapshot has run once on
the live fixture, on one correct save (`WITNESS_LIVE.md`).

## What the stand-in is

`standin.py` converts the pinned fixture's real `baseline.sql` (283 tables,
256,044 rows) to SQLite. Every table, column, default and installer row is
OpenEMR's. The writes are emulated: the New Patient save writes the rows
`WITNESS.md` lists, and each named user's write adds the audit row OpenEMR
writes. The live run of 2026-10-24 showed these audit rows are more generous
than OpenEMR's own, so the stand-in overstates what audit attribution can
excuse (`WITNESS.md`, amendment 5).

## Limits

- One referral, one app. The counts are cases, not rates.
- Held-out results are stand-in results. Live OpenEMR may differ, as it did
  for `framed` on 2026-10-24.
- The held-out authors wrote from a brief and `SPEC.md` only. Their briefs
  are in the private folder.

## Run it

```bash
cd packages/flow/benchmark
python -m verification_coverage.independent.phase0 standin \
  --baseline-sql "$OPENEMR_FIXTURE_STATE/baseline.sql" --out OUT \
  [--contract production|plus|whole] [--suite PATH]
python -m verification_coverage.independent.summarize OUT/rows.json
# the older checks of #1452:
python -m verification_coverage.independent.run \
  --baseline-sql "$OPENEMR_FIXTURE_STATE/baseline.sql" --work WORK --out OUT [--slice]
# with the fixture lease held:
python -m verification_coverage.independent.phase0 live --out OUT
```

`--suite` reads a private suite and its `<suite>.ops.yaml`. No model calls.
