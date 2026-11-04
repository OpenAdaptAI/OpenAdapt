# Pre-registration: screen-only grounding under drift

Written and committed on 2026-11-04, before any of the measurements below
were taken on this branch. The base is `origin/main` at `c21ba89c4`.

## Question

On a pixel-only surface (Citrix, RDP, VNC), how often does the resolution
ladder find the right control, click the wrong one, or refuse? Does the local
grounding rung (`runtime/local_grounder.py`, off by default) help, and can it
be turned on by default?

## Arms

| Arm | Code | Local grounding rung |
|---|---|---|
| A | `origin/main` at `c21ba89c4` | off |
| B | `origin/main` at `c21ba89c4` | on |
| C | this branch | off |
| D | this branch | on |

Every arm runs with no structural backend (screen only). Each target gets one
of four outcomes: **correct** (the point is inside the true control's box),
**wrong** (a point anywhere else), **refused** (a typed refusal), **miss**
(no rung resolved). Wrong targets and refusals are reported separately.

## Suites

1. **Drift-21.** The 21-button page of `benchmark/structural_action`
   (6 buttons unchanged, 15 moved to another column, relabelled Open to View
   and re-themed). Screen-only resolution of each button. The 15 drifted
   buttons are identical to one another and carry a different label; no
   pixel evidence on the page tells them apart. The safe ceiling for a
   screen-only method is therefore 6 correct and 15 refused.
2. **Icon and checkbox.** A rendered page of icon-only toolbar buttons
   (distinct glyphs, plus a repeated glyph told apart by a text landmark),
   checkboxes and radio buttons with labels. Conditions: unchanged, display
   scale 125% and 150%, dark theme, JPEG quality 50 with 4:2:0 chroma, every
   box toggled to the other state.
3. **AVC444.** The real frames from the H.264/AVC444 RDP campaigns behind
   `benchmark/rdp_avc_real` (Windows Server 2022 host, stock client). Full
   ladder per click step on the step's `before` frame, with that benchmark's
   ground truth and tolerance.
4. **Latency.** Local rung query time, p50 and p95, at 1920x1080, on Apple
   Silicon CPU (this Mac, M2 Max) and on a laptop x86 CPU when the Windows lab
   laptop is free.

## Thresholds (fixed now)

- **Blocking:** any wrong target in arm C or D on suites 1 to 3 blocks this
  pull request's default-path change and any default-on proposal.
- **Drift-21:** arm C and D wrong = 0 (arm A is expected to have wrong
  targets; the number is measured, not assumed), correct >= arm A's correct
  among the 6 unchanged buttons.
- **Icon and checkbox:** arm D correct >= arm A correct + 25% of the targets;
  wrong = 0.
- **AVC444:** wrong = 0 in arm C and D; arm C correct >= arm A correct - 3
  (a new refusal costs a false stop, never a wrong click; more than 3 is a
  regression).
- **Latency:** issue #1848's target is 50 ms per query. Earlier probes put
  p50 near 130 ms at 1080p, so the target is expected to be missed; the result
  is reported either way.
- **Default on** for the local rung needs all of: wrong = 0 on every suite,
  p95 <= 250 ms at 1080p on both CPUs, and no new false stop on AVC444.
  Otherwise it stays opt-in.
