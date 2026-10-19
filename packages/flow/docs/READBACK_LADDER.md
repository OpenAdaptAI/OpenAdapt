# How a save was checked

Every save a run makes is checked at one check level
([`check_level.py`](../openadapt_flow/check_level.py)). The run's report
records the level for each save (`readback.saves`) and for the run
(`readback.checked_how`, the weakest save). The agent result carries it as
`checked_how` and `checked_how_label`.

| `checked_how` | Label | What happened | Tier |
|---|---|---|---|
| `unchecked` | Not checked | Nothing confirmed the save | none |
| `same_screen` | The screen said it saved | The value was read on the screen the save left | 4 |
| `reopened` | Reopened in the same session | The acting session reset its state, found the record again and read it | 3 |
| `separate_session` | Checked in a separate session | A separately signed-in session opened the record and read it (`ui_readback`) | 2 |
| `system_of_record` | Checked in your system's records | An API, database, FHIR or file read found the record | 1 |

## What makes a run VERIFIED

`VERIFIED` means every save was read back independently of the acting
session: `separate_session` or `system_of_record`. That is the floor for both
production profiles, Standard and Regulated.

`same_screen` and `reopened` are reported with their own labels and never
make a run `VERIFIED` by themselves. A run whose saves were all confirmed, but
at least one only at those levels, ends `COMPLETED_UNVERIFIED`. The report sets
`readback.below_minimum`, and the agent says `not_sure_if_saved` with reason
`checked_below_minimum`: a person checks the record.

A reopen counts toward `VERIFIED` only under an execution profile whose
contract sets `reopened_counts_as_verified`. No built-in profile does.

A workflow can raise the floor with `minimum_readback`: `separate_session`
(the default) or `system_of_record`. It can't lower it. `qualify` enforces the
same minimum before the workflow ships (`minimum_readback` in `verify.yaml`,
or the older `minimum_effect_strength`), and a workflow that sets it also
refuses to run with a weaker verifier.

## How Flow reopens a record without configuration

A reopen earns the `reopened` level only when its navigation contains a
state reset Flow recognizes (a submitted search, a reload, a reopened
window). Without one the read could come from a form buffer or a page the
save left behind, so it counts as `same_screen`, however many steps it has.

From the demonstration, the compiler learns:

- **A search by the reference number.** The save's screen showed exactly one
  new number (an encounter, claim or confirmation number) that the person did
  not type. The check reads that number from the same place after the run's
  save and types it into the search. No number, or two, halts the check. The
  number is never written to the report.
- **A search by this run's input.** After the save, the person typed one of
  the run's inputs into a search and submitted it. The check replays that
  search with the run's value. A search for the patient finds the patient's
  latest matching record whether or not this save landed, so it confirms only
  when the save showed a reference number and the record it opens shows that
  number and no other. Otherwise the check halts: an older note with the same
  text must not confirm a save that failed.
- **A read-only guard.** No step of a reopen may press a save, submit or
  delete chord, click a control labelled with a commit word, or click where
  no label can be read. See [`LIMITS.md`](LIMITS.md) for what the guard
  can't see.

Learned checks confirm nothing until a person admits them
(`openadapt-flow check admit`).

A separate browser session (`ui_readback`) can also open the record by the
number the save assigned: `ui_readback.reference` reads it from the run's
page URL or screen and fills it into `record_url`.

## Not in this version

Reopening a desktop or RDP/Citrix record in a fresh session (relaunching the
app, or a second session with its own credentials) is not built. Those
surfaces reach `reopened` at most, which is not `VERIFIED`.
