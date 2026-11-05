# Pre-registration 3: confirmatory run of the frozen candidate

Written on 2026-11-05. The candidate is frozen at `9809ecab2` (product code).
After that commit, only benchmark files and documents change. The held-out
seed is drawn from this file's own bytes when it is committed, so it is drawn
after the freeze, and no page from it has been rendered.

## Seed and set

`run_arms.py --suites heldout --heldout-prereg PREREGISTRATION_3.md` renders
the round 2 generator (6 pages, 5 to 7 rows each, five conditions) from
`seed = int(sha256(PREREGISTRATION_3.md bytes)[:16], 16)`. The generator code
is unchanged from round 2.

## Arms

| Arm | Code | Local grounding |
|---|---|---|
| P-off, P-on | `89f51575` (measured NO-GO candidate) | off, on |
| N-off, N-on | `9809ecab2` (frozen candidate) | off, on |

Regression suites on N-off and N-on: drift-21, controls, the 81 AVC444 frames
and the 1,428 live RDP targets. These are not held out.

The private full replay is run by its owners on a fresh generation of their
`test_registered_controls.py` fixture, drawn after this commit. It uses the
same 44 case groups, P against N, with local grounding off and on.

## Thresholds (fixed now)

- **Hard gate:** 0 wrong points or inputs, in N-off and N-on, on every
  held-out condition, every regression suite and the private full replay.
  Any wrong blocks a GO.
- Held-out `unchanged`: N correct at least 98%.
- Held-out `moved`: N correct at least 90% with local grounding off, and at
  least 85% with it on.
- Held-out `dark`, local grounding off: N correct at least 80%.
- Held-out `transfer_missing` and `transfer_none`: N correct at least P's.
  These targets are refused or missed by design (round 2, (a)). There is no
  refusal-rate target beyond not exceeding P's refused + missed count.
- Private full replay: in every case where N clicks the right control, the
  run reports success (the round 2 heal change). Structural and identity
  cases keep their registered outcomes.
- Refusals and misses are reported separately from wrong results, per arm
  and condition. A GO needs every line above met. Otherwise the result is
  NO-GO, with no tuning or rerun on this seed.
