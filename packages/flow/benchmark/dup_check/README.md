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

## The exact numbers

A miss is a second chart for a patient who already has one.

**Test set (extended stream):** 676 items, 440 new patients and 223 true
duplicates.
- 47 duplicates come from the soak's own seed-7 generator: 12 existing
  patients referred again, 19 resends, 10 same people with a mistyped date of
  birth, and 6 registered by another clerk moments before.
- 176 are planted variants of the existing patients.

The first report's "12 of 110" was the same stream before the second and
third rounds of planted kinds were added. Each round grew the set; the misses
below are on the full 223.

| | screen check | record check |
|---|---:|---:|
| missed, of 223 | 30 | 24 |
| missed, of the 47 from the generator | 0 | 0 |
| missed, of the 176 planted | 30 | 24 |
| false stops, of 440 new patients | 252 | 2 |

The record check's 24 misses, all a known limit (uncatchable without extra
data):
- 12 `limit_married_moved_dob_typo`: a new last name, a new address, phone
  and email, and a mistyped day of birth;
- 12 `limit_new_name_moved_same_dob`: new first and last names and new
  contact details, with the same date of birth.

The referral shares nothing with the chart except, in the second kind, a date
of birth that thousands of patients share. Catching them needs an MRN, an
insurance member id, or a person. The screen stops the second kind only
because it stops every new patient who shares a date of birth with anyone.

The screen check's 30 misses:
- 12 `heldout_married_dob_typo`;
- 12 `limit_married_moved_dob_typo`;
- 6 `r2_prior_name_moved_dob_typo`.

The record check catches the first and third of these.

The soak's own 55 items with the same planting: 231 items, 184 true
duplicates (8 from the generator, 176 planted). Screen: 30 missed, 10 false
stops. Record: 24 missed (the same two limit kinds), 0 false stops.

The planted kinds not in the table below (all caught by both checks):
- `planted_married`: a married name, everything else the same;
- `heldout_first_typo` and `heldout_last_typo`: one transposed letter;
- 12 of each.

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

Then production-lifecycle named six cases that must stop. Each is planted as
a `pl_` kind:
- a married name and a new address;
- a mistyped day of birth;
- a second surname ("Garcia Lopez" against "Garcia");
- an initial ("J." for the first name);
- a crossed name;
- both names misspelled with the same phone and email.

The initial was the one gap, and a rule was added for it.

Extended stream (500 seed-7 items, plus the planted and extra charts): 676
items, 440 new charts, 223 true duplicates.

| check | false stops (per 100 items) | missed duplicates |
|---|---:|---:|
| screen (`no_duplicate_candidate` on the list) | 252 (37.3) | 30 |
| record, first version | 2 (0.3) | 72 |
| record, now | 2 (0.3) | 24 |

The soak's own 55 items, with the same planting (231 items, 184 true
duplicates): screen 10 false stops (4.3 per 100), 30 missed; record 0 false
stops, 24 missed.

Every kind, extended stream (planted count; missed by screen, record v1,
record now):

| kind | planted | screen | v1 | now | what catches it now |
|---|---:|---:|---:|---:|---|
| married name + mistyped day (`heldout_married_dob_typo`) | 12 | 12 | 12 | 0 | first name, a near date of birth, a shared phone, street or email |
| married name, new address (`pl_married_moved`) | 12 | 0 | 0 | 0 | same first name and date of birth |
| same name, mistyped day (`pl_ambiguous_same_person`, `ambiguous_same_person`) | 22 | 0 | 0 | 0 | same name, date of birth one field off |
| day and month swapped (`planted_swapped_dob`) | 3 | 0 | 0 | 0 | same name, swapped date |
| second surname (`pl_second_surname`) | 6 | 0 | 0 | 0 | a shared surname part, same first name and date of birth |
| hyphenated name (`planted_hyphenated`) | 12 | 0 | 0 | 0 | a shared surname part |
| initial for the first name (`pl_initial`) | 12 | 0 | 12 | 0 | the initial rule |
| nickname (`r2_nickname`) | 6 | 0 | 6 | 0 | the nickname table |
| first and last name swapped (`r2_name_swap`) | 12 | 0 | 12 | 0 | the crossed names |
| both names misspelled, same contact (`pl_both_typos_same_contact`) | 12 | 0 | 0 | 0 | similar names, same date of birth |
| prior name on the chart, moved, mistyped day (`r2_prior_name_moved_dob_typo`) | 6 | 6 | 6 | 0 | the chart's birth name |
| year digits transposed (`r2_year_transposed`) | 11 | 0 | 0 | 0 | same name, date of birth one field off |
| married name, moved, mistyped day (`limit_married_moved_dob_typo`) | 12 | 12 | 12 | 12 | nothing in the referral: needs an MRN, an insurance id or a person |
| new first and last name, moved, same day (`limit_new_name_moved_same_dob`) | 12 | 0 | 12 | 12 | nothing in the referral; the screen stops it only because it stops every shared date of birth |

Every match the record check makes is `close` or `exact`, and both stop.

**The trade-off.** Misses fell from 72 to 24, and every remaining miss is a
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
