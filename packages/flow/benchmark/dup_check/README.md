# Duplicate check: screen against record, offline

In the production soak (#1513), the duplicate check was the largest operator
cost after approvals. It stops a new patient who shares a last name or date
of birth with an existing chart. This measures the check that asks the system
of record instead (`runtime/checks/duplicate_record.py`) against the screen
rule, on the same stream and the same patient table.

```bash
python -m benchmark.dup_check.measure          # writes results/*.json
```

No app, no model calls. `measure.py` says exactly what each check sees.

## The streams

- `soak-seed7-55`: the 55 items the soak ran (`lifecycle/results/production-2026-10-24/after/workload.jsonl`), with its 12 existing charts.
- `seed7-500`: 500 items from the same generator and seed (`lifecycle.workload.generate(7, 500, 10)`).

Planted into each, for every existing chart, are true duplicates the soak
lacks:
- the same person under a married name;
- the same person under a hyphenated name;
- the same person with day and month swapped (when that is a valid date).

Three more kinds were added after the record check's rules were written, as
a held-out set: a typo in the first name, a typo in the last name, and a
married name plus a mistyped day of birth.

Each item meets the existing charts, every earlier item's right outcome from
the soak's truth, and another clerk's chart for `concurrent_same`.

## Results (2026-10-24)

| stream | items | new-chart items | true duplicates | screen: false stops per 100 items | record: false stops per 100 items | screen: missed | record: missed |
|---|---:|---:|---:|---:|---:|---:|---:|
| soak-seed7-55 + planted | 118 | 45 | 71 | 8.5 (10) | 0.0 (0) | 12 | 12 |
| seed7-500 + planted | 563 | 440 | 110 | 44.8 (252) | 0.4 (2) | 12 | 12 |

- **Screen false stops** are new patients who share a last name or date of
  birth with any chart, including charts earlier items created. On the long
  stream, 151 of 252 are `clean` items that met a namesake: the 300-surname
  pool fills up.
- **Record false stops:** 2, both `shared_dob` items whose first name equals
  that of the chart with the same date of birth. The married-name rule stops
  them for a person, by design.
- **Missed, both checks:** all 12 are the held-out `heldout_married_dob_typo`.
  A married name with a mistyped date of birth shares neither the last name
  nor the date of birth with the chart, so neither check sees it. The other
  held-out kinds (first-name and last-name typos) were caught by the record
  check, and by the screen through the shared date of birth.
- **Record decisions** on the long stream: 450 `none`, 37 `exact`, 63
  `close`, 13 `unreadable`. The `unreadable` ones are the 13 referrals with
  no date of birth, a right stop.

## Limits

- Offline: the table has FHIR string-search semantics (a case-insensitive
  last-name prefix, an exact birth date). Not yet run against live OpenEMR's
  FHIR or REST search; that run needs the fixture slot (`dup-check-sor` in
  the queue). The paging and fail-closed behavior is tested against a real
  local HTTP server (`tests/test_duplicate_record_check.py`).
- The close-match rules were written with the soak's generator in view
  (`ambiguous_same_person` changes the day of birth). Only the held-out kinds
  are a test of rules not tuned to them.
- One seed. The screen rule is the `no_duplicate_candidate` rule applied to
  OpenEMR's listing as the soak describes it (any chart sharing the first
  name, the last name or the date of birth); it is not a run of the browser.
