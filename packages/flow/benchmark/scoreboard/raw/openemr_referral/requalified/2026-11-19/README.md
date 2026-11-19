# The OpenEMR referral program, qualified again (19 November 2026)

The certified referral program (`ec096afa…`, certified 20 October) halted
every referral from 1 November at step_009: its date-picker check expected
the literal `October-2026-`. This is the same program with that one check
compiled against the run's clock (`text_clock: "{Month}-{YYYY}-"`, by
`python -m openadapt_flow.compiler.clock_pass … --recorded-on 2026-10-20`),
qualified and certified again on the local OpenEMR 8.0.0.3 fixture. Every
patient is made up.

- Bundle: `benchmark/openemr_local/referral_program/program/`, content digest
  `10e4594523362a0c79a8a533742a71b5b73178d32378c7bc934b51a2b006bc1c`.
  Re-saving it under compiler 3.2.0 also writes two additive fields as `null`
  (`Anchor.form_identity`, `TokenTemplate.bearing`); nothing else changed.
- Command: `python -m scripts.openemr_qualification --bundle-src <passed
  bundle> --out <dir> --fixture-state … --lock … --ui-url … --api-url …`
  (`clinical-write`, 3 trials per case, SQL second oracle on).
- Result: `CERTIFIED under clinical-write`; `qualification-result/v1
  9a629d28509bd6eb: passed=True silent_incorrect_success=0 over_halt=0
  executor_model_calls=0 verifier_model_calls=0`.

## Case by case against 20 October

All 18 qualification trials stopped where, and for the reason, they did in
October (`qualify.log` here; October's in the qualify-referral scratch log):

| Case | 20 Oct | 19 Nov |
| --- | --- | --- |
| representative ×3 | VERIFIED | VERIFIED |
| fault-ambiguity ×3 | HALTED_BEFORE_EFFECT at step_040 (OCR safety refusal) | same |
| fault-wrong-identity ×3 | REJECTED_POLICY at step_040 (identity check failed) | same |
| fault-stale-identity ×3 | HALTED_BEFORE_EFFECT at step_040 (target not resolved) | same |
| fault-weak-effect ×3 | HALTED_BEFORE_EFFECT at step_040 (verifier strength changed) | same |
| fault-missing-effect ×3 | HALTED_BEFORE_EFFECT at step_040 (no EffectVerifier) | same |
| rule-existing-duplicate ×3 | halted at step_039, no new or changed chart | halted at step_039 on "No matches were found.", no mutation |

## What it took on current main

Three more faults stood between the clock fix and a certification; PR #2280
fixes each:

1. The read-back (`verify.yaml`) could not establish completeness since
   #2041. Measured on this fixture: `_limit` is capped at 200, the body has
   no total, `links.first` is always present and `links.next` appears only
   when a record follows the page, and the next link's `_count` is ignored
   (following it returns every row). The recipe now reads `_limit=200` with
   `records_next_path: links.next` and `records_next_absent_is_last: true`:
   a 200-row page with no next link read complete; a `_limit=2` read of 5
   charts was refused (`multi-page reads require declared immutable snapshot
   semantics`).
2. The first-local delivery fence (#2209) refused step_039 (Create New
   Patient, reversible by the operator's confirmed classification, no
   effects) in every case: it ignored the qualification project's decision
   during a campaign, and in a certified run checked the certification
   without the admitted policy digest, so it never reproduced.
3. With business rules, `qualify --certify` failed its final save after the
   rule trials passed: saving the rule trials' copy moved the workflow's
   verified source to that copy (#1993), and the copy was then removed.

## The +6 uuid rows in the recorder's oracle

A recording on the lifecycle fixture showed `uuid_mapping +18` and
`uuid_registry +20` against the contract's +12 and +14. Grouped by owner, all
six extra rows on each table are FHIR `Group` and `Location` mappings for
three `users` rows (and each mapping's own registry row): OpenEMR maps every
account on the first patient save after a reset, and this fixture carries
three clerk accounts the pinned image's four built-ins don't have. The UI
arms' contract now adds two rows per account beyond the four, on each table;
with it, the recording passed its REST oracle and exact delta audit.

## The quickstart on this bundle

`tests/test_quickstart_openemr.py::test_real_openemr_quickstart` (passed)
(`OPENADAPT_REAL_QUICKSTART_OPENEMR=1`, headless, its own OpenEMR 8.0.0.3
stack), 19 November 2026, from this branch: runs 1 to 5 `VERIFIED` /
`VERIFIED` on bundle `10e45945…` with 0 model calls, and the possible
duplicate (Rosa Delgado born 1958-03-22 against the existing chart born
1958-03-27) `HALTED` / `HALTED_BEFORE_EFFECT` at step_039 with 0 new charts
in the database. The first attempt stopped before the fixture started: a new
machine had no quickstart state folder (fixed in the same PR). The test's
summary-line assertion still expected wording the quickstart no longer
prints; it now checks "5 of 5 saved and checked".
