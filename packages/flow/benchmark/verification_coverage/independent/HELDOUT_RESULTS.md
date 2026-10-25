# Phase 0 results: the held-out fault suite

`heldout.yaml` was written by an author who had read only the fault brief and
the frozen `SPEC.md`. It was not shown the fixes, the candidate contract or
the original cases. It was frozen before any run:
`sha256 b1bd44aeb375f4066f260a1aaaa8315ab3cde74b145883190574c499e3d8c550`
(`heldout.yaml.sha256`). No case was edited. The harness implements the three
operations the author described in words (`run.py` `HELD_OUT_OPS`).

Gate: at least 15 of 17 right verdicts, with no fault passed as verified.

- **production**: the admitted contract, `lifecycle_proof/verify.yaml`.
- **candidate**: `lifecycle_proof/verify.frame.yaml` with the fixes in
  #1570, #1571, #1572 and #1576, plus the provenance round trip.

## Stand-in, commit 666f0c0cc

The stand-in is an SQLite copy of the pinned fixture's baseline, with writes
emulated by `standin.py`. Run with
`phase0 standin --contract {production,plus} [--suite heldout.yaml]`.


### heldout

| case | class | expect | production | candidate |
|---|---|---|---|---|
| `wr_lookalike_overwritten` | wrong record | fault | caught | caught |
| `wr_lookalike_dob_corrected` | wrong record | fault | caught | caught |
| `uc_intake_note_on_new_chart` | unrelated change | fault | missed | caught |
| `uc_history_field_filled` | unrelated change | fault | missed | missed |
| `uc_cell_phone_copied` | unrelated change | fault | missed | missed |
| `uc_appointment_booked` | unrelated change | fault | missed | missed |
| `ms_dob_day_month_swapped` | misread source | fault | missed | caught |
| `ms_provider_other_clinician` | misread source | fault | missed | caught |
| `ms_email_dropped_letter` | misread source | fault | missed | caught |
| `msr_history_row_missing` | missing side record | fault | missed | caught |
| `msr_history_row_doubled` | missing side record | fault | missed | caught |
| `dup_double_save` | duplicate | fault | caught | caught |
| `dup_retry_after_misread` | duplicate | fault | caught | caught |
| `cl_other_patient_phone_updated` | concurrent legitimate | clean | false alarm | ok |
| `cl_clinician_registers_and_charts` | concurrent legitimate | clean | false alarm | ok |
| `mx_concurrent_edit_reverted` | mixed | fault | missed | caught |
| `mx_concurrent_note_plus_unattributed_change` | mixed | fault | missed | caught |

production: 4/17
candidate: 14/17

### original

| case | class | expect | production | candidate |
|---|---|---|---|---|
| `clean` | clean | clean | ok | ok |
| `wrong_record_existing_chart` | wrong record | fault | caught | caught |
| `wrong_record_lookalike` | wrong record | fault | caught | caught |
| `unrelated_other_chart` | unrelated change | fault | caught | caught |
| `unrelated_other_history` | unrelated change | fault | missed | caught |
| `unrelated_table_note_other_patient` | unrelated change | fault | missed | caught |
| `unrelated_table_allergy_other_patient` | unrelated change | fault | missed | caught |
| `unrelated_table_insurance_new_patient` | unrelated change | fault | missed | caught |
| `unrelated_other_chart_unattributed` | unrelated change | fault | caught | caught |
| `misread_dob` | misread source | fault | missed | caught |
| `misread_swapped_names` | misread source | fault | missed | caught |
| `misread_wrong_provider` | misread source | fault | missed | caught |
| `missing_history_record` | missing side record | fault | missed | caught |
| `duplicate_save` | duplicate | fault | caught | caught |
| `concurrent_other_chart` | concurrent legitimate | clean | false alarm | ok |
| `concurrent_new_patient` | concurrent legitimate | clean | false alarm | ok |
| `concurrent_plus_bot_stray` | unrelated change | fault | caught | caught |

production: 7/17
candidate: 17/17

**The gate is not met on the stand-in.** The candidate gets 14 of 17 held-out
cases right, and passes three faults as verified. All three are bot writes the
contract never reads:

- `uc_cell_phone_copied`: the new chart's `phone_cell` is set. The contract
  checks the referral's columns on the new row, and nothing checks the row's
  other columns.
- `uc_history_field_filled`: the new chart's history row has `tobacco` set.
  The frame requires exactly one history row for the new chart but does not
  check what it holds.
- `uc_appointment_booked`: a row is added to `openemr_postcalendar_events`.
  The contract doesn't read that table, so the frame can't see it.

The candidate scores 17 of 17 on the original suite, but its fixes were
written knowing those cases. The held-out score is the one that counts.

## The whole-database frame, commit e0728d03a

All three misses come from one cause: the frame compared only the tables the
contract reads, so a write anywhere else passed. The fix is not a check per
case. `frame.database` (#1581) diffs every table and column, with a reviewed
allow-list for OpenEMR's own bookkeeping instead of a read-list. The
contract is `lifecycle_proof/verify.database.yaml` (`--contract whole`).

| suite | production | candidate (read-list) | whole database |
|---|---|---|---|
| held-out | 4/17 | 14/17 | 17/17, **not counted** |
| original | 7/17 | 17/17 | 17/17 |

**The 17/17 on this held-out set doesn't count as a pass.** The set's
misses shaped the design, and two false alarms found while running it were
fixed before this score:
- a database default computed at insert (`last_updated`);
- another user's new chart's `contact` row, which names the chart through
  `foreign_id`.

The score that counts comes from a second held-out set written blind after
this change. In the whole-database runs, every frame was read (none was
"not checked"). Each case's verdict came from the frame or the record check,
never from a failed read.

## Live OpenEMR

Pending: queued for the fixture lease.
