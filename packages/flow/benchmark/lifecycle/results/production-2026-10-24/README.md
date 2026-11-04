# Production configuration on local OpenEMR: before and after (2026-10-24)

Does the production configuration remove work, or only stop safely? These
runs measure it with the lifecycle soak on the local OpenEMR referral
fixture (OpenEMR 8.0.0.3, synthetic patients only).

## The configuration

It is the configuration `benchmark/lifecycle_proof` proved in proof-15. The
soak runs it per item through `production_arm.py`:

- the Regulated profile;
- governed authorization from the run gate on the certified, encrypted
  bundle, pinned to the admitted digest;
- durable runs with an encrypted checkpoint store;
- the API tier where admissible (refused on this program by its own proposal,
  so the GUI save runs);
- pipelined read-back, one window (the harness waits for each check before it
  judges the item);
- self-repair on;
- `problems_only` approval.

No model was called in any run.

## The stream

The stream is seed 7, 55 referrals over 10 simulated days, workload
`lifecycle-workload/2026-10-24.1`. Its items:

- 30 clean;
- 4 new patients who share a last name with an existing chart;
- 3 who share a date of birth with one;
- the soak's data exceptions and item faults (duplicates, resends, missing and
  malformed values, other clerks, a slow save, a forced logout).

The mix is stress-weighted, not a measured intake mix.

One app change ran before item 26: OpenEMR's save button "Confirm Create New
Patient" became "Save new patient" (proof-15's change), served to every
client.

The operator is the soak's scripted operator. It is charged from cost model
`lifecycle-cost/2026-10-20.1`:

- approval: 20 s;
- stop: 90 s;
- reconcile: 120 s;
- repair review: 120 s;
- entering an item by hand: 180 s;
- admission: 300 s;
- plus a 30 s interrupt on every request.

These are assumptions, not measured operator times; see the bands below.

## The two arms

| Arm | What runs |
| --- | --- |
| `before` | Main as it was: the recorded program, whose duplicate check requires "No matches were found.", and no relabel repair. Admitted afresh by its own qualification (6 cases, 796 s). |
| `after` | The duplicate check reads OpenEMR's match list (`no_duplicate_candidate`, a fresh admission of that program version). The relabel repair is on. A switched-off `problems_only` gate asks a person before the save instead of stopping. Code: branch `fix/relabel-repair-duplicate-list`. |

`before` was **stopped early at 27 of 55 items**, at the coordinator's
request. Every item had stopped, and continuing would only have repeated
that. Two of its items ran after the app change.

## Results

### The metrics, exactly

- **Verified items per operator-minute (the headline).**
  - Numerator: items whose final state the independent oracle (a direct SQL
    read) judges right and that no person entered by hand. This includes
    items where the right outcome is no new chart (a duplicate, a resend, a
    missing date of birth) and the program stopped without writing.
  - Denominator: every operator minute the run charged. That covers
    approvals, stops, reconciliations, resumes, repair reviews, fresh
    admissions and items entered by hand, with a 30 s interrupt on every
    request. Setup (the first admission) is left out; the "with setup" row
    adds it.
