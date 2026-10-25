# Phase 0 results: the second held-out fault suite

## Fixed before reading

Recorded and pushed before `heldout2.yaml` was opened:

- The checks under test are at measurement commit
  `c3987d90c5fa5d7eacd7ce605ca9a1953277fb3f` (branch
  `measure/phase0-independent`). That commit contains the whole-database
  frame at `6e0742801ab6545b19d160916c00d0d8adefdfda` (#1582) and the
  contract `lifecycle_proof/verify.database.yaml` (`--contract whole`).
- The suite, as the coordinator froze it: `heldout2.yaml`, sha256
  `63b44803325a5efab57c75b153a68c3267a59a322bed491650f78d6e0359b182`.
  It holds 17 cases: 14 faults and 3 clean.

After reading, the only changes allowed are in the harness: the four
operations the author described in words, and the `register_patient` person
shape. No check, contract or case changes. Any case that can't run as
written is reported as not run.
