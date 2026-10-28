# Phase 0: held-out fault suites (aggregate results)

The blind held-out suites, their per-case results and their briefs are
private (`openadapt-internal`, `qualification/heldout-faults/`). Tuning
against a held-out suite makes it an operating-point corpus, and a public
suite stops being blind. This file keeps what can be public: each suite's
hash, its counts per class, and aggregate scores with the commit measured.
The harness is public; `phase0 --suite PATH` runs a private suite.

Gate: at least 15 of 17 right, with no fault passed as verified.

Contracts: production (`lifecycle_proof/verify.yaml`); read-list candidate
(`verify.frame.yaml`); whole database (`verify.database.yaml`, #1582).

## Held-out 1

`sha256 b1bd44aeb375f4066f260a1aaaa8315ab3cde74b145883190574c499e3d8c550`.
17 cases: wrong record 2, unrelated change 4, misread source 3, missing side
record 2, duplicate 2, concurrent legitimate 2 (clean), mixed 2.

| contract | commit | right | faults passed as verified | counted |
|---|---|---|---|---|
| production | 666f0c0cc | 4/17 | 9 | yes |
| read-list candidate | 666f0c0cc | **14/17** | 3 | yes: gate not met |
| whole database | e0728d03a | 17/17 | 0 | no: this set shaped the design |

The read-list candidate passed three bot writes outside the tables it reads.
That led to the whole-database frame.

## Held-out 2

`sha256 63b44803325a5efab57c75b153a68c3267a59a322bed491650f78d6e0359b182`.
The check commit was recorded before the suite was opened. 17 cases: wrong
record 2, unrelated change 3, misread source 3, missing side record 2,
duplicate 2, concurrent legitimate 3 (clean), mixed 2.

| contract | commit | right | faults passed as verified | counted |
|---|---|---|---|---|
| production | b90903a79 | 6/17 | 9 | yes |
| read-list candidate | b90903a79 | 15/17 | 1 | yes: gate not met |
| whole database | b90903a79 | **16/17** | 0 | yes: **gate met** |
| whole database, after #1586 | 6d0b1525d | 17/17 | 0 | no: this set shaped the fix |

The whole-database contract's one wrong verdict was a clean case it flagged.
OpenEMR saves street and city in capitals, and the record check compared
them with the referral's mixed case (#1524). #1586 lets a contract declare
the stored form.

## Held-out 3

`sha256 a00983b9f7b5a6de587c99c12352fa8c6cf961d8cb67ca28b9be7c1304965c08`.
The check was fixed at `6d0b1525d` before the suite was written; the run is at
harness commit `5efb2986d`, with no check changes. 17 cases: concurrent
legitimate 5 (clean), mixed 3, misread source 3, unrelated change 3, wrong
record 1, missing side record 1, duplicate 1.

| contract | right | faults passed as verified | false alarms | counted |
|---|---|---|---|---|
| production | 12/17 | 0 | 5 | yes |
| read-list candidate | 12/17 | 0 | 5 | yes |
| whole database | **14/17** | 0 | 3 | yes: **gate not met** |

Every contract caught all 12 faults. The whole-database contract flagged
three of the five clean cases, from two gaps:

- **Value kinds:** a phone typed with other punctuation, and a date typed in
  another format, are compared as text, where the spec compares digits and
  calendar dates.
- **Concurrent edits to the new chart:** another user's change to the bot's
  new record is never excused, even in a field the save didn't set.

## After the review fixes (stand-in, not counted)

The adversarial review of #1582 found 7 silent passes, a false hold on any
table over `max_rows`, an inconsistent read and plaintext rows in the
baseline. The fixes are in #1582, #1586, #1597 and #1571. They change the
checker, so the scores above stand as measured. At measurement commit
`1821c79c8` the whole-database contract gets **17/17 on the stand-in on all
four suites** (the original suite and held-out 1, 2 and 3), passes no fault as
verified, and reads every frame. None of these four scores counts: each set
shaped the checker.

The figure that counts today is held-out 3's **14/17** at `6d0b1525d`.

## Held-out 4

`sha256 b2f3ccb62f5c0159a26224f29b1abf8f67008b014b872ce4f92d6f41c6ad41b3`.
The check was fixed at `1821c79c8` before the suite was written; the run is
at harness commit `e299dd69c`, with no check changes. 17 cases: wrong record
2, unrelated change 3, misread source 3, missing side record 2, duplicate 2,
concurrent legitimate 3 (clean), mixed 2.

| contract | right | faults passed as verified | false alarms | counted |
|---|---|---|---|---|
| production | 14/17 | 0 | 3 | yes |
| read-list candidate | 14/17 | 0 | 3 | yes |
| whole database | **17/17** | 0 | 0 | yes: **gate met** |

All three contracts caught all 14 faults. Only the whole-database contract
passed the three clean cases: another user's work in the same window,
including on the bot's new chart, and values that differ only in stored
format. This is a stand-in result; the live OpenEMR run is next.

## Held-out 5

The check is fixed at measurement commit `784fc15e4`, recorded and pushed
before the suite was opened: #1582, #1586, #1597 and #1604 after three
review rounds, with the whole-database contract pinning the OpenEMR profile
`openemr-frame-profile/2026-10-29.2`
(`sha256:a3daefcf7b7f96e0c68a7d5c1829c3e358dc1ce9366e3098d18bc80441685bed`).
It runs on the stand-in and on live OpenEMR; both results count.

## Live OpenEMR

Pending: queued for the fixture lease.