- **The by-hand reference.** One item entered by hand costs 180 s (the cost
  model's `manual_entry_s`), so 0.333 items per operator-minute. Every cost
  scales together in the bands, so the ratio of the two numbers is the same
  in every band.
- **Operator minutes for the whole stream** (a secondary view). The run's
  operator minutes, compared with entering every item of the stream by hand
  (55 × 180 s = 165 minutes). This view credits the program for the 14 items
  it left to a person only through the minutes it spent on them, so it reads
  more favourably than the headline.

### Headline

**About break-even on this stream.** After the fixes, the configuration
completes 0.327 verified items per operator-minute, against 0.333 entering
them by hand: 0.98 times the by-hand rate, in every cost-model band (x0.5:
0.653 against 0.667; x2: 0.163 against 0.167). Before the fixes, main
completed 0.033, a tenth of the by-hand rate, and the program saved no chart
at all.

| | before (27 items) | after (55 items) |
| --- | ---: | ---: |
| **Verified items per operator-minute** (run only) | **0.033** | **0.327** |
| ... with setup | 0.031 | 0.314 |
| By hand, items per operator-minute | 0.333 | 0.333 |
| Charts the program saved and the oracle confirmed | 0 | 30 |
| Items right without hand entry (incl. correct non-saves) | 4 | 41 |
| Items entered by hand | 23 | 14 |
| False stops per 100 items | 70.4 | 16.4 |
| All stops per 100 items | 100 | 45.5 |
| Operator minutes per stopped item | 4.56 | 3.72 |
| Operator minutes in total (run) | 123.0 | 125.5 |
| Operator minutes for the stream by hand | 81.0 (27 items) | 165.0 (55 items) |
| Silent wrong (claimed done, oracle wrong) | 0 of 0 | 0 of 30 (95% CI 0-11.3%) |
| Wrong writes the record check caught (`flagged_wrong`) | 0 | 2 |
| Median item time | 227 s | 119 s |
| Host 1-minute load, median / max | 7.0 / 76.1 | 8.0 / 102.5 |

### If entering an item by hand takes longer than 3 minutes

The cost model charges 3 minutes to enter one referral by hand (the
12-field form, reading the referral and the duplicate search). A real intake
desk, with lookups, phone calls and interruptions, may take longer. This
table recomputes `after` from its own ledger with each by-hand time. The
run's own hand entries are charged at that time too; every other charge stays
at x1:
- approval 20 s;
- stop 90 s;
- reconcile 120 s;
- repair review 120 s;
- a 30 s interrupt on every request.

| By hand, minutes per item | after: verified items per operator-minute | By hand: items per operator-minute | after ÷ by hand |
| ---: | ---: | ---: | ---: |
| 3 (the cost model) | 0.327 | 0.333 | 0.98 |
| 5 | 0.267 | 0.200 | 1.34 |
| 8 | 0.210 | 0.125 | 1.68 |
| 10 | 0.183 | 0.100 | 1.83 |

None of these times is measured from a person. `benchmark/lifecycle/metrics.py`
(`BY_HAND_MINUTES`) computes the table, and `results.json` holds it under
`headline.by_hand_sensitivity`.

Where `after`'s 125.5 operator minutes went:

| Kind | Minutes |
| --- | ---: |
| Stops, with the items entered by hand | 82.0 |
| Approvals (30 × 50 s) | 25.0 |
| Reconciliations | 11.0 |
| Relabel repair review | 2.5 |
| Fresh admission after the app change | 5.0 |

**The switch turning off.** All 25.0 approval minutes (20% of the run's
operator time) came after one caught problem turned the `problems_only`
switch off: the malformed date of birth at item 6 (`uncertain_result`). The
switch record opened for the re-admitted version turned off the same way, at
its first item, 26. A switch that has turned off stays off until a new switch
record is issued; this run does not change that rule. Whether it should
re-arm on its own is a policy decision, not made here.

### Why items stopped

`before` stopped on all 27 items:

- **Duplicate check: 15.** OpenEMR lists any chart that shares a first or last
  name or the date of birth, and the recorded program stops on any listed
  chart. 9 of the 15 shared only a first name with an existing chart.
- **`problems_only` switched off, stopped unasked: 10.** The switch turned
  off, and every later item stopped at the save without a person being asked
  (fixed in `after`).
- Missing date of birth: 1 (right).
- Relabeled save, identity check: 1.

`after` stopped on 25 of 55:

- **Duplicate check: 18**, by the field the item shared with an existing chart:
  last name 9, last name and date of birth 5, date of birth 2, first name only
  1, none at the start 1.
  - 8 were right stops: duplicates, resends, another clerk's chart, and the
    same person with a mistyped date of birth.
  - 9 were false stops: new patients with a common surname or a shared date of
    birth. The rule stops them on purpose; a person decides.
- Missing values: 4.
  - 2 missing a date of birth were right stops.
  - 2 missing a home phone stopped too: the program refuses an empty input,
    although a blank home phone is allowed.
- Malformed date of birth: 2 wrong writes caught (below).
- Relabeled save: 1, repaired (below).

### The two wrong writes

The program types the date of birth into a text field, and its parameter is
declared as a plain `string`. Two referrals wrote it as `MM/DD/YYYY`
(`malformed_dob`). OpenEMR stored today's date. In both, the record check
refuted the save (`RECONCILIATION_REQUIRED`) and the item went to a person.
Neither was silent, but a wrong chart landed.

The first one, item 6, turned the `problems_only` switch off for the rest of
the run (`uncertain_result`). Every later saved item then needed one approval:
the 25 approval minutes above. Declaring the date of birth as a date
parameter refuses these inputs before any step. That change is in #1514
(`ParamKind.DATE` is checked before the run starts; `typed_dob` in the arm).
It is not in this `after` run, and it is the next measured change.
`before` didn't write them only because it stopped earlier on these items.

### After the app change: stopped to running again

At item 26 the run stopped before the save: the save button's identity
check failed. The smallest safe path back, measured live:

1. **Prove it's a relabel** (`repair/relabel.py`). The recorded identity hash
   matches the observed control with only the label swapped back, so the
   control id, kind and form are unchanged.
2. **Re-qualify on the new release.** This is the parent's own six-case
   campaign on the test copy, with the environment lock re-pinned to the
   running release (one changed file, `new_search_popup.php`). It took 763 s
   of machine time.
3. **A person reviews once** (charged 2.5 minutes). A save button's identity is
   part of the record check, so no lineage hop may carry it, and approving it
   is a **fresh admission** of the certified copy (charged 5 minutes). The
   deployment re-pins to its digest.
4. The stopped item, which never wrote, ran again on the new version as a
   retry and saved once.

That's 8.3 operator minutes, including the retry's approval. It took 896 s
from the stop to the item done, and 1,370 s from the change to the next
verified item (item 28). The smoke run in `recovery-smoke/` measured the same
path: 850 s to re-qualify, then VERIFIED.

In `before` and in proof-15, every item after this change stopped until a
person re-taught the step.

## Product bugs these runs found (fixed on `fix/relabel-repair-duplicate-list`)

1. **Re-qualification after a repair was always refused in the process that
   qualified the parent.** A case run's authorization id is its run id, and a
   process refuses an id it has already used. `readmission.requalify` now
   names each re-qualification's runs afresh. proof-15's repair hop never
   ran, so it never hit this.
2. **A switched-off `problems_only` gate never asked a person.** The switch
   check runs at the save, after the last point a person can be asked. The
   gate now asks there while the switch log reads off, as `each_item` does.
3. **The duplicate check stopped on first-name matches.** The new
   postcondition stops only on a listed chart with the item's last name or
   date of birth, and fails closed on an unreadable or unloaded list.

The harness found and fixed two problems of its own:

- the DB dump was not binary-safe;
- restoring an older dump of OpenEMR's `keys` table broke every OAuth token.

## What these runs don't show

- **Operator times are assumed** (the cost model); no person was timed.
- **One seed.** The 95% intervals on rates are wide (false stops 13-40% in
  `after`).
- `before` covers 27 items, not 55.
- **Timings:** some items ran at a 1-minute load above 40 (max 102 in
  `after`, 76 in `before`). Item times at those loads may be longer.
- **The switch record for a re-admitted version is opened locally**, as
  proof-15 does; Cloud's thresholds and status route are not part of the run.
- **The harness's by-hand fix of a wrong chart creates a second chart**
  rather than editing the wrong one, so those 2 items end `wrong` in the
  final view.
- **Pipelined read-back's wall-time gain is not shown**, because the harness
  waits for each check.

## Files

Each of `before/`, `after/` and `recovery-smoke/` holds:

- `ledger.jsonl`: every event, item, operator request and phase;
- `workload.jsonl`: the stream and its ground truth;
- `results.json` and `REPORT.md`, computed from the ledger
  (`python -m benchmark.lifecycle.report <dir>` recomputes them);
- `stops.jsonl`;
- `arm-ledger.jsonl`: the arm's own events;
- `switch/`: the `problems_only` switch records and logs.

## Reproduce

Run one Docker fixture at a time. Set `STATE_DIR` to a directory for the
fixture's state and `KEYS_DIR` to one for its test keys, both outside the
repository. Then,
from `packages/flow`:

```bash
export LIFECYCLE_OPENEMR_PROJECT=openadapt-openemr-compose LIFECYCLE_UI_PORT=9401 LIFECYCLE_API_PORT=9400
export LIFECYCLE_OPENEMR_STATE=$STATE_DIR PYTHONPATH=.:../types
python -m benchmark.lifecycle_proof.run up --out runs/up --keys $KEYS_DIR
python -m benchmark.lifecycle.run --app openemr --seed 7 --items 55 --days 10 --drift relabel_save@26 \
  --arm benchmark.lifecycle.production_arm:make --arm-options '{"recovery":"none"}' --out <dir>
python -m benchmark.lifecycle.run --app openemr --seed 7 --items 55 --days 10 --drift relabel_save@26 \
  --arm benchmark.lifecycle.production_arm:make \
  --arm-options '{"recovery":"relabel","duplicate_check":true}' --out <dir>
```
