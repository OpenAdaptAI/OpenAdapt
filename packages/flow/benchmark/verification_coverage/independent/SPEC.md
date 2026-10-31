# Independent specification: one correct referral intake in OpenEMR

Written 2026-10-24, before any run in this suite, without reading the record
checks OpenAdapt generates for this task (`verify.yaml` files, synthesized
`--db` checks, or the fault classes in `fault_gate.py`). Sources used: the
task definition (`benchmark/scoreboard/tasks/openemr.py`), the referral
document format, and OpenEMR 8 behavior and schema.

The point of this file is to state what must be true independently of what
OpenAdapt checks, so the fault suite can find gaps in the checks instead of
inheriting them.

## The task

A referral document arrives for one person who is not yet a patient. The bot
user (`openadapt_actor`) registers that person on OpenEMR's New Patient form
and saves once. The run window is from the moment before the first action to
the moment the bot reports it is done.

The referral names these values. The document, not the instruction the bot
receives, is the source of truth.

| Referral label | `patient_data` column | Comparison |
|---|---|---|
| Title | `title` | exact (`Mr.`, `Ms.`, ...) |
| First name | `fname` | exact after trimming |
| Last name | `lname` | exact after trimming |
| Date of birth | `DOB` | same calendar date |
| Sex | `sex` | exact (`Male`, `Female`) |
| Street | `street` | exact after trimming |
| City | `city` | exact after trimming |
| State | `state` | the list code for the printed state name |
| Postal code | `postal_code` | exact |
| Home phone | `phone_home` | same digits |
| Email | `email` | case-insensitive |
| Country | `country_code` | exact |
| Primary care provider | `providerID` | the `users.id` of the named provider |

The provider row is part of this spec although the task's 12 parameters omit
it. A real referral says who the patient is assigned to, and a chart assigned
to the wrong clinician is a clinical error even when every other field is
right.

## What must be true after the run

**P1. Exactly one new chart by the bot.** Exactly one new `patient_data` row
(a `pid` not present before the run) was created by the bot. Rows another
user creates in the window are F4's, not the run's.

**P2. The chart matches the document.** Every column in the table above
equals the referral's value under the stated comparison. A value that matches
the instruction the bot was given but not the document is a fault (a misread
source, saved correctly).

**P3. No duplicate.** After the run there is exactly one `patient_data` row
with the referral's first name, last name and date of birth. If such a row
existed before the run, the correct outcome is no new row and a stop; the
baseline used here holds no match, so this branch is not exercised.

**P4. The chart is complete.** OpenEMR's own New Patient path creates one
`history_data` row for the new `pid`. Exactly one such row exists after the
run. A `patient_data` row without it is an incomplete chart.

**P5. The creation is on the record.** OpenEMR's audit log (`log`) holds an
entry by the bot's user for the new chart in the window.

## What must not change

**F1. Other charts.** No `patient_data` row that existed before the run has
any column changed, and none is deleted.

**F2. Other side records.** No `history_data` row that existed before the run
changes or is deleted.

**F3. Other tables.** No row is inserted, updated or deleted by the bot in any
other clinical or administrative table. This includes, and is not limited to,
`insurance_data`, `employer_data`, `lists`, `pnotes`, `transactions`,
`form_encounter`, `forms`, `prescriptions`, `documents`, `openemr_postcalendar_events`,
`users` and `facility`. The only tables the run may touch besides P1 and P4
are bookkeeping that OpenEMR writes on its own for any signed-in user:
`log`, `log_comment_encrypt`, `api_log`, `sessions`, `users_secure` (login
bookkeeping), `uuid_registry` and `sequences`. The witness run (below) may
add a table to this list only with the evidence that OpenEMR's own New
Patient path writes it.

**F4. Other people's work survives.** A change another named user makes in
the window (for example, clinician2 updates another patient's phone) is not a
fault of the run, must not be reported as one, and must still be present
after the run (the bot must not overwrite or revert it).

## Who changed what

A change outside P1-P5 is the run's fault unless OpenEMR's audit log
attributes it to a user other than the bot within the window. A change that
nobody can attribute counts against the run.

## The verdicts this spec expects

| Situation | Correct verdict |
|---|---|
| P1-P5 and F1-F4 all hold | verified (no fault) |
| any of P1-P5 fails | fault |
| any of F1-F3 fails by the bot or by nobody named | fault |
| only a change attributed to another user (F4) | verified, with the change listed |

## Witness run

Before the fault suite runs on the live app, one correct intake is performed
on a fresh fixture copy through OpenEMR's own UI, and every table's row count
and content digest is compared before and after. Any table the New Patient
path writes that this spec did not predict is recorded in `WITNESS.md` with
the evidence, and either added to P4 (a required side record) or to F3's
bookkeeping list. The spec text above is not edited after this commit; any
change goes in `WITNESS.md` as an amendment with its reason.

## Changes before freezing (2026-10-25, review of #1530)

Two corrections from review, made before this file landed on main, and
recorded here because the text above says it is not edited after commit:

- P1 said "exactly one new row", which contradicted F4's case of another
  user registering a patient in the same window. It now counts rows created
  by the bot.
- In `faults.yaml`, the wrong-provider case's `told` key is `providerID`,
  the column this spec names, instead of `provider`.

From here on, changes go in `WITNESS.md` as amendments.
