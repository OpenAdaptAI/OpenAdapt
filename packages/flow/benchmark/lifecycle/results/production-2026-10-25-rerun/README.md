# Production configuration on local OpenEMR: the after arm re-run (2026-10-25)

This is the same 55-item stream and app change as
[`production-2026-10-24`](../production-2026-10-24/README.md): seed 7, 10
simulated days, workload `lifecycle-workload/2026-10-24.1`, and the save button
relabeled before item 26. This time the fixes that followed it are in. The
code isn't on main yet, so these are the exact commits measured.

## What was measured

Branch `measure/production-lifecycle-rerun` at **38fc21afb** (`run.log`
records it), which contains:

| Piece | PR | Commit in this run |
| --- | --- | --- |
| Relabel repair, match-list duplicate check, fresh re-qualification ids, switched-off gate asks, ISO dates at intake, typed dates refused before any step | #1514 | 1485eb851 and the intake/typed-date commits |
| Switch policy: input problems leave the switch on, signed re-arm after 10 verified items | #1517 | **6b52dd320** (see the deviation below) |
| Record-channel duplicate check: FHIR, structured `duplicate_check` outcome | #1516 | 647af767d |
| Soak harness and arm: per-item timeout, re-arm offered to the operator, FHIR fixture patches | this branch | 4f7d71dc2, 38fc21afb |

Configuration: the same as before, plus:

- **Regulated profile, `problems_only` approval:** governed run gate, pinned
  digest, durable runs, pipelined read-back, repair on.
- **Program version:** admitted afresh with:
  - the duplicate check pinned to the record channel over **FHIR**
    (`candidate_source="record"`, `candidate_channel="fhir"`). OpenEMR's REST
    search can't page here, because its next link names the configured site
    address; FHIR returns every match in one Bundle;
  - the date of birth as a typed `date` parameter.
- **Intake:** each item's date converted to ISO as intake does
  (`intake_dates`).
- **Recovery and re-arm:** relabel recovery on, and the scripted operator
  signs a re-arm when one is offered.
- **Watchdogs:** a 2700 s per-item timeout and a one-hour ledger watchdog.
  Neither fired.
- **Fixture:** a fresh OpenEMR stack. Its OAuth keys were restored after every
  reset, and the read client had `api:fhir user/Patient.read`
  (`openemr_patches.py`).

No model was called.

### Deviation: the switch policy commit

