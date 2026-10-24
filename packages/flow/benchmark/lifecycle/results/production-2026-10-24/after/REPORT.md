# Lifecycle soak report

Protocol `lifecycle/2026-10-20.1`, workload `lifecycle-workload/2026-10-24.1`, seed 7, 55 referrals over 10 simulated days. Engine commit `26947f9683d6`. App: openemr 8.0.0.3, synthetic data only. Cost model `lifecycle-cost/2026-10-20.1`.

## Arm `openadapt-production-relabel`

- **Verified items per operator-minute: 0.327** (41 verified of 55 items over 125.5 operator minutes; 0.314 with the 5.0 setup minutes). By hand: 0.333 an operator-minute.
- False stops per 100 items: 16.36; all stops per 100 items: 45.45; operator minutes per stop: 3.72.
- After the app change `relabel_save` (item 26): 7 of 30 items false-stopped, 42.5 operator minutes of maintenance (stops, repair reviews); first verified item after it: 28, 1370.3 s after the change.

| By hand, minutes per item | Verified per operator-minute | By hand per operator-minute | Ratio |
|---:|---:|---:|---:|
| 3 | 0.327 | 0.333 | 0.98 |
| 5 | 0.267 | 0.2 | 1.34 |
| 8 | 0.21 | 0.125 | 1.68 |
| 10 | 0.183 | 0.1 | 1.83 |

- Duplicate-check stops: 18; field shared with an existing chart: {"lname": 9, "DOB": 2, "none at start": 1, "lname+DOB": 5, "first name only": 1}; judged {"false_stop": 9, "right_stop": 8, "fault_stop": 1}; exception kinds {"clean": 2, "shared_dob": 3, "concurrent_same": 2, "dup_resend": 2, "ambiguous_same_person": 1, "common_surname": 4, "dup_existing": 3, "slow_save": 1}.
- Stops judged `data_stop`, by cause: other: REJECTED_POLICY HALTED / REJECTED_POLICY 2.
- Stops judged `false_stop`, by cause: duplicate check listed a chart 9.
- Stops judged `fault_stop`, by cause: duplicate check listed a chart 1.
- Stops judged `flagged_wrong`, by cause: other: RECONCILIATION_REQUIRED System-of-record effect verification HALTED step 'step_040' (click 'Confirm Crea 2.
- Stops judged `right_stop`, by cause: duplicate check listed a chart 8, other: REJECTED_POLICY HALTED / REJECTED_POLICY 2.
- Stops judged `stopped_saved`, by cause: identity check failed (relabeled control) 1.
- Cost-model bands, verified per operator-minute: x0.5 0.653, x1 0.327, x2 0.163.

| | x0.5 | x1 | x2 |
|---|---:|---:|---:|
| Verified completed items per operator-hour | 37.7 | 18.85 | 9.43 |
| ... if every silent wrong is later found and fixed | 37.7 | 18.85 | 9.43 |
| All-manual items per operator-hour | 40 | 20 | 10 |
| Operator work removed vs all-manual | 20.9% | 20.9% | 20.9% |
| Setup minutes | 2.5 | 5 | 10 |
| Per-item operator minutes (mean) | 1.073 | 2.145 | 4.291 |
| Maintenance minutes (all UI changes) | 3.75 | 7.5 | 15 |
| Operator hours in total | 1.087 | 2.175 | 4.35 |

- Items: 55; verified completed without hand entry: 41; right in the end: 53; done by hand: 14.
- False stops: 9 of 37 clean items, 24.3% (95% CI 13.4-40.1%); 7 of them with a UI change in force.
- Silent wrong: 0 of 30 items claimed done, 0.0% (95% CI 0.0-11.3%) (judged by direct SQL, never the product's verdict).
- Operator requests: {"stop": 23, "reconcile": 2, "approval": 30, "repair_review": 1}; actions: {"do_by_hand": 14, "approve": 30, "skip": 10, "accept_repair": 1, "confirm_saved": 1}.
- Outcomes: {"correct": 30, "false_stop": 9, "flagged_wrong": 2, "right_stop": 10, "stopped_saved": 1, "data_stop": 2, "fault_stop": 1}.
- Wall: 9124.5 s of item time, median 118.78 s an item; host 1-minute load median 8.0, max 102.5.
- Model calls 0, list-price cost $0.0.

Outcome by exception:

| Exception | Outcomes |
|---|---|
| ambiguous_same_person | right_stop 1 |
| clean | correct 28, false_stop 2 |
| common_surname | false_stop 4 |
| concurrent_other | correct 1 |
| concurrent_same | right_stop 2 |
| dup_existing | right_stop 3 |
| dup_resend | right_stop 2 |
| malformed_case | stopped_saved 1 |
| malformed_dob | flagged_wrong 2 |
| missing_optional | data_stop 2 |
| missing_required | right_stop 2 |
| session_expiry | correct 1 |
| shared_dob | false_stop 3 |
| slow_save | fault_stop 1 |

After each UI change (items until the next change):

| Change | From item | Items | Outcomes |
|---|---:|---:|---|
| relabel_save | 26 | 30 | correct 13, data_stop 2, false_stop 7, fault_stop 1, right_stop 6, stopped_saved 1 |

## Assumptions

Operator seconds per touch (`lifecycle-cost/2026-10-20.1`): admit 300, approval 20, compile_review 600, interrupt 30, manual_entry 180, question 60, reconcile 120, repair_review 120, stop 90, teach_prep 300. Every request also pays the interrupt charge. Bands scale every number.

No timings from real operators exist yet. Desktop and the console record when a request was raised and when it was answered, but every answer on record was given by a script, so those gaps measure the harness, not a person. Until Desktop has real operator telemetry, the per-touch numbers below are assumptions: keystroke-level estimates for reading a card and clicking (approval), plus looking a record up in the app (stop, reconcile), and a typed-entry estimate for a 12-field registration form (manual entry: about 120 characters at 40 words a minute, plus reading the referral and the duplicate search). The interrupt charge is the resumption lag reported in interruption studies (tens of seconds).
