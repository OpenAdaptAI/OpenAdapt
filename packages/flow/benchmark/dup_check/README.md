# Duplicate check: screen against record, offline

In the production soak (#1513), the duplicate check was the largest operator
cost after approvals. It stops a new patient who shares a last name or date
of birth with an existing chart. This measures the check that asks the system
of record instead (`runtime/checks/duplicate_record.py`) against the screen
rule, on the same stream and the same patient table.

```bash
python -m benchmark.dup_check.measure --out DIR   # per-item rows, kept out of the repo
```

The per-item rows of the run below are private, in openadapt-corpus
(`reliability_recipes/verification_coverage/dup_check_offline-2026-10-24/`);
this README keeps the aggregate tables. No app, no model calls. `measure.py` says exactly what each check sees. The
record check reads an in-memory table with FHIR search semantics
(`TableSource`): a case-insensitive name prefix and an exact birth date.

## The exact numbers

A miss is a second chart for a patient who already has one.

**Test set:** 500 items from the soak's seed-7 generator
(`lifecycle.workload.generate(7, 500, 10)`), with planted duplicates and extra
charts: 694 items, 440 new patients and 241 true duplicates.
- 47 duplicates come from the generator itself: 12 existing patients referred
  again, 19 resends, 10 same people with a mistyped date of birth, and 6
  registered by another clerk moments before.
- 194 are planted variants of existing patients.

The set grew in four rounds. The first report's "12 of 110" was round one;
every number here is on the full 241.

| | screen check | record check, first version | record check, now |
|---|---:|---:|---:|
| missed, of 241 | 42 | 90 | 24 |
| missed, of the 47 from the generator | 0 | 0 | 0 |
| false stops, of 440 new patients | 252 (36.3 per 100 items) | 2 (0.3) | 2 (0.3) |

The soak's own 55 items, with the same planting (249 items, 202 true
duplicates): screen 42 missed and 10 false stops; record 24 missed and 0
false stops.

**Every kind** (planted count; missed by the screen, by the first version of
the record check, and by the record check now):

| kind | planted | screen | v1 | now | what catches it now |
|---|---:|---:|---:|---:|---|
| from the generator (`dup_existing`, `dup_resend`, `ambiguous_same_person`, `concurrent_same`) | 47 | 0 | 0 | 0 | exact match, or same name with the date one field off |
| married name (`planted_married`) | 12 | 0 | 0 | 0 | same first name and date of birth |
| married name, new address (`pl_married_moved`) | 12 | 0 | 0 | 0 | same first name and date of birth |
| married name + mistyped day (`heldout_married_dob_typo`) | 12 | 12 | 12 | 0 | first name, a near date, a shared phone, street or email |
| hyphenated name (`planted_hyphenated`) | 12 | 0 | 0 | 0 | a shared surname part |
| second surname, "Garcia Lopez" for "Garcia" (`pl_second_surname`) | 6 | 0 | 0 | 0 | a shared surname part |
| day and month swapped (`planted_swapped_dob`) | 3 | 0 | 0 | 0 | same name, swapped date |
| same name, mistyped day (`pl_ambiguous_same_person`) | 12 | 0 | 0 | 0 | same name, date one field off |
| year digits transposed (`r2_year_transposed`) | 11 | 0 | 0 | 0 | same name, date one field off |
| typo in the first name (`heldout_first_typo`) | 12 | 0 | 0 | 0 | a similar name, same date |
| typo in the last name (`heldout_last_typo`) | 12 | 0 | 0 | 0 | a similar name, same date |
| typo in the last name + mistyped day (`rv_last_typo_dob_typo`) | 12 | 12 | 12 | 0 | same first name, similar last name, a near date |
| initial for the first name (`pl_initial`) | 12 | 0 | 12 | 0 | the initial rule |
| nickname, Bob for Robert (`r2_nickname`) | 6 | 0 | 6 | 0 | the nickname table |
| nickname + mistyped day (`rv_nickname_dob_typo`) | 6 | 0 | 6 | 0 | the nickname table, same last name, a near date |
| first and last name swapped (`r2_name_swap`) | 12 | 0 | 12 | 0 | the crossed names |
| both names misspelled, same contact (`pl_both_typos_same_contact`) | 12 | 0 | 0 | 0 | similar names, same date |
| prior name on the chart, moved, mistyped day (`r2_prior_name_moved_dob_typo`) | 6 | 6 | 6 | 0 | the chart's birth name |
| married name, moved, mistyped day (`limit_married_moved_dob_typo`) | 12 | 12 | 12 | **12** | known limit |
| new first and last name, moved, same day (`limit_new_name_moved_same_dob`) | 12 | 0 | 12 | **12** | known limit |

