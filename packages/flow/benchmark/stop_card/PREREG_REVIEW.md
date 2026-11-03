# Pre-registration: what a one-tap stop card costs a person

Committed before the card is built or any step is timed. Nothing here
changes after the first timed card; a later change is added at the end as a
dated deviation. This is for fair test run 3 and later. It changes nothing
in runs 1 or 2: their costs (`incremental_value/costs_2.json`) are frozen.

## Why

Run 1 charged every caught harm 420 person-seconds under cost model V1:
stop 90, reconcile 120, redo 180, interrupt 30. Nobody was timed. Those 420 s
are what lost run 1 on person time. The 90 s stop and the 120 s reconcile
pay for a person working out what the verifier found. The 180 s redo pays for
fixing it by hand. A card that shows the exact difference, with one tap to
act on it, removes most of those three steps.

## The card

One card per stopped write. It shows, from the verifier's own reads:

- **The difference,** one row per finding:
  - a wrong field: the field, the value the task gave, the value the record
    holds;
  - an element the task didn't give: its path and value;
  - a second matching record: its id, and who wrote it and when;
  - a write outside the task: the resource, the action (create, update or
    delete), who wrote it and when;
  - a change by someone else: who and when.
- **Who else wrote it.** Every write in the window, with its actor from the
  audit trail. The agent's own writes are marked.
- **Actions,** each offered only when it applies:
  - **Approve:** the record is right as it stands. The approval is recorded
    with the person's key.
  - **Undo:** reverses the agent's own writes in the window. A created
    resource is deleted. An updated or deleted one is put back at its version
    before the window. Offered only when nobody else wrote those resources
    in the window. Afterwards it reads them again, and a failed read-back
    says so on the card.
  - **Fix:** writes the task's values into the record the agent created,
    and removes the elements the task didn't give. Offered only when every
    finding is a field-level difference on that one record, and nobody else
    wrote it. Afterwards the record is checked against the same contract,
    with the same rules, and a failed check says so on the card.
  - **Open in the app:** the fallback. The person reconciles by hand.

The card never loosens a check. Approve records a person's decision. It
doesn't turn a stop into a pass for the verifier's statistics: it is
counted as a stop the person resolved.

## How its cost is modelled

Person time per stop is the sum of timed or published steps. No step is
fitted to the outcome.

| Step | Seconds | Source |
|---|---:|---|
| Interrupt (switch to the card and back) | 30 | V1, unchanged |
| Open the card: point and click | 1.2 | KLM: P 1.1 + B 0.1 |
| Read each row | words ÷ 238 × 60 | Brysbaert 2019, silent reading of English non-fiction (238 wpm) |
| Compare each row | 1.35 | KLM: M |
| Decide | 1.35 | KLM: M |
| Act: point and click | 1.2 | KLM: P 1.1 + B 0.1 |
| Wait for Undo or Fix and its read-back | measured | wall time on the live fixture, median of the timed runs, counted in full |
| Open in the app (no action applies) | 300 | V1 reconcile 120 + redo 180, unchanged |

KLM is the keystroke-level model (Card, Moran and Newell, *The Psychology
of Human-Computer Interaction*, 1983). Its operators assume a practised,
error-free user. Two choices go against the card to cover that: every row
is compared (M), not only skimmed; and the machine wait counts in full,
even though a person could switch away.

Words per row are counted from the rendered card text, values included.

**Sensitivity.** Every person step except the interrupt is reported at ×0.5,
×1 and ×2. The verdict uses ×1.

## What is measured, and on what

1. **Timed machine steps.** Undo, Fix and their read-backs are timed on live
   Medplum 5.2.8 (the #1640 fixture), on the stops of the blind suite that
   `contract_autogen/BLIND.md` runs. The report gives each step's median and
   p90 over at least 5 runs.
2. **Which action applies.** Each stop gets exactly one category: approve,
   undo, fix, or open in the app. The rules are fixed above. The category is
   decided by the card code, not by hand.
3. **Person seconds per stop.** That comes from the table, by category,
   with the rendered word counts.
4. **Run 1, retrospectively.** The same model is applied to run 1's B stops
   by class (wrong value, collateral, side record, mixed, misleading
   referral, duplicate, false stops), using what the card would show for
   each class. This is labelled retrospective and doesn't change run 1's
   verdict.

## What would count against the card

- **Fix or Undo with a failed read-back.** That stop costs the full 300 s
  fallback on top of the card steps, and every such case is reported.
- **A category the card gets wrong.** For example, it offers Fix when
  someone else wrote the record, or Undo when that would remove another
  user's write. Each one is reported as a defect, and the card isn't used in
  run 3 until it's fixed and re-measured.
- **The result.** If the median person seconds per caught harm under ×1 is
  not below 210 (half of V1's 420), this report says the card did not halve
  review cost.

## Not claimed

These are modelled seconds from published operators and timed machine
steps, not a timed person. Run 3's own pre-registration decides whether to
use this model; this file only fixes how the model is built and measured.

## Deviations

1. **2026-10-29, before any card was built or timed: what Undo leaves
   undone.** Undo keeps the agent's created record only if the card's check
   of that record passes: right values, closed, and the only match. When
   that holds, the task stands done after Undo. When Undo removes the
   created record as well, the task is not done, and the stop is also
   charged V1's redo of 180 s, scaled with the other person steps. The
   original text didn't say what an Undo that removes the task's own record
   costs.
2. **2026-10-29, before any card was built or timed: when Undo and Fix are
   offered, after an adversarial review (`contract_autogen/REVIEW.md`).**
   Neither is offered when anyone else wrote anything in the window. Each
   refuses at the tap if a record it would write changed, or anyone else
   wrote, since the card was built. That refusal counts as a failed
   read-back, so the fallback is charged. Fix is offered only when every
   extra element is in one place. Only HTTP 404 or 410 confirms a delete.
   Undo keeps security labels and compares them on read-back. Each change
   makes the card offer fewer one-tap actions, never more.
