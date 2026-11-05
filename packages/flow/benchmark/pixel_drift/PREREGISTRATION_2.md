# Pre-registration 2: moved-control refusals, the post-click heal, dark geometry

Written on 2026-11-05, before any code change for this round. The base is
`89f51575` (the candidate measured NO-GO; its private evidence packet is the
reference). The held-out set below is generated from a seed fixed by this
file's own bytes, so it was not seen, rendered or queried when the changes
were made.

## What this round addresses

- **(a) Template refusals.** The candidate refused 36 targets on the
  template rung. Of the 18 per local setting, 16 were state-transfer cases:
  the checked appearance moved to another row while the true control stayed.
  The other 2 were a unique arrow moved 34 or 80 px with no retained context.
- **(b) A correct click reported as a failure.** After a correct click on a
  non-template rung, the heal proposed new `context_text` for an anchor that
  had no recorded context or template identity band. The governor rejected
  the heal, so the run reported `success=False`.
- **(c) Two wrong dark-theme points with local grounding off.** The geometry
  rung placed `chk-1` and `rad-1` about 11 px left of the control's centre,
  1 px outside the padded truth box.

## Planned changes

- **(a)** A moved template match is not revived by whole-frame uniqueness,
  appearance confidence or scale. The only new route to a click is located
  retained context that puts the target on the match, which the candidate
  already accepts. For state transfer, the planned change considers the
  recorded spot itself: when that spot still holds the same control in another
  state, it is the target. "Same control in another state" means the crop's
  edge outline, which a checked or unchecked fill leaves in place, matches at
  the recorded spot, and no other place matches the outline better. Whether
  this can be done with zero wrong clicks is measured, not assumed. If it
  cannot, these cases stay refused, and the report says so.
- **(b)** The heal keeps an unarmed anchor's identity evidence as recorded.
  It does not add `context_text` where the anchor had no context or template
  band. The governor is unchanged.
- **(c)** The geometry rung's estimate snaps to the control's edge outline
  within half the control's size of the estimate, only when exactly one place
  there matches. Otherwise the step refuses rather than clicking an
  uncorroborated estimate.

No existing correlation, OCR, landmark, locality, risk or identity threshold
changes. Nothing is specific to a theme or an application.

## Held-out set (generated, never seen before the code freeze)

`run_arms.py --suites heldout` renders pages from
`seed = int(sha256(PREREGISTRATION_2.md bytes)[:16], 16)` (this file, as
committed). Each of 6 pages has:

- 5 to 7 rows at a pitch of 30, 34 or 40 px, at a random origin;
- each row has one control (checkbox, radio, or one of the eight SVG icons)
  and a two-word label drawn from a fixed neutral word list.

Each page appears in five conditions. Each control is a target, with the
label as its retained landmark unless the condition says otherwise.

| Condition | Change | Correct outcome |
|---|---|---|
| `unchanged` | none | click the control |
| `moved` | the whole list moves 34 to 120 px, labels move with it | click the control |
| `dark` | dark theme, same layout | click the control or refuse; never another place |
| `transfer_missing` | the checked state moves one or two rows; the target row's label is removed | click the control or refuse |
| `transfer_none` | as above; the anchor keeps no landmark | click the control or refuse |

Truth is the control's page box padded by 3 px, as in the first round.

## Arms

| Arm | Code | Local grounding |
|---|---|---|
| P-off, P-on | `89f51575` (measured candidate) | off, on |
| N-off, N-on | this round's final head | off, on |

The first round's suites (drift-21, controls, the 81 AVC444 frames, the live
RDP frames) are rerun on N-off and N-on as a regression check. They are no
longer held out.

## Thresholds (fixed now)

- **Hard gate:** 0 wrong points in N-off and N-on on every held-out condition
  and every regression suite. Any wrong point blocks the change.
- `unchanged`: N correct at least 98% of targets.
- `moved`: N correct at least 90% of targets.
- `dark` with local grounding off: 0 wrong (the issue in (c)); correct is
  reported, with no minimum.
- `transfer_missing` and `transfer_none`: 0 wrong. **Refusal target:** N
  refuses at most 50% of these targets, against P's rate. If N cannot get
  below 50% with 0 wrong, the outline-at-spot rule is dropped and the result
  is reported as not met.
- **(b):** a real-Replayer test in which a correct geometry or OCR click on an
  unarmed anchor triggers the heal and the run reports success, with the
  governor unchanged. The full-replay suite behind the NO-GO is private; its
  owners should rerun it on a fresh generation before any GO.
- Refusals and misses are reported separately from wrong points for every
  arm and condition.
