# Amendment 1 to pre-registration 3: the private full-replay fixture

Written on 2026-11-05, before any private case is generated or run. The
held-out seed of `PREREGISTRATION_3.md` is unchanged, because that file is
unchanged. Bars are unchanged.

## Seed

```
p3 = sha256(bytes of PREREGISTRATION_3.md)        # af9b9c91…c776
seed = int(sha256(p3.hexdigest() + ":private-full-replay").hexdigest()[:16], 16)
```

The public held-out pages use `p3` alone, so the two sets are drawn
independently from the same frozen file.

## Generator

The owners' existing `test_registered_controls.py` (the round 1 private
fixture) with its 44 case groups unchanged: the same widget kinds, row
pitches, transfer distances, landmark variants, structural and identity cases,
the same 800x600 viewport and the same oracle. Only its free parameters are
drawn from `random.Random(seed)`, in case order: label text (from the
fixture's own neutral word source), list origin within the viewport, and each
group's choice among its declared pitches and distances. No group, criterion,
threshold or label rule is added, removed or changed.

## Cases and arms

- 44 cases per local grounding setting, so 88 per code arm and 176 full
  `Replayer.run` entries in all.
- Every case runs in all four arms: P-off, P-on, N-off, N-on. The same
  generated fixture bytes go to every arm.
- The case list and its mapping to arms are written out and hashed before any
  case runs. The mapping is frozen from then on, and no case is dropped,
  added or rerun after its outcome is seen.

## Isolation

P and N are separate whole checkouts: P at `89f51575`, N at `9809ecab2`.
Exactly three runtime files differ between them:
`runtime/resolver.py`, `vision/match.py` and `runtime/heal.py`. Each arm runs
its own checkout's code for all three, not a resolver swapped into one tree.
Runtime packages, browser and model weights are identical across arms, as in
round 1.

## Assertions kept

The fixture's existing `guarded_coordinate` and identity assertions stay
intact. They are reported exactly as they come out, including any that fail
or are not reached, and are not treated as passing when skipped.
