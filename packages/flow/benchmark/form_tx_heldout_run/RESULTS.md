# Results: form layer on held-out synthetic quote portals

Run 2026-11-21 on the zenbook lab (WSL, headless Chromium), once per arm, in
the pre-registered order. Engine `6392bac91` (`ENGINE_SHA`), portals and answer
key from `claude/form-tx-heldout-portals` at `36be56dcd` (answer key sha256
`948f695a...7227`). The pre-registration was pushed at `8ea2e5602`, before the
portals were merged into this branch at `eeb53cadb`; deviations D1 and D2 were
recorded before any scored case ran. Raw rows: `results/arm_B.jsonl`,
`results/arm_C.jsonl`, `results/arm_A.txt`. Counts: `summarize.py`.

Everything here is synthetic. The four portals and their carriers are made
up. The pack author (the form layer's author) read the portal source, which
shows how the traps work, and wrote each pack by hand from one dev case.

## Headline

On the one portal it could operate, the layer submitted 6 of 9 quotes, all
correct, and submitted no wrong value. It verified none of them: that portal
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
| Correct submissions (of 32) | ARM_A_CORRECT | 6 | 6 |
| Correct and `verified_done` | n/a | 0 | 0 |
| Wrong submitted | ARM_A_WRONG | 0 | 0 |
| - with `verified_done` (false proof) | n/a | 0 | 0 |
| - flagged after submit | n/a | 0 | 0 |
| - silent | ARM_A_WRONG | 0 | 0 |
| Unsubmitted | ARM_A_UNSUB | 26 | 26 |
| Trap cases stopped before submit (of 18) | 0 | 6 | 6 |
| - then submitted correctly | n/a | 4 | 4 |
| Clean cases with a false stop (of 14) | 0 | 3 (3 halts) | 3 (3 halts) |
| Fields `not_checked` | n/a | 134 | 134 |
| Cases ending `readback_incomplete` | n/a | 6 | 6 |
| Operator hand-backs: halts | 0 | 20 | 20 |
| Operator hand-backs: post-submit flags | 0 | 6 | 6 |
| Runs that raised an error | 0 | 8 (all cobalt) | 8 (all cobalt) |

The clean-case false-stop rate counts only clean cases the layer reached: all
3 harborline clean cases stopped. The 11 clean cases on the other portals
never got that far.

### Per portal, arm B

| Portal | Cases | Submitted | Correct | Wrong | Stops before submit | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| harborline (table layout, popup estimator) | 9 | 6 | 6 | 0 | 9 cases, 20 halts | 6 `readback_incomplete`, 3 `escalated_to_owner` |
| cobalt (single-page app, type-aheads) | 8 | 0 | 0 | 0 | 0 | 8 errors: `MissingAnchorError` on the first type-ahead |
| pinecrest (form in an iframe) | 8 | 0 | 0 | 0 | 0 | 8 `readback_refuted` with nothing submitted |
| tidewater (shadow DOM) | 7 | 0 | 0 | 0 | 0 | 7 `readback_refuted` with nothing submitted |

Arm C matched arm B case for case.

## Reject criteria

- **R1, "a receipt means the quote is right":** not killed, but not shown
  either. No receipt was issued, because nothing could be read back.
- **R2, "no wrong value goes through silently":** not killed. 0 wrong values
  submitted; but only 6 cases submitted at all.
- **R3, "it stops wrong values before submit":** 6 of 18 trap cases (33%)
  stopped before submit. That is below the 75% bar, so the claim may only
  state the fraction. The 12 unstopped trap cases were on portals where the
  layer submitted nothing.
- **R4, "it does not stop clean work":** **killed.** All 3 clean cases it
  reached stopped (100%, above the 25% bar).
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

POSTHOC_SECTION

## What this cannot show

Four synthetic portals written by one agent are not carrier portals. Nothing
here says how the layer behaves on real carriers, real sessions, CAPTCHAs or
real operators.
