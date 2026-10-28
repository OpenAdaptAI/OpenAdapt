# Phase 0 results: the third held-out fault suite

## Fixed before the suite is written

Recorded and pushed before `heldout3.yaml` is authored:

- The checks under test are at measurement commit `6d0b1525d` (branch
  `measure/phase0-independent`). It contains the whole-database frame (#1582)
  and stored forms (#1586), and the contract `lifecycle_proof/verify.database.yaml`
  (`--contract whole`).
- The brief is `heldout3-fault-brief.md`, in the coordinator's notes. It has
  the same rules, classes and operations as the second brief.

After the suite is frozen, the only changes allowed are in the harness: the
operations its author describes in words. Any case that can't run as written
is reported as not run.
