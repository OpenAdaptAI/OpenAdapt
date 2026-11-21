# Results: the form layer after its gap fixes, on four new held-out quote portals

Run 2026-11-21 on the zenbook lab (WSL, headless Chromium), once per arm, in
the pre-registered order. Engine `72a6ad428` (`ENGINE_SHA`; deviation D1
records the re-pin from `335c322ec`, made before any scored case). Portals
and answer key from `claude/form-tx-heldout2-portals` at `d2d688337`
(answer key sha256 `46df94bf...0f90`). Packs were written by separate agents
from each portal's dev case only and frozen at `7a2abea0c`, before the first
scored case; `git diff 72a6ad428 7a2abea0c -- packages/flow/openadapt_flow`
is empty. Raw rows: `results/arm_B.jsonl`, `results/arm_A.json` and the full
logs. Counts: `../form_tx_heldout_run/summarize.py --suite=heldout2`.

Everything here is synthetic. The four portals and their carriers are made
up; a web search for each carrier name found no insurer of that name.

## Headline

**Verdict: rework.** On the new portals the layer stopped 15 of 18 trap
cases before submit, but none of those stops ended in a correct quote. It
also stopped 10 of 15 clean cases. It submitted 11 quotes: 6 right, 5 wrong.
All 5 wrong quotes were flagged after submit, and none got a receipt. Only 1
submitted quote ended verified with a receipt. The unguarded filler got 21 of
33 wrong, silently.

On the old four portals, now a dev set, the same engine got 20 of 32 right,
10 of them verified. It also issued **2 receipts for wrong quotes**
(cobalt, `cr-s02` and `cr-s07`). In both, the wrong field was a list of
additional drivers that the pack never bound, so the read-back never looked
at it. A receipt covers only the fields the pack binds, not the whole record.

## Kill or rework criteria (new portals, arm B)

| | Criterion | Bar | Result | |
| --- | --- | --- | --- | --- |
| K1 | Trap cases stopped before submit | at least 75% | 15 of 18 (83%) | met |
| K2 | Clean cases with a false stop | at most 10% | 10 of 15 (67%) | **not met** |
| K3 | Submitted quotes `verified_done` with a receipt | at least 50% | 1 of 11 (9%) | **not met** |
| K4 | Wrong values submitted | zero | 5 cases | **not met** |

- **K1 companion (registered under D1):** 0 of the 15 stopped trap cases then
  ended with a correct quote. 12 ended with the scripted operator escalating,
  because no script entry matched the stop (see below); 3 were submitted
  wrong after the operator signed in again (cause 1). No portal stopped on every case. K1 is
  met only by stops that a person then had to finish.
- **Reached-only false stops:** all 15 clean cases were reached, so the
  figure is the same, 67%.
- **Kill lines:** no wrong quote got a receipt on the new portals, and no
  wrong quote ended without a flag. On two of the five, though
  (`fc-s02`, `gw-s04`), the flag (`readback_incomplete`) named other fields
  and not the wrong one: the wrong field was a control the pack never bound.
  On the dev set the receipt claim is killed (two false receipts, above).
- **Popup rule:** no new-portal case was held below verified by popup inputs
  alone; on the dev set, 2 were.

## Metrics, old and new portals (arm B)

| Metric | First run, old portals (frozen engine `6392bac91`) | This engine, old portals (dev set) | This engine, new portals (held out) |
| --- | ---: | ---: | ---: |
| Scored cases (trap / clean) | 32 (18 / 14) | 32 (18 / 14) | 33 (18 / 15) |
| Correct submissions | 6 | 20 | 6 |
| Correct and `verified_done` with a receipt | 0 | 10 | 1 |
| Submitted | 6 | 25 | 11 |
| Submitted that ended `verified_done` with a receipt | 0 | 12 | 1 |
| Wrong submitted | 0 | 5 | 5 |
| - with a receipt (false proof) | 0 | **2** | 0 |
| - flagged after submit | 0 | 3 | 5 |
| - silent | 0 | 0 | 0 |
| Trap cases stopped before submit | 6 (33%) | 16 (89%) | 15 (83%) |
| - then submitted correctly | 4 | 9 | 0 |
| Clean cases with a false stop | 3 (21%) | 2 (14%) | 10 (67%) |
| Unsubmitted | 26 | 7 | 22 |
| Fields `not_checked` | 134 | 29 | 35 |
| Cases ending `readback_incomplete` | 6 | 9 | 6 |
| Operator hand-backs: stops / post-submit flags | 20 / 6 | 30 / 13 | 27 / 10 |
| Runs that raised an error | 8 | 0 | 0 |

