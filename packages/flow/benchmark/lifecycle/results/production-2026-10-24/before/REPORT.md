# Lifecycle soak report

Protocol `lifecycle/2026-10-20.1`, workload `lifecycle-workload/2026-10-24.1`, seed 7, 55 referrals over 10 simulated days. Engine commit `26947f9683d6`. App: openemr 8.0.0.3, synthetic data only. Cost model `lifecycle-cost/2026-10-20.1`.

## Arm `openadapt-production` (incomplete)

- **Verified items per operator-minute: 0.033** (4 verified of 27 items over 123.0 operator minutes; 0.031 with the 5.0 setup minutes). By hand: 0.333 an operator-minute.
- False stops per 100 items: 70.37; all stops per 100 items: 100.0; operator minutes per stop: 4.56.
- After the app change `relabel_save` (item 26): 1 of 2 items false-stopped, 5.0 operator minutes of maintenance (stops, repair reviews); first verified item after it: None, None s after the change.

| By hand, minutes per item | Verified per operator-minute | By hand per operator-minute | Ratio |
|---:|---:|---:|---:|
| 3 | 0.033 | 0.333 | 0.1 |
| 5 | 0.024 | 0.2 | 0.12 |
| 8 | 0.017 | 0.125 | 0.13 |
| 10 | 0.014 | 0.1 | 0.14 |

- Duplicate-check stops: 15; field shared with an existing chart: {"first name only": 9, "lname": 3, "DOB": 1, "none at start": 1, "lname+DOB": 1}; judged {"false_stop": 10, "right_stop": 3, "fault_stop": 1, "data_stop": 1}; exception kinds {"clean": 8, "shared_dob": 1, "concurrent_same": 1, "concurrent_other": 1, "malformed_dob": 1, "dup_resend": 1, "ambiguous_same_person": 1, "common_surname": 1}.
- Stops judged `data_stop`, by cause: duplicate check listed a chart 1, identity check failed (relabeled control) 1, problems_only switch off; stopped unasked 1.
- Stops judged `false_stop`, by cause: duplicate check listed a chart 10, problems_only switch off; stopped unasked 9.
- Stops judged `fault_stop`, by cause: duplicate check listed a chart 1.
- Stops judged `right_stop`, by cause: duplicate check listed a chart 3, other: REJECTED_POLICY HALTED / REJECTED_POLICY 1.
- Cost-model bands, verified per operator-minute: x0.5 0.065, x1 0.033, x2 0.016.

| | x0.5 | x1 | x2 |
|---|---:|---:|---:|
| Verified completed items per operator-hour | 3.75 | 1.88 | 0.94 |
| ... if every silent wrong is later found and fixed | 3.75 | 1.88 | 0.94 |
| All-manual items per operator-hour | 40 | 20 | 10 |
| Operator work removed vs all-manual | -58.0% | -58.0% | -58.0% |
| Setup minutes | 2.5 | 5 | 10 |
| Per-item operator minutes (mean) | 2.278 | 4.556 | 9.111 |
| Maintenance minutes (all UI changes) | 0 | 0 | 0 |
| Operator hours in total | 1.067 | 2.133 | 4.267 |

- Items: 27; verified completed without hand entry: 4; right in the end: 27; done by hand: 23.
- False stops: 19 of 19 clean items, 100.0% (95% CI 83.2-100.0%); 1 of them with a UI change in force.
- Silent wrong: 0 of 0 items claimed done, n/a (judged by direct SQL, never the product's verdict).
- Operator requests: {"stop": 27}; actions: {"do_by_hand": 23, "skip": 4}.
- Outcomes: {"false_stop": 19, "data_stop": 3, "right_stop": 4, "fault_stop": 1}.
- Wall: 5743.1 s of item time, median 226.94 s an item; host 1-minute load median 7.0, max 76.1.
- Model calls 0, list-price cost $0.0.

Outcome by exception:

| Exception | Outcomes |
|---|---|
| ambiguous_same_person | right_stop 1 |
| clean | false_stop 17 |
| common_surname | false_stop 1 |
| concurrent_other | fault_stop 1 |
| concurrent_same | right_stop 1 |
| dup_resend | right_stop 1 |
| malformed_case | data_stop 1 |
| malformed_dob | data_stop 2 |
| missing_required | right_stop 1 |
| shared_dob | false_stop 1 |

After each UI change (items until the next change):

| Change | From item | Items | Outcomes |
|---|---:|---:|---|
| relabel_save | 26 | 2 | data_stop 1, false_stop 1 |

## Assumptions

Operator seconds per touch (`lifecycle-cost/2026-10-20.1`): admit 300, approval 20, compile_review 600, interrupt 30, manual_entry 180, question 60, reconcile 120, repair_review 120, stop 90, teach_prep 300. Every request also pays the interrupt charge. Bands scale every number.

No timings from real operators exist yet. Desktop and the console record when a request was raised and when it was answered, but every answer on record was given by a script, so those gaps measure the harness, not a person. Until Desktop has real operator telemetry, the per-touch numbers below are assumptions: keystroke-level estimates for reading a card and clicking (approval), plus looking a record up in the app (stop, reconcile), and a typed-entry estimate for a 12-field registration form (manual entry: about 120 characters at 40 words a minute, plus reading the referral and the duplicate search). The interrupt charge is the resumption lag reported in interruption studies (tens of seconds).
