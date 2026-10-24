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

## Results (2026-10-24, second round)

The first version of the record check missed 12 of 110 planted duplicates,
all of one kind: a married name with a mistyped day of birth. In every one,
the phone, street and email matched the chart exactly. So the second round
adds:
- searches by first name, phone and email;
- a nickname table;
- swapped first and last names;
- the chart's prior names (OpenEMR's `birth_fname`/`birth_lname`, or FHIR
  `name.use` old or maiden);
- contact details (phone, email, street) as tie-breakers for a mistyped date
  of birth.

It also adds the kinds those rules target (`r2_`). Two `limit_` kinds share
nothing with the chart but the person, so no rule here can catch them. The
`r2_` rules were written knowing the `r2_` kinds, so those catches are not a
blind test.

Extended stream (500 seed-7 items, plus the planted and extra charts): 622
items, 440 new charts, 169 true duplicates.

| check | false stops (per 100 items) | missed duplicates |
|---|---:|---:|
| screen (`no_duplicate_candidate` on the list) | 252 (40.5) | 30 |
| record, first version | 2 (0.3) | 60 |
| record, second round | 2 (0.3) | 24 |

The soak's own 55 items, with the same planting (177 items, 130 true
duplicates): screen 10 false stops (5.6 per 100), 30 missed; record 0 false
stops, 24 missed.

Missed duplicates by kind, extended stream:

| kind | planted | screen | record v1 | record v2 | field that would have caught it |
|---|---:|---:|---:|---:|---|
| married name + mistyped day (`heldout_married_dob_typo`) | 12 | 12 | 12 | 0 | phone, street, email (caught by v2) |
| first and last name swapped (`r2_name_swap`) | 12 | 0 | 12 | 0 | the names, crossed (v2) |
| nickname, Bob for Robert (`r2_nickname`) | 6 | 0 | 6 | 0 | the nickname table (v2) |
| prior name on the chart, moved, mistyped day (`r2_prior_name_moved_dob_typo`) | 6 | 6 | 6 | 0 | the chart's birth name (v2) |
| year digits transposed (`r2_year_transposed`) | 11 | 0 | 0 | 0 | the date of birth, one field off |
| married name, moved, mistyped day (`limit_married_moved_dob_typo`) | 12 | 12 | 12 | 12 | none in the referral: needs an MRN, an insurance id or a person |
| new first and last name, moved, same day (`limit_new_name_moved_same_dob`) | 12 | 0 | 12 | 12 | none in the referral; the screen stops it only because it stops every shared date of birth |

**The trade-off.** Misses fell from 60 to 24, and every remaining miss is a
`limit_` kind, while false stops stayed at 2 of 440 new charts. Both are
`shared_dob` items whose first name equals that of the chart with the same
birth date, which the married-name rule sends to a person on purpose. The
screen still stops `limit_new_name_moved_same_dob`, and the record check
doesn't: a full name change and a move, with nothing but the birth date in
common, looks like any new patient who shares a birthday. Stopping it would
mean stopping every shared date of birth, which is 33 of the screen's 252
false stops.

One rule was narrowed during this round. "Another name, one shared contact
detail and a near date of birth" stopped two new patients, because the
generator's phone pool has only 100 numbers (`555-01xx`). It now needs two
shared contact details for a near date of birth, or one for the same date.
That narrowing is tuned to this stream, and a real phone pool is far larger.

## Limits

- Offline: the table has FHIR string-search semantics (a case-insensitive
  last-name prefix, an exact birth date). Not yet run against live OpenEMR's
  FHIR or REST search; that run needs the fixture slot (`dup-check-sor` in
  the queue). The paging and fail-closed behavior is tested against a real
  local HTTP server (`tests/test_duplicate_record_check.py`).
- The close-match rules were written with the soak's generator in view
  (`ambiguous_same_person` changes the day of birth). Only the held-out kinds
  are a test of rules not tuned to them.
- The search for prior names assumes OpenEMR's FHIR `family` search also
  matches a patient's old names. The SQL fallback does (`birth_lname`); the
  live run must confirm FHIR. `patient_history`'s previous names are not read
  yet.
- One seed. The screen rule is the `no_duplicate_candidate` rule applied to
  OpenEMR's listing as the soak describes it (any chart sharing the first
  name, the last name or the date of birth); it is not a run of the browser.
