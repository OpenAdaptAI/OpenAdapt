# Re-measure: the stop card on the frozen Immunization suite, with `void`

Written and committed before this re-measure ran. Blind run 1
(`benchmark/contract_autogen/results/RESULTS.md`, #1653) found one false stop
and one card defect. Both came from a look-alike record that the contract
could not leave out by status. Under `stop_card/PREREG_REVIEW.md`, the card
isn't used in run 3 until that is fixed and re-measured. This file fixes how.

## What changes

Only the contract, by one line:

    void:
    - status="entered-in-error"

FHIR R4's Immunization.status says entered-in-error means the record should
not have been made. A person adds the line from that definition, not from the
suite. The suite, its sha256 (`c091ab44…`), the card code, the cost model and
the harness are the same as in blind run 1.

## What is run

1. `void/cases.yaml`: 8 cases written for this change, with outcomes fixed
   before any run. They are run on live Medplum 5.2.8 twice: with the void
   line, and without it, to show what the line changes.
2. The frozen blind suite with `ensure_immunization_void.yaml`, through
   `contract_autogen/blind.py suite`, with stop cards and timed Undo or Fix,
   at least 5 timed runs per category.
3. `stop_card/model.py` on the result.

## What decides whether the card goes into run 3

All of these must hold. Otherwise the card stays out, and this file's
results say which one failed.

- **Silent passes on the frozen suite: 0.**
- **Category defects: 0.** A defect is any stop whose card offers Undo or
  Fix when the right answer was that the record is fine, or offers Fix or
  Undo where `PREREG_REVIEW.md` says it mustn't.
- **Failed read-backs: 0**, or each one is charged the 300 s fallback and
  reported.
- **Median person seconds per caught harm (×1) below 210.**
- **`void/cases.yaml` with the void line: 8 of 8 right**, and no case
  reported `found_existing` or `created_verified` against its expected
  outcome.

False stops are counted and reported. A false stop that stays is not a
defect by itself, but its card's category is judged under the second rule.
