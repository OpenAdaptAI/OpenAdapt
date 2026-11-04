# Lifecycle soak report

Protocol `lifecycle/2026-10-20.1`, workload `lifecycle-workload/2026-10-20.2`, seed 7, 2 referrals over 1 simulated days. Engine commit `fd0bdcbac807`. App: openemr 8.0.0.3, synthetic data only. Cost model `lifecycle-cost/2026-10-20.1`.

## Arm `openadapt-production-relabel`

- **Verified items per operator-minute: 0.075** (1 verified of 2 items over 13.33 operator minutes; 0.055 with the 5.0 setup minutes). By hand: 0.333 an operator-minute.
- False stops per 100 items: 50.0; all stops per 100 items: 50.0; operator minutes per stop: 5.0.
- After the app change `relabel_save` (item 1): 1 of 2 items false-stopped, 12.5 operator minutes of maintenance (stops, repair reviews); first verified item after it: 1, 1133.8 s after the change.
- Stops judged `false_stop`, by cause: duplicate check listed a chart 1.
- Cost-model bands, verified per operator-minute: x0.5 0.15, x1 0.075, x2 0.037.

| | x0.5 | x1 | x2 |
|---|---:|---:|---:|
| Verified completed items per operator-hour | 6.55 | 3.27 | 1.64 |
| ... if every silent wrong is later found and fixed | 6.55 | 3.27 | 1.64 |
| All-manual items per operator-hour | 40 | 20 | 10 |
| Operator work removed vs all-manual | -205.6% | -205.6% | -205.6% |
| Setup minutes | 2.5 | 5 | 10 |
| Per-item operator minutes (mean) | 1.458 | 2.917 | 5.833 |
| Maintenance minutes (all UI changes) | 3.75 | 7.5 | 15 |
| Operator hours in total | 0.153 | 0.306 | 0.611 |

- Items: 2; verified completed without hand entry: 1; right in the end: 2; done by hand: 1.
- False stops: 1 of 2 clean items, 50.0% (95% CI 9.4-90.5%); 1 of them with a UI change in force.
- Silent wrong: 0 of 1 items claimed done, 0.0% (95% CI 0.0-79.3%) (judged by direct SQL, never the product's verdict).
- Operator requests: {"repair_review": 1, "approval": 1, "stop": 1}; actions: {"accept_repair": 1, "approve": 1, "do_by_hand": 1}.
- Outcomes: {"correct": 1, "false_stop": 1}.
- Wall: 1395.0 s of item time, median 1133.0 s an item; host 1-minute load median 20.7, max 20.7.
- Model calls 0, list-price cost $0.0.

Outcome by exception:

| Exception | Outcomes |
|---|---|
| clean | correct 1, false_stop 1 |

After each UI change (items until the next change):

| Change | From item | Items | Outcomes |
|---|---:|---:|---|
| relabel_save | 1 | 2 | correct 1, false_stop 1 |

## Assumptions

Operator seconds per touch (`lifecycle-cost/2026-10-20.1`): admit 300, approval 20, compile_review 600, interrupt 30, manual_entry 180, question 60, reconcile 120, repair_review 120, stop 90, teach_prep 300. Every request also pays the interrupt charge. Bands scale every number.

No timings from real operators exist yet. Desktop and the console record when a request was raised and when it was answered, but every answer on record was given by a script, so those gaps measure the harness, not a person. Until Desktop has real operator telemetry, the per-touch numbers below are assumptions: keystroke-level estimates for reading a card and clicking (approval), plus looking a record up in the app (stop, reconcile), and a typed-entry estimate for a 12-field registration form (manual entry: about 120 characters at 40 words a minute, plus reading the referral and the duplicate search). The interrupt charge is the resumption lag reported in interruption studies (tens of seconds).
