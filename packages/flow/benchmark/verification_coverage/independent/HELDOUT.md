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

The check is fixed at `6d0b1525d` before the suite is written. The suite
will be authored, stored and run privately; its hash and aggregate result
will be added here.

## Live OpenEMR

Pending: queued for the fixture lease.
