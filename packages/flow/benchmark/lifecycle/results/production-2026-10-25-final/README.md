# Production configuration on local OpenEMR: the final run on main (2026-10-25)

This is the same 55-item stream and app change as
[`production-2026-10-24`](../production-2026-10-24/README.md) and
[`production-2026-10-25-rerun`](../production-2026-10-25-rerun/README.md):
seed 7, 10 simulated days, workload `lifecycle-workload/2026-10-24.1`, and the
save button relabeled before item 26. This time every fix is on main except
the record-channel duplicate check (#1516, still a draft).

## What was measured

Branch `measure/production-lifecycle-final` at **8bfd7f6af** (`run.log`
records it):

| Piece | Commit |
| --- | --- |
| origin/main, including #1514, #1517 (switch policy at its merged head) and #1557 (the census fix) | 78ff5ad00 |
| #1516, record-channel duplicate check over FHIR | 4b635f2d6 |
| Soak harness and arm (per-item timeout, re-arm offer, FHIR fixture patches) | this branch |

The configuration is the re-run's:

- **Profile and approval:** Regulated profile, `problems_only` approval,
  durable runs, pipelined read-back, repair on.
- **Admission:** the program was admitted afresh, with the duplicate check on
  the record channel over FHIR and the date of birth as a typed `date`.
- **Intake and recovery:** intake converted each date to ISO, relabel
  recovery was on, and the scripted operator signed any re-arm it was
  offered.
- **Limits:** a 2700 s per-item timeout and a one-hour ledger watchdog. Neither
  fired.
- **Fixture:** a fresh OpenEMR stack, with its OAuth keys restored after every
  reset.

No model was called. The #1513 deviation (an older #1517 commit) doesn't
apply here: #1517 is merged.

## Results

At x1 of the cost model (approval 20 s, stop 90 s, reconcile 120 s, repair
review 120 s, plus a 30 s interrupt on every request). These are run-only
operator minutes.

**Headline: 1.055 verified items per operator-minute, 3.17 times the by-hand
rate at 3 minutes per item.** That is 51 verified items over 48.33 operator
minutes, or 0.956 when the 5.0 setup minutes are included.

| By hand, minutes per item | Verified items per operator-minute | By hand per operator-minute | Ratio |
| ---: | ---: | ---: | ---: |
| 3 (the cost model) | 1.055 | 0.333 | 3.17 |
| 5 | 0.905 | 0.200 | 4.53 |
| 8 | 0.746 | 0.125 | 5.97 |
| 10 | 0.668 | 0.100 | 6.68 |

No by-hand time was measured from a person. Every other charge stays at x1,
and the run's own hand entries are charged at each by-hand time.

| | before (27 items) | first after (55) | re-run (55) | **this run (55)** |
| --- | ---: | ---: | ---: | ---: |
| Verified items per operator-minute | 0.033 | 0.327 | 0.658 | **1.055** |
| Charts the program saved and the oracle confirmed | 0 | 30 | 40 | **41** |
| Items right without hand entry (incl. correct non-saves) | 4 | 41 | 51 | **51** |
| Items entered by hand | 23 | 14 | 4 | **4** |
| False stops per 100 items | 70.4 | 16.4 | 1.8 | **0** (95% CI 0-9.4% of 37 clean items) |
| All stops per 100 items | 100 | 45.5 | 27.3 | **25.5** |
| Silent wrong (claimed done, oracle wrong) | 0 | 0 of 30 | 0 of 40 | **0 of 41** (95% CI 0-8.6%) |
| Approvals | | | 33 | **1** |
| Items right at the end | 27 of 27 | 53 of 55 | 55 of 55 | **55 of 55** |
| Operator minutes (run) | 123.0 | 125.5 | 77.5 | **48.3** |
| Median item time | 227 s | 119 s | 123 s | **121 s** |
| Host 1-minute load, median / max | 7.0 / 76.1 | 8.0 / 102.5 | 10.3 / 95.2 | **6.5 / 46.5** |

Where the 48.3 operator minutes went:

| Kind | Minutes |
| --- | ---: |
| 10 stops skipped (right stops) | 20.0 |
| 4 stops entered by hand | 20.0 |
| Fresh admission after the app change | 5.0 |
| Repair review, relabel | 2.5 |
| Approval: 1 (item 26) | 0.8 |
| Reconciliation | 0 |
| Re-arms | 0 |

**Re-arm minutes: 0.** The old version's switch turned off at item 26
(`screen_changed`, a program problem). The repaired version was admitted
afresh with its own switch, which stayed on through item 55. No re-arm was
needed or offered.

### Phase 0: throughput and silent-wrong criteria met

Phase 0 has three criteria: at least 2 times hand entry at 3 minutes per
item, 0 silent wrongs, and at least 15 of 17 independent faults caught. This
run measures the first two:

- **Throughput: met.** The run reached 3.17 times hand entry.
- **Silent wrongs: met.** There were 0 in the 41 items claimed done, judged by
  direct SQL and never by the product's verdict.
- **Fault coverage: measured separately** (#1539, the independent fault
  suite). This run says nothing about it, so it doesn't show that Phase 0 is
  met overall.

The per-touch charges are the cost model's assumptions, not timings from real
operators (see `REPORT.md`).

### Items timed with host load above 40

Items 7 to 10 ran while the host's 1-minute load was above 40. These are the
start and end loads:

| Item | Exception | Load at start / end | Outcome | Item time |
| ---: | --- | --- | --- | ---: |
| 7 | clean | 7.0 / 45.6 | correct | 121.0 s |
| 8 | missing required field | 45.6 / 46.5 | right stop | 9.7 s |
| 9 | clean | 46.5 / 43.3 | correct | 121.0 s |
| 10 | clean | 43.3 / 11.0 | correct | 120.1 s |

They are counted in the numbers above. Their item times match the rest of the
run. Without them the rate is 48 verified over 46.33 operator minutes, which
is 1.036 per operator-minute, or 3.11 times hand entry.

### Stops, by cause (14 of 55)

- **Right stops: 10.**
  - **8 real duplicates:**
    - 3 charts that already existed;
    - 2 registered moments earlier by another clerk;
    - 2 resends (1 at the record-channel check, 1 by an item check before
      the run started);
    - the same person with a mistyped date of birth.
  - **2 with no date of birth.** These were refused before any step.
- **Data stops: 3.**
  - A date of birth that reads either way (item 19). It was refused before
    any step.
  - 2 referrals with no home phone (items 32 and 53). The program refuses
    them as an empty input although a blank phone is allowed.
- **False stops: 0.**
- **Item 26: stopped by the harness, not by the product.** The report's judge
  counts it as a data stop. What happened:
  1. The first run refused the relabeled save button. Its identity check
     failed, which is the expected stop after an app change.
  2. The relabel repair was reviewed and accepted, re-qualified (6 cases) and
     admitted afresh.
  3. On the re-run the gate asked a person to approve the save. The scripted
     operator approved, but the harness wrote the answer unsigned and claimed
     a `cloud_aal2` sign-in.
  4. Since ea96cc4e7 (on main from 2026-10-21, not in the re-run's commit), an
     approval counts only when a registered approver key has signed it. The
     answer was refused (`auth_method_unverified`), and the gate stopped
     before saving (`gate_error`). Nothing was written, nothing was retried,
     and the item was entered by hand.

  This is the right refusal by the product and a defect in the harness. It
  costs this run one verified item and 5.0 minutes of hand entry, so the
  headline **under-states** the program by that much. The harness fix is to
  sign the scripted approval with a test approver key on the run's pinned
  roster.

### Misses

None. Every item's final state was right (55 of 55), and no duplicate was
saved. The new patients who share a last name (4) or a date of birth (3) with
an existing chart were all registered correctly, with no person.

### After the app change

- 0 of the 30 items from item 26 on false-stopped.
- Item 27 verified with no person, 1,124.7 s after the change.
- The change took 7.5 operator minutes: the repair review (2.5) and the fresh
  admission (5.0).

## Files

Paths in the committed files are relative to the run's root (`runs/<name>/`).
They were rewritten from the paths of the machine that ran the soak, and the
harness now writes them relative itself (`benchmark/lifecycle/paths.py`). The
report recomputes to the same `results.json`.

Everything is computed from `ledger.jsonl`
(`python -m benchmark.lifecycle.report <dir>`):

- `ledger.jsonl`, `workload.jsonl`, `results.json`, `REPORT.md`,
  `stops.jsonl`;
- `arm-ledger.jsonl`;
- `switch/`: the switch record and both logs (the original version's and the
  repaired version's);
- `run.log`: the console output, with the merge wait, the commit, and every
  item line.
