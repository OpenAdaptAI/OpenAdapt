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

## Part 2: `void` together with the agent's own corrections (#1655)

#1655 merged into the same module while this change was in review: a create
followed by the same agent's updates of that record can count as one write.
The two interact, because a correction must leave the record the person in
every version, and `void` changes what "the person" means. A second
independent agent reviewed the merged code (`4f57e9ce9`), on 2026-10-30. It
was given both features' code, tests and this file, and asked for any silent
pass, crash or loosening where they meet. It found no silent pass on the
expected server behaviour, and three edges where the verifier failed open
or crashed instead of failing closed. All three are fixed, each with its own
test.

| # | Severity | Finding | Fix | Regression test |
|---|---|---|---|---|
| C1 | Safe | A version that goes into void, out of void, or into void and back is not a correction | none needed: `correction_refused` runs `is_person` on every version, and the final record is checked for void | `test_a_correction_out_of_void_is_not_a_correction`, `test_a_correction_into_void_needs_review` |
| C2 | Minor (needs two server faults; older than void) | A `_history` version with no `lastUpdated` was skipped, so a void middle version could escape the version count and the checks on each version, if the audit trail also missed that write | A version with no time is unreadable | `test_review_c2_a_version_with_no_time_is_unreadable` |
| C3 | Should-fix (older found-path gap) | When the read by id of the found record failed but a search still returned it, the run could be `found_existing` without the heartbeat or the void check on the record | A failed read of a record the search still finds is unreadable | `test_review_c3_a_found_record_that_cant_be_read_by_id_needs_review` |
| C4 | Minor (an unvalidated contract only) | An `Unreadable` raised inside `judge` (a bad void entry in a contract built without validation) escaped `verify_ensure` as an exception | `judge` runs inside the `try` that turns `Unreadable` into review | `test_review_c4_an_unreadable_void_entry_in_judge_fails_closed` |
| C5 | By design | A middle version with a status `void` doesn't name, such as `not-done`, can be corrected away | Documented in `docs/ENSURE_RECORD.md`: a correction's middle versions are filtered only by `void` | none |

Checked and found safe by the reviewer:
- the agent voiding another record that was the person, then creating its own;
- another actor voiding the found record during the run;
- a void duplicate hiding from the duplicate check (a second create, or any agent write to another record, is still review);
- another actor toggling a void look-alike during the run;
- the version count, the reads, the heartbeat and the late-write check;
- a void entry on a path a correction can change;
- the found path when the found record was corrected.

Each fix sends more runs to review, never fewer. The live re-measure was
run again on the merged code: see `results/RESULTS.md`.