The rows add to 241. Every match the record check makes is `close` or
`exact`, and both stop the item for a person.

**Known limit: 24 misses, uncatchable without extra data.**
- 12 are a new last name with a new address, phone and email, and a mistyped
  day of birth. They share only the first name with the chart.
- 12 are new first and last names with new contact details. They share only
  the date of birth, as hundreds of patients do.

Catching them needs an MRN, an insurance member id, or a person. The screen
stops the second kind only because it stops every new patient who shares a
date of birth with anyone. Stopping on a shared date of birth alone would
bring back the screen's `shared_dob` false stops (33 here).

**The trade-off.**
- Misses fell from 90 (first version) to 24, and all 24 are the known limit.
- False stops stayed at 2 of 440 new patients. Both are `shared_dob` items
  whose first name equals that of the chart born that day, which the
  married-name rule sends to a person on purpose.
- One rule was narrowed along the way. "Another name, one shared contact
  detail and a near date of birth" stopped two new patients, because the
  generator's phone pool has only 100 numbers (`555-01xx`). It now needs two
  shared contact details for a near date, or one for the same date. That
  narrowing is tuned to this stream.

## Live OpenEMR (2026-10-24)

`live.py` on the pinned OpenEMR 8.0.0.3 fixture. It seeded 23 synthetic
charts, 13 of them born on one day, and probed both channels. Raw output
(`probe.json`, `decisions.json`) is private, in openadapt-corpus
`reliability_recipes/verification_coverage/live-2026-10-24/dup_check/`.

What OpenEMR does:

| | REST `/api/patient` | FHIR `Patient` |
|---|---|---|
| page size | `_limit` honored | `_count` honored |
| next page | `links.next`, but built on OpenEMR's configured site address, not the address the request went to; no total | no next link at all, and `Bundle.total` equals the rows returned, so `_count=5` silently returns 5 of 13 |
| with no page size | all 120 of 120 rows sharing a birth date | all 120 of 120 |
| `lname` / `family` | prefix match (`Hartwell` finds `Hartwell1`...) | prefix match |
| birth name | not exposed | a birth name isn't returned as an old name; searching it finds nothing |

The check's decisions on six referrals with known answers:
- exact;
- a married name;
- a nickname;
- a swapped day and month;
- a new patient sharing only the birth date;
- a namesake.

| channel | right | notes |
|---|---:|---|
| FHIR, as the check sends it (no `_count`) | 6 / 6 | |
| REST, 500 rows a page | 6 / 6 | |
| REST, 5 rows a page | 4 / 6 | the two that needed a second page were `unreadable`: the next link points to the site address, another origin, so the check refused it (fail closed) |

So:
- REST stays opt-in. Its paging only works where OpenEMR's site address is
  the address the check reads.
- `FhirPatientSource` never sends `_count`. OpenEMR returned all rows without
  it (up to 120 tested), and the total can't reveal a cap, so a larger result
  set than that is untested.
- Prior names are only searchable through the SQL adapter.

## How the set was built

For every existing chart, `plant` inserts duplicates at seeded positions:
- round one: a married name, a hyphenated name and a swapped date, then the
  held-out first-name typo, last-name typo, and married name plus a mistyped
  day;
- round two (`r2_`): a crossed name, a transposed year, and nicknames and
  prior names on extra charts;
- round three (`pl_`): the six cases production-lifecycle named;
- round four (`rv_`): the two the review reproduced.

Each item meets the existing and extra charts, every earlier item's right
outcome from the soak's truth, and another clerk's chart for
`concurrent_same`.

The rules for each round were written knowing that round's kinds, so only
round one's held-out kinds were a blind test. They are why the second round
exists.

## Limits

- Offline. The channel adapters' paging and fail-closed behavior is tested
  against a real local HTTP server (`tests/test_duplicate_record_check.py`),
  not against OpenEMR.
- The search for prior names assumes a FHIR `family` search also matches a
  patient's old names. The SQL adapter does (`birth_lname`).
  `patient_history`'s previous names are not read.
- One seed. The screen rule is `no_duplicate_candidate` applied to OpenEMR's
  listing as the soak describes it: any chart sharing the first name, the
  last name or the date of birth. It is not a run of the browser.
