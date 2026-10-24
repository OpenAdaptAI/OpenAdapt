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
  value of any `candidate_params` parameter (case-insensitive). With the
  example above, a chart with the same last name or the same date of birth
  stops the item. A chart that shares only a first name doesn't.
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
- A value is recognized only in the form the run typed it. If the app shows a
  value in another form (a date as `09/09/1990` for `1990-09-09`), the check
  misses that candidate and lets the item continue. Name only parameters the
  list shows exactly as typed, and check this on the real list before
  admitting the program. OpenEMR's list shows the date of birth as typed.
