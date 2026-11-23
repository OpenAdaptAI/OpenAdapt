# Results: the reworked form layer on two new held-out quote portals

Run 2026-11-21 on the zenbook lab (WSL, headless Chromium), once per arm, in
the pre-registered order. Engine `13772e2b6` (`ENGINE_SHA`, re-pinned under
D1 before any scored case). Portals and answer key from
`claude/form-tx-heldout3-portals` at `177378a6e` (answer key sha256
`c4938154...c4d1`, checked after the merge). The packs were written by two
fresh agents from each portal's dev case only, and committed at `85606e0f0`
before the first scored case. Raw rows are in `results/arm_B.jsonl`,
`results/arm_A.jsonl` and the full logs. Counts:
`../form_tx_heldout_run/summarize.py --suite=heldout3`. Freeze check: `git
diff 13772e2b6 85606e0f0 -- packages/flow/openadapt_flow` and `git diff
85606e0f0 a1f024d37 -- packages/flow/benchmark/form_tx_heldout_run/packs3`
are both empty.

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

Each cause below is checked against the answer key's trap labels and
operator script, read after the scored run. Where the mechanism is inferred
from the rows and has not been confirmed by a diagnostic run, it says so.

1. **A bound question shown with no intake value, left empty, verifies**
   (`cv-s04`, `needs_operator_answer`, trap `unknown_question`; the false
   receipt).
   - The intake says dogs are kept but gives no breeds. The script expects
     the operator to supply "Breed of each dog".
   - The layer stopped on the bite-history question (validation, named). The
     operator answered it, and the run submitted with breeds empty.
   - Inferred mechanism, not confirmed by a diagnostic run: the pack binds
     the breed field, so the unbound-control checks skip it. With no fact
     and no `required` marker, no stop fires. The summary shows a dash,
     which the pack declares blank, so read-back has nothing to compare.
2. **Validation stops on the one-question-at-a-time form carry no label**
   (`pb-s02`, `pb-s05`, `pb-s08`, all `needs_operator_answer` on "Lay-up
   period").
   - Inferred: the message "Please answer this question to continue." is not
     attached to the control. The control is the same one for every
     question, so neither a banner-to-label match nor row growth finds it.
   - The script's label never matched, and the operator escalated.
3. **A type-ahead option that does not exactly equal the derived fact.**
   - `cv-s05` (clean, trap `typeahead_ambiguous`) is a false stop: "No option
     matched" on the mortgagee. The careful human picks the right one of
     several similar options. The pack builds the expected entry from one
     dev sample.
   - `cv-s06` (`must_stop_before_submit`, trap `estimator_wrong`) also
     stopped on the mortgagee, before reaching the estimator, and escalated.
     It counts toward K1 but not for the trap's reason.
4. **Missing popups in the packs** (false stops on clean cases with
   `estimator_ok`). `cv-s03` stopped on a dwelling limit the intake leaves
   to the estimator, and `pb-s06` on an agreed hull value left to the
   book-value lookup. Neither pack declares that popup, because the dev
   case never opened it.
5. **A summary that differs from what was entered, with no stop before
   submit** (`pb-s04`, trap `summary_differs`). The stored horsepower was
   800 against an intake of 400. A careful human catches it on the review
   page. The layer does not compare the review page before submit, so it
   was caught only after submit (`readback_refuted`, naming the field).
6. **Stops on the wrong field.** `cv-s02` (trap `prefill_conflict` on the
   co-applicant) stopped first on a phone prefill, and `pb-s07` (trap on the
   lienholder) stopped first on a model-year prefill. The script had no entry
   for either, so both escalated. They count toward K1.

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
