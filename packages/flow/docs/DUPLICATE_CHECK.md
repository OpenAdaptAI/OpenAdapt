# A duplicate check that reads the match list

Many registration forms run a duplicate search before they save. OpenEMR's New
Patient form, for example, opens a list of existing charts that might be the
same person. A recorded program usually expects the empty list ("No matches
were found."), so any listed chart stops the item before the save.

OpenEMR lists a chart that shares only a first name with the new patient. On
the lifecycle soak's synthetic worklist, most of those stops were false: the
listed chart was clearly someone else, and a person entered the item by hand.

## The postcondition

`no_duplicate_candidate` replaces the "empty list" check on the step that
opens the search:

```json
{
  "kind": "no_duplicate_candidate",
  "text": "No matches were found.",
  "candidate_rows": "tr.oneresult",
  "candidate_frame_path": ["#modalframe"],
  "candidate_params": ["lname", "DOB"],
  "timeout_s": 10.0
}
```

It passes only when the list has loaded and names no real candidate:

- A listed row is a **candidate** when it contains every word of this run's
  value of any `candidate_params` parameter (case-insensitive), or, for a
  date, shows the same date in any format. With the example above, a chart
  with the same last name or the same date of birth stops the item, even
  under another last name (a married-name change). A chart that shares only
  a first name doesn't.
- An **empty list** passes only when its "no matches" marker (`text`) is
  shown, so a list that hasn't loaded yet never passes.
- It **fails closed**: an unreadable list (frame missing or ambiguous, or a
  backend that can't read rows), a missing or empty parameter value, or any
  candidate row all fail the step before anything is saved.

The rule only lets an item continue past rows that share none of the identity
values. A same-name patient with a mistyped date of birth still stops, and a
person decides. The rows are read from the DOM (`element_texts` on the browser
backend), never from OCR.

## Changing a program to use it

The compiler doesn't propose this postcondition yet. Changing it is a program
change, so the program needs a fresh qualification and admission. The
lifecycle soak's `production_arm.with_duplicate_list_check` makes the change
on the OpenEMR referral program, and the soak admits that version afresh
(`"duplicate_check": true`).

## Limits

- The list must render its rows together, as OpenEMR's server-rendered list
  does. A list that streams rows in one by one could pass on a partial list.
- A date (a value that is an ISO date) is compared as a date. Every date the
  row shows is read the way intake reads dates (`Apr 20, 1944`, `20.04.1944`,
  `04/20/1944`, ISO), and a row date that reads either way counts when either
  reading matches. Other values are matched word for word, so name only
  parameters the list shows as they were typed.

## Asking the system of record instead

`no_duplicate_candidate` can search the system of record for the patient
instead of reading the on-screen list (`runtime/checks/duplicate_record.py`, a
check whose source is the record).

The channels:

| Source | Scope | Used |
|---|---|---|
| `FhirPatientSource` | generic FHIR R4: `Patient` search with standard parameters, for any R4 server | whenever the run's effect verifier reads FHIR |
| `RestPatientSource` | OpenEMR adapter: OpenEMR's standard REST `/api/patient` | only for a program pinned to the record check (below) |
| `SqlPatientSource` | OpenEMR adapter, fallback: a read-only query on `patient_data` (MySQL or MariaDB `%s` parameters; `paramstyle="qmark"` for SQLite) | only when wired by hand |

**One source for every path.** `Replayer` resolves the source itself, from
its own effect verifier and the program's pin (`resolve_duplicate_source`).
So qualification, a run, a resume and the lifecycle proof that read the same
system of record use the same check:
- no construction path can choose another;
- a qualification hook can't set one;
- a verifier the resolution doesn't recognize gives no source rather than an
  error.

`tests/test_duplicate_source_paths.py` scans every `Replayer(...)`
construction to keep it so.

**The program decides, and qualification sees it.** The postcondition's
`candidate_source` is part of the qualified program:
- `record`: the record check, with the FHIR or OpenEMR REST channel. A run
  with no record channel fails the step and never reads the screen instead.
- `screen`: the on-screen list, even when a channel exists.
- unset: the FHIR channel when the run has one, else the screen.

**Parameter names.** The check reads `fname`, `lname`, `DOB`, and when
present `mrn`, `phone_home`, `email` and `street`: OpenEMR's referral names.
A program for another app maps its own with `candidate_record_fields`, for
example `{"fname": "given", "lname": "family", "DOB": "birth_date"}`.

The check searches:
- by the date of birth, and by the date with day and month swapped;
- by each part of the last and first name, and by the first name;
- by MRN, phone and email when the run has them.

It reads every page, and it decides from whole records:

| Decision | When | What happens |
|---|---|---|
| `exact` | same first name, last name and date of birth, or the same MRN | stop: the chart exists |
| `close` | see the list below | stop for a person; the halt names each record and why |
| `none` | no record is this patient | proceed |
| `unreadable` | see the list below | stop (fail closed) |

A record is `close` when it has:
- the same or a similar name, a nickname, an initial or the chart's prior
  name, with day and month swapped, or with the date of birth one field off
  and at most one name part differing (or a shared phone, email or street);
- the same date of birth with a similar name or a shared surname part;
- first and last name swapped;
- the same date of birth and first name under another last name;
- another name with the same date of birth and a shared contact detail, or a
  near date of birth and two.

A record date that is only a year or a year and month counts as no date of
birth, so a name match is `close`, never a non-match.

The check is `unreadable` when:
- the channel is unreachable, or refuses the credentials;
- the reply is not a result set, or a REST body carries any error field or
  no `data` list;
- a reported total doesn't match what was read;
- the search runs past 50 pages, or a REST page is full with no next link;
- a next-page link points to another host;
- a record has no id;
- the run's date of birth is missing or not ISO.

Sharing only a last name, or only a date of birth, doesn't stop the item.

**The stop reason.** A record-check stop reads `failed: no_duplicate_candidate
(record check via <channel>: <decision>, <reason>...)`. The channel is
`none` when a pinned program had no channel. Decision `unreadable` is a
problem with the program or its channel. `exact` and `close` are about the
item.

Not catchable from the referral alone: a person whose name changed, who moved,
and whose date of birth is mistyped or merely shared. Nothing the referral
carries ties them to the chart; that needs an MRN, an insurance id, or a
person.

Measured offline on the production soak's seed-7 stream, with true
duplicates planted: `benchmark/dup_check/README.md`.