The run used #1517 at 6b52dd320. That commit counted a duplicate stop as an
item-input problem only when `duplicate_check == "candidate"` (the screen
check's value). The record check reports `"exact"` or `"close"`, and
f42e51a8e, which counts those too, was not on this branch. So in this run
every real duplicate the record check found turned the switch off as a
program problem, instead of leaving it on.

That happened at item 12 (another clerk had registered the same patient).
From item 13 on, every saved item waited for a person's approval:

- 33 approvals, 27.5 operator minutes;
- no run of 10 verified items since a switch-off ever completed, so no
  re-arm was offered.

#1517's head classes these correctly, with tests. This run therefore
**under-states** the policy's effect. The numbers below are as measured.

## Phase 0 gate: not met

The gate is at least 2 times the by-hand rate at 3 minutes per item, with 0
silent wrongs. This run measured **1.97 times**, just under 2, with **0 silent
wrongs**. So the gate is **not met** on this run:

- the rate condition fails, by 0.03;
- the silent-wrong condition holds;
- the run used an older switch-policy commit (see the deviation below), so
  the result understates the policy as merged.

A further 55-item run on main (with #1517 merged) plus #1516 is queued to
measure the policy as merged.

## Results

At x1 of the cost model (approval 20 s, stop 90 s, reconcile 120 s, repair
review 120 s, a 30 s interrupt on every request). Run-only operator minutes.

**Headline: 0.658 verified items per operator-minute, 1.97 times the by-hand
rate at 3 minutes per item.** Before the fixes, main reached 0.033, and the
first `after` run 0.327.

| By hand, minutes per item | Verified items per operator-minute | By hand per operator-minute | Ratio |
| ---: | ---: | ---: | ---: |
| 3 (the cost model) | 0.658 | 0.333 | 1.97 |
| 5 | 0.596 | 0.200 | 2.98 |
| 8 | 0.523 | 0.125 | 4.18 |
| 10 | 0.483 | 0.100 | 4.83 |

No by-hand time is measured from a person. Every other charge stays at x1, and
the run's own hand entries are charged at each by-hand time.

| | before (27 items) | first after (55) | this re-run (55) |
| --- | ---: | ---: | ---: |
| Verified items per operator-minute | 0.033 | 0.327 | **0.658** |
| Charts the program saved and the oracle confirmed | 0 | 30 | **40** |
| Items right without hand entry (incl. correct non-saves) | 4 | 41 | **51** |
| Items entered by hand | 23 | 14 | **4** |
| False stops per 100 items | 70.4 | 16.4 | **1.8** |
| All stops per 100 items | 100 | 45.5 | **27.3** |
| Silent wrong (claimed done, oracle wrong) | 0 | 0 of 30 | **0 of 40** (95% CI 0-8.8%) |
| Wrong writes the record check caught | 0 | 2 | **0** |
| Items right at the end | 27 of 27 | 53 of 55 | **55 of 55** |
| Operator minutes (run) | 123.0 | 125.5 | **77.5** |
| Median item time | 227 s | 119 s | 123 s |
| Host 1-minute load, median / max | 7.0 / 76.1 | 8.0 / 102.5 | 10.3 / 95.2 |

Where the 77.5 operator minutes went:

| Kind | Minutes |
| --- | ---: |
| Stops, including 4 items entered by hand | 37.0 |
| Approvals: 33 after the switch turned off (the deviation) | 27.5 |
| Repair review, relabel | 2.5 |
| Fresh admission after the app change | 5.0 |
| Reconciliation | 5.5 |
| Re-arms | 0 (none offered) |

### Stops, by cause (15 of 55)

- **Right stops: 10.**
  - **8 real duplicates.**
    - Which: 3 charts that already existed, 2 registered moments earlier by
      another clerk, 2 resends, and the same person with a mistyped date of
      birth.
    - Where they stopped: 7 at the record-channel duplicate check; 1 resend
      by an item check before the run started.
  - 2 with no date of birth (refused before any step).
- **Data stops: 3.**
  - A date of birth `10/01/1958` that reads either way. Intake held it
    back, and the typed date refused it before any step. Nothing was written.
  - 2 referrals with no home phone, which the program refuses as an empty
    input although a blank phone is allowed.
- **False stop: 1 (item 20).** The click on "Confirm Create New Patient" didn't
  register: the duplicate dialog stayed open with the button focused, and no
  chart was written. The program couldn't confirm the save and sent the item
  to reconciliation (fail closed). It is a program problem, so the switch
  stayed off.
- **Saved, then stopped: 1 (item 26).** The relabel item: re-qualified,
  admitted afresh, and saved on the re-run. The record check couldn't match a
  last name written in capitals with a trailing space, so a person confirmed
  it.

### Misses

None. Every item's final state was right (55 of 55), and no duplicate was
saved. The 4 new patients who share a last name with an existing chart, and
the 3 who share a date of birth, were all registered correctly. Only item 5
needed no person; the other 6 came after item 12 and waited for the approval
the switch-off required (the deviation above).

### After the app change

- Item 26 stopped on the relabeled save, was re-qualified on the new release
  and admitted afresh, and saved.
- Item 27 verified with no person, 1,148 s after the change.
- Operator time for the change: 7.5 minutes (review 2.5, admission 5.0).
- No other item false-stopped after the change.

## Where the code for this run is

This directory holds the evidence only. The run's harness changes are on
`measure/production-lifecycle-rerun` and land with or after #1516, because the
arm sets #1516's `candidate_channel`:
- the per-item timeout;
- the re-arm offered to the operator;
- the soak-only FHIR patches in `benchmark/lifecycle/openemr_patches.py`;
- the fixture's token-scope fix for `api:fhir`.

## Files

Everything is computed from `ledger.jsonl`
(`python -m benchmark.lifecycle.report <dir>`):

- `ledger.jsonl`, `workload.jsonl`, `results.json`, `REPORT.md`,
  `stops.jsonl`;
- `arm-ledger.jsonl`;
- `switch/`: both switch records and logs;
- `run.log`: the console output, with the commit.
