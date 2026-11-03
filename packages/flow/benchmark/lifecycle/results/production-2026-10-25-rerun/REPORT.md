# Lifecycle soak report

Protocol `lifecycle/2026-10-20.1`, workload `lifecycle-workload/2026-10-24.1`, seed 7, 55 referrals over 10 simulated days. Engine commit `38fc21afbfff`. App: openemr 8.0.0.3, synthetic data only. Cost model `lifecycle-cost/2026-10-20.1`.

## Arm `openadapt-production-relabel`

- **Verified items per operator-minute: 0.658** (51 verified of 55 items over 77.5 operator minutes; 0.618 with the 5.0 setup minutes). By hand: 0.333 an operator-minute.
- False stops per 100 items: 1.82; all stops per 100 items: 27.27; operator minutes per stop: 2.83.
- After the app change `relabel_save` (item 26): 0 of 30 items false-stopped, 7.5 operator minutes of maintenance (stops, repair reviews); first verified item after it: 27, 1147.7 s after the change.

| By hand, minutes per item | Verified per operator-minute | By hand per operator-minute | Ratio |
|---:|---:|---:|---:|
| 3 | 0.658 | 0.333 | 1.97 |
| 5 | 0.596 | 0.2 | 2.98 |
| 8 | 0.523 | 0.125 | 4.18 |
| 10 | 0.483 | 0.1 | 4.83 |

- Duplicate-check stops: 7; field shared with an existing chart: {"none at start": 1, "lname+DOB": 4, "lname": 1, "first name only": 1}; judged {"right_stop": 7}; exception kinds {"concurrent_same": 2, "dup_resend": 1, "ambiguous_same_person": 1, "dup_existing": 3}.
- Stops judged `data_stop`, by cause: other: REJECTED_POLICY HALTED / REJECTED_POLICY 3.
- Stops judged `false_stop`, by cause: other: RECONCILIATION_REQUIRED Postconditions failed for step 'step_040' (click 'Confirm Create New Patient'): 1.
- Stops judged `right_stop`, by cause: duplicate check listed a chart 7, other: REJECTED_POLICY HALTED / REJECTED_POLICY 3.
- Stops judged `stopped_saved`, by cause: identity check failed (relabeled control) 1.
- Cost-model bands, verified per operator-minute: x0.5 1.316, x1 0.658, x2 0.329.

| | x0.5 | x1 | x2 |
|---|---:|---:|---:|
| Verified completed items per operator-hour | 74.18 | 37.09 | 18.55 |
| ... if every silent wrong is later found and fixed | 74.18 | 37.09 | 18.55 |
| All-manual items per operator-hour | 40 | 20 | 10 |
| Operator work removed vs all-manual | 50.0% | 50.0% | 50.0% |
| Setup minutes | 2.5 | 5 | 10 |
| Per-item operator minutes (mean) | 0.636 | 1.273 | 2.545 |
| Maintenance minutes (all UI changes) | 3.75 | 7.5 | 15 |
| Operator hours in total | 0.688 | 1.375 | 2.75 |

- Items: 55; verified completed without hand entry: 51; right in the end: 55; done by hand: 4.
- False stops: 1 of 37 clean items, 2.7% (95% CI 0.5-13.8%); 0 of them with a UI change in force.
- Silent wrong: 0 of 40 items claimed done, 0.0% (95% CI 0.0-8.8%) (judged by direct SQL, never the product's verdict).
- Operator requests: {"stop": 14, "approval": 33, "reconcile": 1, "repair_review": 1}; actions: {"skip": 10, "approve": 33, "do_by_hand": 4, "accept_repair": 1, "confirm_saved": 1}.
- Outcomes: {"correct": 40, "right_stop": 10, "data_stop": 3, "false_stop": 1, "stopped_saved": 1}.
- Wall: 8055.8 s of item time, median 123.34 s an item; host 1-minute load median 10.3, max 95.2.
- Model calls 0, list-price cost $0.0.

Outcome by exception:

| Exception | Outcomes |
|---|---|
| ambiguous_same_person | right_stop 1 |
| clean | correct 29, false_stop 1 |
| common_surname | correct 4 |
| concurrent_other | correct 1 |
| concurrent_same | right_stop 2 |
| dup_existing | right_stop 3 |
| dup_resend | right_stop 2 |
| malformed_case | stopped_saved 1 |
| malformed_dob | correct 1, data_stop 1 |
| missing_optional | data_stop 2 |
| missing_required | right_stop 2 |
| session_expiry | correct 1 |
| shared_dob | correct 3 |
| slow_save | correct 1 |

After each UI change (items until the next change):

| Change | From item | Items | Outcomes |
|---|---:|---:|---|
| relabel_save | 26 | 30 | correct 21, data_stop 2, right_stop 6, stopped_saved 1 |

## Assumptions

Operator seconds per touch (`lifecycle-cost/2026-10-20.1`): admit 300, approval 20, compile_review 600, interrupt 30, manual_entry 180, question 60, reconcile 120, repair_review 120, stop 90, teach_prep 300. Every request also pays the interrupt charge. Bands scale every number.

No timings from real operators exist yet. Desktop and the console record when a request was raised and when it was answered, but every answer on record was given by a script, so those gaps measure the harness, not a person. Until Desktop has real operator telemetry, the per-touch numbers below are assumptions: keystroke-level estimates for reading a card and clicking (approval), plus looking a record up in the app (stop, reconcile), and a typed-entry estimate for a 12-field registration form (manual entry: about 120 characters at 40 words a minute, plus reading the referral and the duplicate search). The interrupt charge is the resumption lag reported in interruption studies (tens of seconds).
