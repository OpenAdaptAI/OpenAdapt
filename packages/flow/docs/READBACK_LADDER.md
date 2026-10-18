# How a save was checked

Every save a run makes reaches one rung. The run's report records the rung
per save (`readback.saves`) and for the run (`readback.reached`, the weakest
save). The agent result carries it as `checked_how`.

| Rung | `checked_how` | Label | What happened | Tiers |
|---|---|---|---|---|
| R0 | `not_checked` | Not checked | Nothing confirmed the save | none |
| R1 | `screen_said_saved` | The screen said it saved | The value was read on the screen the save left | 4 |
| R2 | `reopened` | Reopened and checked on screen | The record was opened again, by another path, and read | 3, 2 |
| R3 | `records` | Checked in your system's records | An API, database or file read found the record | 1 |

## The minimum

A workflow sets `minimum_readback` (`R1`, `R2`, `R3`; default `R2`). A run is
`done` only when every save reaches it. A save confirmed below it ends
`COMPLETED_UNVERIFIED`, and the agent says `not_sure_if_saved` with reason
`checked_below_minimum`: a person checks the record. `R1` counts only when the
workflow sets it, and then the same-screen check is wired with no connector.

`qualify` enforces the same minimum before the workflow ships
(`minimum_readback` in `verify.yaml`, or the older `minimum_effect_strength`).
An execution profile can only raise it: Regulated still needs tier 2.

## How Flow reopens a record without configuration

From the demonstration, the compiler learns:

- **A search by this run's input.** After the save, the person typed one of
  the run's inputs into a search and submitted it. The check replays that
  search with the run's value. It must start with a reset Flow recognizes (a
  submitted search, a reload, a reopened window), and it confirms nothing
  until a person admits it.
- **A search by the reference number.** The save's screen showed exactly one
  new number (an encounter, claim or confirmation number) that the person did
  not type. The check reads that number from the same place after the run's
  save and types it into the search. No number, or two, halts the check. The
  number is never written to the report.
- **A read-only guard.** No step of a reopen may press a save, submit or
  delete key or click a control labelled with a commit word. See
  [`LIMITS.md`](LIMITS.md) for what the guard can't see.

A separate browser session (`ui_readback`) can also open the record by the
number the save assigned: `ui_readback.reference` reads it from the run's
page URL or screen and fills it into `record_url`.

## Not in this version

Reopening a desktop or RDP/Citrix record in a fresh session (relaunching the
app, or a second session with its own credentials) is not built. Those
surfaces reach R2 by reopening in the acting session.
