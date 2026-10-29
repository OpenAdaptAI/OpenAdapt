# Adversarial review of the FHIR outcome-entry fix

The fix changes the verifier (`runtime/effects/fhir.py`), so a separate
agent reviewed it on 2026-10-29. It had the diff and the three-layer review
rule: a finding blocks only if it can report a wrong write as verified, miss
a duplicate, or falsely hold a realistic save. It worked read-only and
contacted no server.

**Verdict: pass, no blocking finding.** The dropped outcome entry has no id
and no field values, so it could never match an expected record or count as
a new one. Before the fix it was already left out of the match count and the
`total` check. An error severity, a missing severity, or a malformed `issue`
list fails closed (unreadable), on any page.

| # | Finding (non-blocking) | Action |
| --- | --- | --- |
| 1 | An OperationOutcome entry without `search.mode` was still counted as a record: with no `total`, the same false "removed record" hold | fixed: with no mode, an OperationOutcome is an outcome; test `test_fhir_an_outcome_without_a_mode_is_not_a_record` |
| 2 | A warning that the results are partial (records filtered for security) is skipped, so a hidden duplicate isn't seen | not changed. The old code missed it too, and Epic's sandbox sends only "no results". Follow-up: a list of warning codes that mean partial |
| 3 | `ensure_record`'s reader dropped outcome entries without checking severity: a 200 search with an error outcome and no `total` read as "no match", so an ensure could create a duplicate | fixed in `ensure_record.py`: an error outcome is unreadable; tests `error-outcome` and `test_a_warning_outcome_is_a_message_not_an_unreadable_search` |
| 4 | Record-check drafting (`record_check_synthesis.py`) takes outcome entries as records, so an Epic empty search drafts `resource_type: OperationOutcome` | not changed: drafts only, never verdicts. Follow-up |
| 5 | Tests didn't cover an outcome beside matches and a `total`, or on page two | fixed: `test_fhir_an_outcome_beside_matches_on_a_later_page` |
