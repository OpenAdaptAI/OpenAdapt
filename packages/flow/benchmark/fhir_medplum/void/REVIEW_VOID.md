# Adversarial review: `void` in `openadapt_flow/ensure_record.py`

The change touches the verifier, so it was reviewed before merge, on
2026-10-29. The reviewer was an independent agent given the commit
(`90c61a2ae`), the module and its tests, never the author's reasoning. It
was asked for every way the change could produce a silent pass, loosen an
earlier refusal, or raise where the verifier used to fail closed. It ran its
own scratch tests.

| # | Severity | Finding | Fix | Regression test |
|---|---|---|---|---|
| 1 | Silent pass (old, not a regression) | A record that was the one match at baseline and turned `entered-in-error` during the run was still `found_existing`, through `existing_now` | The found path rejects a void `existing_now` | `test_review_1_an_existing_record_voided_in_the_window_is_not_found` |
| 2 | Minor | Python `==` let `true` void a record holding `1`, and `0` void `false` | Values compare as JSON text | `test_review_2_void_values_compare_as_json_not_python_equality` |
| 3 | Minor | A mistyped path (a space, an unbalanced `[`) loaded and never matched | The path must be element names with optional `[name=value]` filters | `test_a_void_entry_must_be_a_path_and_a_json_value_off_identity` |
| 4 | Minor | A filtered path split at the `=` inside the filter and couldn't load | Split at the first `=` outside a filter | `test_review_4_a_void_path_may_filter_a_list` |
| 5 | Minor | A list value never matched | The value must be one JSON scalar | the same parametrized test |
| 6 | Minor | A contract built without validation raised `ValueError` past `verify_ensure` | A bad entry raises `Unreadable`, which fails closed | `test_review_6_an_unvalidated_bad_entry_reads_as_unreadable` |
| — | Minor (from item 7 of the safe list) | The identity overlap check compared exact strings, so `telecom.value` slipped past `telecom[system=email].value` | Paths compare with filters dropped | the same parametrized test |

Checked and found safe by the reviewer:
- an agent or another user hiding a duplicate with a void value, since each route already sends the run to review;
- baseline and after both use `is_person`, so they agree;
- an overlap with checked fields only adds refusals;
- the closed-record check is unchanged;
- values containing `=` parse correctly;
- list paths are checked across every element;
- the heartbeat and late-write checks are unaffected.

One intended loosening: two records at baseline, one of them void, now give
`found_existing` for the other. That rests only on the void value, which the
contract's author takes from the resource's FHIR definition.
