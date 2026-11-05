# Handover: #1919 pixel grounding (to Codex, 2026-11-05)

## State

- PR #1919, branch `claude/pixel-grounding-drift` (also on
  `claude/pixel-grounding-round2`). Frozen product code `9809ecab2`. Every
  commit after it changes benchmark files and documents only.
- Product changes against `main`:
  - moved look-alikes need located retained context, otherwise a typed
    refusal (`runtime/resolver.py`, `vision/match.py`);
  - the post-click heal adds no `context_text` to an anchor without an
    identity band (`runtime/heal.py`);
  - the geometry estimate snaps to the control's outline, refusing when
    separate copies are near (`resolver._outline_corroborated`,
    `match.outline_matches_near`).
- Status: no GO. The public held-out suite under pre-registration 3 met every
  bar ([results/round3/RESULT.md](results/round3/RESULT.md)). The private
  full replay has not been run.

## Pre-registrations and amendments

| File | Commit |
|---|---|
| `PREREGISTRATION.md` (round 1) | `f55800815` |
| `PREREGISTRATION_2.md` (round 2, development) | `42df059b2` |
| `PREREGISTRATION_2_AMENDMENT.md` | `5de7433a9`, `30d493a96` |
| `PREREGISTRATION_3.md` (confirmatory; sha256 `af9b9c91…c776`) | `17784ff62` |
| `PREREGISTRATION_3_AMENDMENT.md` (private replay: seed, arms, isolation) | `c9616912c` |
| `PREREGISTRATION_3_AMENDMENT_2.md` (exact draw rules) | `16e753d43` |
| `PREREGISTRATION_3_AMENDMENT_3.md` (structural and former-unique move-80 share draws; case-list hash `987ec991…4c9a`) | `0ea1ed24d` |

## Running the public held-out suite

Run one arm at a time through `~/oa/scratch/cpu-slots/run.sh`, with the load
under 35. For arm P, check out `89f51575` and copy this folder's
`run_arms.py` and `PREREGISTRATION_3.md` into it. Set `PYTHONPATH` to the
arm's `packages/flow`.

```bash
python benchmark/pixel_drift/run_arms.py --arm N-off --suites heldout --heldout-prereg PREREGISTRATION_3.md --output out.json
python benchmark/pixel_drift/run_arms.py --arm N-on --local-grounding --suites heldout --heldout-prereg PREREGISTRATION_3.md --output out.json
```

Regression suites: add `--suites drift21,controls,avc444,rdp_live`,
`--avc444` with the campaign folders under `~/oa/scratch/avd_real/out`
(`pn-run-3`, `emr-run-2`, `emr-run-3`), and `--rdp-live` with
`~/oa/scratch/pixel-drift-rdp/rdp_{clean,shaped,bad}`. These frames are not
in the repository.

## Open questions

1. The private full replay under pre-registration 3 and amendments 1 to 3:
   the GO or NO-GO decision.
2. State-transfer targets stay refused or missed by design (reorder
   argument, round 2 (a)). A reviewer may want this availability cost
   accepted explicitly.
3. With local grounding on, more targets are refused (dark: 13 of 39). The
   model rung refuses where geometry would have snapped. A possible
   follow-up is to abstain there instead of refusing.
4. Local model latency is far over #1848's 50 ms target. It stays opt-in.
5. #1960: `test_layout_drift::test_real_render[font_size_changed]` fails on
   `main`. It is unrelated to this PR.