The first-run column is copied from `../form_tx_heldout_run/RESULTS.md`; it
used the operator's by-hand estimator (its D2), which this run dropped.

Arm A (the portal author's unguarded filler, re-run once unchanged) on the
new portals: 12 correct, 21 wrong submitted with no flag, 0 unsubmitted; 3
right and 5 or 6 wrong on every portal.

### Per portal, new portals, arm B

| Portal | Cases | Submitted | Correct | Wrong | Verified with receipt | Trap stopped | Clean false stops | `not_checked` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ferncastle (tabbed producer desk, landlord) | 8 | 2 | 0 | 2 | 0 | 5 of 5 | 2 of 3 | 2 |
| glasswing (embedded renters widget) | 8 | 3 | 2 | 1 | 0 | 3 of 4 | 2 of 4 | 12 |
| okelund (frameset agency system, auto) | 8 | 3 | 2 | 1 | 1 | 3 of 4 | 3 of 4 | 0 |
| velloway (accessible one-page umbrella app) | 9 | 3 | 2 | 1 | 0 | 4 of 5 | 3 of 4 | 21 |

Stops by kind (27): prefill conflict 12, validation error 9, popup 3, sign-in
wall 3. The 22 unsubmitted cases all ended with the operator escalating.

## What went wrong, by cause

1. **A sign-in in the middle loses earlier answers.** In every case where
   the session expired mid-quote and the operator signed in (`fc-s04`,
   `ok-s03`, `vw-s06`; on the dev set `hl-s05`, `tw-s03`), the portal
   reset fields the layer had already filled or that the operator had
   answered, the layer resumed without re-checking them, and it submitted
   the reset values. The read-back caught all five (`readback_refuted`).
   After a sign-in, the layer must re-verify every field it already wrote.
2. **Fields the pack does not bind are not checked at all.** `fc-s02`
   (a client number left empty) and `gw-s04` (a portal-prefilled roommate
   kept) were submitted wrong, and the dev-set false receipts were an
   unbound driver list. A visible control holding a value that no binding
   covers needs a stop or at least a `not_checked` verdict that names it.
3. **Stops a person cannot act on.** 9 validation stops carried no field
   label ("did not advance after clicking Next"), and radio-group conflicts
   carried the option label ("$1,000,000", "$500") instead of the group's
   name, so the scripted operator found no entry and escalated. Of the 10
   false stops, 6 were prefill conflicts on portal defaults (umbrella limit,
   deductible, liability) where the clean intake asked for the default's
   alternative or the pack could not tell a default from a prefill; 2 were
   the okelund lookup list (cause 5) and 2 were validation stops on
   questions the pack did not bind.
4. **Verification is rare.** Read-back reached `verified_done` once on the
   new portals. Blockers seen on the dev cases and confirmed here: a date
   typed in three boxes but shown as one value (velloway, every case), a
   never-stored confirmation tick box (velloway), a calculator result shown
   as an unlabelled "Label: value" output (glasswing), a field the summary
   does not show (ferncastle units rented), and popup inputs under the
   founder's rule.
5. **A lookup list with several "Select" buttons** stops every time (okelund,
   3 cases): the layer cannot pick the row that matches the intake.

## What changed since the first run

On the old portals, which the fixes were written against, the layer went from
1 operable portal to 4, from 6 correct quotes to 20, from 0 verified to 10,
and from 8 crashes to none. On portals it had not seen, operability held up
(every case reached its form) but correctness and verification did not.

## What this cannot show

Four synthetic portals by one agent are not carrier portals. Nothing here
says how the layer behaves on real carriers, real sessions, CAPTCHAs or real
operators. Pack quality is part of what is measured: every pack was written
from one dev case.
