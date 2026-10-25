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

When the run's effect verifier reads a FHIR server or OpenEMR's patient API,
`no_duplicate_candidate` searches the system of record for the patient instead
of reading the on-screen list (`runtime/checks/duplicate_record.py`, a check
whose source is the record). With no such channel, the on-screen list decides,
as above.

The channels:

| Source | Scope |
|---|---|
| `FhirPatientSource` | generic FHIR R4 (`Patient` search with standard parameters); any R4 server |
| `RestPatientSource` | OpenEMR adapter: OpenEMR's standard REST `/api/patient` |
| `SqlPatientSource` | OpenEMR adapter, fallback: a read-only query on `patient_data`, wired by hand, never by default |

**One source for every path.** `Replayer` resolves the source itself, from the
effect verifier it is given (`resolve_duplicate_source`). So qualification
(`qualify`), a run (`build_replayer`), a resume and the lifecycle proof that
read the same system of record use the same check, and none of them can pick
another. `tests/test_duplicate_source_paths.py` scans every `Replayer(...)`
construction to keep it so.

**The program can pin it.** The postcondition's `candidate_source: record`
makes the record check part of the qualified program: a run with no record
channel fails the step, and never reads the screen instead.
`candidate_source: screen` keeps the list even when a channel exists. Unset,
the run's channel decides. The deployment's `runtime.duplicate_check: screen`
turns the record check off for unpinned programs; a program pinned to
`record` then fails closed.

The check searches by the date of birth, by the date of birth with day and
month swapped, by each part of the last and first name, by first name, and by
MRN, phone and email when the run has them.
It reads every page, and it decides from whole records:

| Decision | When | What happens |
|---|---|---|
| `exact` | same first name, last name and date of birth, or the same MRN | stop: the chart exists |
| `close` | same name (or a nickname, an initial, or the chart's prior name) with day and month swapped, or with the date of birth one field off and either the exact name or a shared phone, email or street; same date of birth with a similar name or a shared hyphenated part; first and last name swapped; same date of birth and first name under another last name; another name with the same date of birth and a shared contact detail, or a near date of birth and two | stop for a person; the halt names each record and why |
| `none` | no record is this patient | proceed |
| `unreadable` | unreachable, refused credentials, not a result set, a reported total that doesn't match what was read, more than 50 pages, a full REST page with no next link, a missing or non-ISO date of birth | stop (fail closed) |

Sharing only a last name, or only a date of birth, doesn't stop the item.

Measured offline on the production soak's seed-7 stream, with true
duplicates planted: `benchmark/dup_check/README.md`.

Not catchable from the referral alone: a person whose name changed, who moved,
and whose date of birth is mistyped or merely shared. Nothing the referral
carries ties them to the chart; that needs an MRN, an insurance id, or a
person.
