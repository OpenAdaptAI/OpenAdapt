# Results: the reworked form layer on two new held-out quote portals

Run 2026-11-21 on the zenbook lab (WSL, headless Chromium), once per arm, in
the pre-registered order. Engine `13772e2b6` (`ENGINE_SHA`, re-pinned under
D1 before any scored case). Portals and answer key from
`claude/form-tx-heldout3-portals` at `177378a6e` (answer key sha256
`c4938154...c4d1`, checked after the merge). The packs were written by two
fresh agents from each portal's dev case only, and committed at `85606e0f0`
before the first scored case. Raw rows are in `results/arm_B.jsonl`,
`results/arm_A.jsonl` and the full logs. Counts:
`../form_tx_heldout_run/summarize.py --suite=heldout3`.

Everything here is synthetic.

## Headline

**Verdict: rework.**

- **Receipt claim killed.** One wrong quote got a receipt (`cv-s04`).
- **Stops before submit.** The layer stopped 8 of 9 trap cases before
  submit (K1 met), but only one of those then ended in a correct quote.
- **False stops on clean cases.** It stopped 3 of 7 clean cases (K2 not
  met).
- **Submitted quotes.** It submitted 7 quotes: 5 right and 2 wrong.
  - 4 of the 7 ended `verified_done` with a receipt, so K3 is met on the
    registered definition. One of those 4 is the wrong one.
  - Counting correct quotes only, 3 of 7 ended verified with a receipt.
- **Unguarded filler (arm A).** It got 10 of 16 wrong, silently.

The n is small: 16 cases in all, and one case moves K2 by 14 points.

## Kill or rework criteria (arm B)

| | Criterion | Bar | Result | |
| --- | --- | --- | --- | --- |
| K1 | Trap cases stopped before submit | at least 75% | 8 of 9 (89%) | met |
| K2 | Clean cases with a false stop | at most 10% | 3 of 7 (43%) | **not met** |
| K3 | Submitted quotes `verified_done` with a receipt | at least 50% | 4 of 7 (57%) | met, but one is a false receipt |
| K4 | Wrong values submitted | zero | 2 cases | **not met** |

- **K1 companion.** Of the 8 stopped trap cases, 1 then ended correct
  (`cv-s07`: two prefill stops answered, then a sign-in mid-quote). The
  other 7 ended with the scripted operator escalating. No portal stopped on
  every case.
- **Kill lines:**
  - **False receipt.** `cv-s04` was submitted with an empty "Breed of each
    dog" and ended `verified_done`. The receipt claim is killed.
  - **Silent wrong.** None. The other wrong quote, `pb-s04` (horsepower
    stored as 800, intake 400), was `readback_refuted`, and the flag named
    the wrong field.
- **Reached-only false stops:** all 7 clean cases were reached, so the
  figure is the same, 43%.

## Per portal (arm B)

| Portal | Cases | Submitted | Correct | Wrong | Verified with receipt | Trap stopped | Clean false stops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| corvane | 8 | 4 | 3 | 1 | 4 (1 wrong) | 4 of 4 | 2 of 4 |
| pellbrook | 8 | 3 | 2 | 1 | 0 | 4 of 5 | 1 of 3 |
| Pooled | 16 | 7 | 5 | 2 | 4 | 8 of 9 | 3 of 7 |

Stops (13 in all): validation 5, prefill conflict 4, ambiguous option 2,
ungrounded required field 1, sign-in wall 1. Pellbrook's 2 correct quotes
ended `readback_incomplete`, because the pack had no read-back keyed to the
lienholder it typed.

Arm A (`naive_filler.py`, run once unchanged): 6 correct, 10 wrong submitted
with no flag, 0 unsubmitted.

## What went wrong, by cause

1. **A bound question shown with no intake value, left empty, verifies**
   (`cv-s04`, the false receipt).
   - The dog questions appear only when dogs are kept. The intake said dogs
     were kept, but it gave no breeds, which is an answer the operator
     should supply.
   - The pack bound the breed field. With no fact and no `required`
     marker, the layer left it empty and submitted.
   - The summary showed a dash, which the pack declares blank, so read-back
     had nothing to compare. The run ended `verified_done`.
   - The rework's unbound-control checks do not reach this, because the
     control is bound.
   - Needed: a bound field shown with no intake value and left empty must
     either stop or be `not_checked`, unless the pack marks it optional.
2. **Validation stops on a one-question-at-a-time form still carry no
   label** (`pb-s02`, `pb-s05`, `pb-s08`). "Please answer this question to
   continue." is shown away from the control, and the control does not
   change, so none of the new rules find it. The operator escalated each
   time.
3. **Type-ahead options must match a fact exactly** (`cv-s05`, `cv-s06`,
   both false stops). The pack built the mortgagee entry from a single dev
   sample. On scored cases the directory text differed, so no option
   matched.
4. **Other false stops:**
   - `cv-s03`: a dwelling limit with no intake value; the estimator was not
     in the pack.
   - `pb-s06`: an agreed hull value missing from the intake, where the
     book-value lookup was not in the pack.
5. **A wrong summary value with no stop before submit** (`pb-s04`). The
   portal showed and stored horsepower doubled. Read-back caught it after
   submit (`readback_refuted`), naming the field. Nothing before submit
   re-reads a value the portal derives.

## What changed since the second run

The rework's targeted classes, as seen here:

- **Sign-in mid-quote.** After the sign-in, `cv-s07` went on to submit
  correctly and verified. The rows do not record whether a re-walk ran.
- **Radio and validation stops name their field.** Every corvane stop
  carried its field's label. The 3 pellbrook stops are the exception
  above.
- **Unbound free text and unused intake values.** These raised no false
  receipt here.

Two classes did not appear on these portals and are not measured here: the
row-picked "Select" and the date in three boxes.

## What this cannot show

Two synthetic portals by one author, 16 cases. Nothing here says how the
layer behaves on real carriers, real sessions or real operators. Pack
quality is part of what is measured: every pack was written from one dev
case.
