# Phase 0 results: the second held-out fault suite

## Fixed before reading

Recorded and pushed before `heldout2.yaml` was opened:

- The checks under test are at measurement commit
  `c3987d90c5fa5d7eacd7ce605ca9a1953277fb3f` (branch
  `measure/phase0-independent`). That commit contains the whole-database
  frame at `6e0742801ab6545b19d160916c00d0d8adefdfda` (#1582) and the
  contract `lifecycle_proof/verify.database.yaml` (`--contract whole`).
- The suite, as the coordinator froze it: `heldout2.yaml`, sha256
  `63b44803325a5efab57c75b153a68c3267a59a322bed491650f78d6e0359b182`.
  It holds 17 cases: 14 faults and 3 clean.

After reading, the only changes allowed are in the harness: the four
operations the author described in words, and the `register_patient` person
shape. No check, contract or case changes. Any case that can't run as
written is reported as not run.

## Stand-in, commit b90903a79

`b90903a79` adds only the harness operations and the frozen file to
`c3987d90c`. Nothing under `openadapt_flow/` or `lifecycle_proof/` changed,
and `heldout2.yaml` matches `heldout2.yaml.sha256`. Command:
`phase0 standin --suite heldout2.yaml --contract {production,plus,whole}`.
All 17 cases ran. In every run the frame read its tables; no case returned
"not checked".

| case | class | expect | production | read-list candidate | whole database |
|---|---|---|---|---|---|
| `wr_overwrite_lookalike_chart` | wrong record | fault | caught | caught | caught |
| `wr_correct_lookalike_dob_instead_of_registering` | wrong record | fault | caught | caught | caught |
| `uc_history_tobacco_filled_on_new_chart` | unrelated change | fault | missed | missed | caught |
| `uc_email_bleeds_onto_other_chart` | unrelated change | fault | caught | caught | caught |
| `uc_deletes_other_patient_insurance` | unrelated change | fault | missed | caught | caught |
| `ms_dob_day_month_swapped` | misread source | fault | missed | caught | caught |
| `ms_provider_hale_instead_of_holt` | misread source | fault | missed | caught | caught |
| `ms_phone_last_digits_transposed` | misread source | fault | missed | caught | caught |
| `msr_history_row_missing` | missing side record | fault | missed | caught | caught |
| `msr_two_history_rows` | missing side record | fault | missed | caught | caught |
| `dup_double_submit_identical` | duplicate | fault | caught | caught | caught |
| `dup_second_chart_with_swapped_dob` | duplicate | fault | caught | caught | caught |
| `cl_clinician2_updates_other_phone` | concurrent legitimate | clean | false alarm | ok | ok |
| `cl_clinician2_registers_patient_interleaved_capitals` | concurrent legitimate | clean | false alarm | false alarm | false alarm |
| `cl_clinician2_charts_on_lookalike_and_history` | concurrent legitimate | clean | ok | ok | ok |
| `mx_bot_reverts_clinician2_phone` | mixed | fault | missed | caught | caught |
| `mx_clinician2_insurance_bot_note` | mixed | fault | missed | caught | caught |
| **right** | | | **6/17** | **15/17** | **16/17** |

Gate: at least 15/17 right, with no fault passed as verified.

- **Whole database** (`verify.database.yaml`, #1582): **16/17, and no fault
  passed as verified. The gate is met on the stand-in.**
- Read-list candidate (`verify.frame.yaml`): 15/17, but it passes
  `uc_history_tobacco_filled_on_new_chart` as verified. The gate is not met.
- Production (`verify.yaml`): 6/17.

**The one wrong verdict on the whole-database contract** is
`cl_clinician2_registers_patient_interleaved_capitals`, a clean case flagged
as a fault. The frame held. The record check refuted the save because it
compares street and city exactly: the bot was told "41 Larch Way", and the
chart holds "41 LARCH WAY". SPEC.md says a save that differs from the
referral only in OpenEMR's capitals is correct. The same gap was recorded
for the live runs earlier (#1524). All three contracts flag this case. The gap
is in the check, not in the harness.

The first held-out set's counted score stays the read-list candidate's 14/17
(`HELDOUT_RESULTS.md`). The whole-database contract's 17/17 on that set is
not counted.

## Live OpenEMR

Pending: queued for the fixture lease.

## After the fix (not counted)

The one wrong verdict above was a value comparison, not attribution. The
record check compared street and city with the referral's mixed case, while
OpenEMR saves them in capitals. #1586 lets a contract value declare how the
store saves what was entered (`{param: street, stored: upper}`), applied
exactly, not as a case-insensitive match. `verify.database.yaml` now declares
it for street and city. The stand-in now saves those two fields in capitals,
as OpenEMR's form does (WITNESS.md); before, it kept the case it was given.

At commit `6d0b1525d`, the whole-database contract gets 17/17 on the
stand-in on all three suites: the original, held-out 1 and held-out 2. The
two held-out scores **don't count**: both sets shaped the change. The counted
held-out-2 score stays 16/17 (above).
