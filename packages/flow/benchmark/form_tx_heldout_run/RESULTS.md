# Results: form layer on held-out synthetic quote portals

Run 2026-11-21 on the zenbook lab (WSL, headless Chromium), once per arm, in
the pre-registered order. Engine `6392bac91` (`ENGINE_SHA`), portals and answer
key from `claude/form-tx-heldout-portals` at `36be56dcd` (answer key sha256
`948f695a...7227`). The pre-registration was pushed at `8ea2e5602`, before the
portals were merged into this branch at `eeb53cadb`; deviations D1 and D2 were
recorded before any scored case ran. The runner and packs were committed at
`3312524d5`, before the first scored case; `git diff 6392bac91 124db5c98 --
packages/flow/openadapt_flow` is empty (124db5c98 is the last commit of this
measurement; later commits on this branch change the engine), so the engine did not change after
the freeze. Raw rows: `results/arm_B.jsonl`, `results/arm_C.jsonl`,
the JSON lines in `results/arm_A_full.log`, and the full console logs `results/arm_*_full.log`.
Counts: `summarize.py`.

**These portals are no longer held out.** This change publishes them with
their answer key. Any later engine change measured on them is a dev-set
result; a new held-out claim needs new portals.

Everything here is synthetic. The four portals and their carriers are made
up. The pack author (the form layer's author) read the portal source, which
shows how the traps work, and wrote each pack by hand from one dev case.

## Headline

On the one portal it could operate, the layer submitted 6 of 9 quotes, all
correct, and submitted no wrong value. That "no wrong value" is narrow: the
6 submitted cases exercised only prefill conflicts, unknown questions, one
summary-differs case and one rule trap (table below). The estimator rounding,
session timeout and type-ahead traps never reached a submit, so the layer was
not tested against them. It verified none of them: that portal
shows the stored quote as plain text, which the layer cannot read, so every
field was reported `not_checked`. On the other three portals it submitted
nothing, because the frozen browser surface cannot see forms in an iframe or
in shadow DOM, and cannot fill a type-ahead that needs typed text. Rule
learning proposed rules but admitted none, and changed nothing.

## Denominators

32 scored cases: harborline 9, cobalt 8, pinecrest 8, tidewater 7. From the
answer key's `expected_behavior`: **18 trap cases and 14 clean cases**. The
pre-registration's "20 trap / 12 clean" was inferred from per-portal counts
and was wrong; under D1 the answer key's counts are used.

## Pre-registered metrics

| Metric | A: unguarded filler | B: layer, learning off | C: layer, learning on |
| --- | ---: | ---: | ---: |
| Correct submissions (of 32) | 12 | 6 | 6 |
| Correct and `verified_done` | n/a | 0 | 0 |
| Wrong submitted | 18 | 0 | 0 |
| - with `verified_done` (false proof) | n/a | 0 | 0 |
| - flagged after submit | n/a | 0 | 0 |
| - silent | 18 | 0 | 0 |
| Unsubmitted | 2 | 26 | 26 |
| Trap cases stopped before submit (of 18) | 0 | 6 | 6 |
| - then submitted correctly | n/a | 4 | 4 |
| Clean cases with a false stop (of 14) | 0 | 3 (3 halts) | 3 (3 halts) |
| Fields `not_checked` | n/a | 134 | 134 |
| Cases ending `readback_incomplete` | n/a | 6 | 6 |
| Operator hand-backs: halts | 0 | 20 | 20 |
| Operator hand-backs: post-submit flags | 0 | 6 | 6 |
| Runs that raised an error or got stuck | 2 (1 error, 1 stuck) | 8 (all cobalt) | 8 (all cobalt) |

On the registered denominator, 3 of 14 clean cases (21%) had a false stop.
Read post hoc, that number is low only because 11 clean cases were on portals
where the layer never got far enough to stop: all 3 clean cases it reached
stopped.

### By trap (arm B; a case can carry several traps)

| Trap | Cases | Layer reached the portal's form | Layer submitted | Layer correct | Unguarded filler wrong |
| --- | ---: | ---: | ---: | ---: | ---: |
| prefill conflict | 7 | 2 | 2 | 2 | 6 |
| prefill agrees | 5 | 2 | 2 | 2 | 1 |
| unknown question | 10 | 6 | 4 | 4 | 9 |
| recurring rule | 6 | 6 | 4 | 4 | 6 |
| rule trap | 1 | 1 | 1 | 1 | 1 |
| summary differs | 6 | 2 | 1 | 1 | 6 |
| estimator wrong | 2 | 1 | 0 | 0 | 2 |
| estimator ok | 15 | 8 | 6 | 6 | 8 |
| session timeout | 3 | 1 | 0 | 0 | 3 |
| type-ahead ambiguous | 5 | 0 | 0 | 0 | 4 |
| clean | 12 | 3 | 2 | 2 | 0 |

"Reached" means the case was on harborline, the only portal whose form the
layer could see. The one estimator-wrong case it reached (hl-s03) was
escalated at an earlier stop, before the operator's by-hand estimator step;
that step clicks Use This Value, so it would have entered the rounded
figure, and the field would have been `not_checked`.

### Per portal, arm B

| Portal | Cases | Submitted | Correct | Wrong | Stops before submit | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| harborline (table layout, popup estimator) | 9 | 6 | 6 | 0 | 9 cases, 20 halts | 6 `readback_incomplete`, 3 `escalated_to_owner` |
| cobalt (single-page app, type-aheads) | 8 | 0 | 0 | 0 | 0 | 8 errors: `MissingAnchorError` on the first type-ahead |
| pinecrest (form in an iframe) | 8 | 0 | 0 | 0 | 0 | 8 `readback_refuted` with nothing submitted |
| tidewater (shadow DOM) | 7 | 0 | 0 | 0 | 0 | 7 `readback_refuted` with nothing submitted |

Arm C matched arm B case for case.

Arm A (the portal author's unguarded filler, re-run once unchanged): 12
correct, 18 wrong values submitted with no flag, 2 not submitted (one
browser error on hl-s04, one stuck on cobalt). Per portal: harborline 2 of
9 correct, cobalt 3 of 8, pinecrest 4 of 8, tidewater 3 of 7. The portal
author's own earlier run gave 13 / 18 / 1. The traps are real: an unguarded
filler gets more than half the quotes wrong, silently.

## Reject criteria

- **R1, "a receipt means the quote is right":** not killed, but not shown
  either. No receipt was issued, because nothing could be read back.
- **R2, "no wrong value goes through silently":** not killed. 0 wrong values
  submitted; but only 6 cases submitted at all.
- **R3, "it stops wrong values before submit":** 6 of 18 trap cases (33%)
  stopped before submit. That is below the 75% bar, so the claim may only
  state the fraction. The 12 unstopped trap cases were on portals where the
  layer submitted nothing.
- **R4, "it does not stop clean work":** not killed on the registered
  denominator: 3 of 14 clean cases (21%, under the 25% bar). Read post hoc,
  the claim is still unsupported: all 3 clean cases the layer reached
  stopped, and the other 11 were never reached.
- **R5, "rule learning reduces hand-backs":** **killed.** 20 hand-backs in
  both arms. Candidate rules were proposed after the second consistent answer
  but every admission was refused: the qualification replays the dev case and
  needs `verified_done`, which no harborline case can reach. Several
  candidates also had no visible label to anchor on. The learning claim is cut.

## What each stop was (harborline, arm B)

Every harborline case stopped at the replacement-cost estimator: it needs
Calculate and then Use This Value, and a popup routine has one apply button.
The scripted operator then ran the estimator by hand (deviation D2). Without
that stop, per case (post-hoc reading of the same rows, not a new run):

| Case | Expected | Other stops before submit | Result |
| --- | --- | --- | --- |
| hl-s01 | needs operator answer | Construction Class (unknown question) | correct |
| hl-s02 | needs operator answer | roof year and losses (prefill conflicts), Construction Class | correct |
| hl-s03 | needs operator answer | estimator Quality Grade (popup default) | escalated: the stop was inside the popup and the operator could not find its label |
| hl-s04 | clean | none | correct |
| hl-s05 | needs operator answer | Construction Class, then a session expiry read as a validation error | escalated |
| hl-s06 | needs operator answer | Construction Class | correct |
| hl-s07 | clean | none | correct |
| hl-s08 | needs operator answer | street, roof year, roof material (prefill conflicts), Construction Class | correct |
| hl-s09 | clean | estimator Quality Grade (popup default) | escalated: a false stop |

## Engine findings

1. **Text-only summaries cannot be read back.** The surface reads values only
   from form controls. Harborline's confirmation page shows stored values as
   table text, so 134 field verdicts were `not_checked`.
2. **No iframe or shadow-DOM support.** Pinecrest showed 0 controls,
   tidewater a single button.
3. **Type-aheads that need typing are unsupported, and fail as errors.** The
   surface opens a listbox by clicking; when none appears it raises
   `MissingAnchorError` out of the run instead of stopping for a person.
4. **An empty pack ends `readback_refuted` without submitting.** With no
   screens or bindings the layer goes straight to read-back. It is fail-closed
   (never verified) but the outcome name says "refuted" about a submit that
   never happened.
5. **A session expiry page with an error banner is classified as a
   validation error**, not an authentication wall, so the operator policy
   did not sign in.
6. **Two-step popups are not expressible.**
7. **An unchecked native radio reads as prefilled** (its value attribute), so
   radios had to be set from the intake.
8. **Rule qualification depends on a verified dev run**, so no rule can be
   admitted on a portal whose summary cannot be read.

## Post-hoc row (labelled, not part of the frozen result)

None has run. `run.py --posthoc` defines one (harborline pack P1, which
overwrites the estimator's pre-set Quality Grade, plus an operator sign-in
when a stop says the session expired). It was queued on the lab and stopped
unrun when the lab had no free memory slot. Any post-hoc result goes in its
own labelled row and never replaces the frozen one.

## What this cannot show

Four synthetic portals written by one agent are not carrier portals. Nothing
here says how the layer behaves on real carriers, real sessions, CAPTCHAs or
real operators.
