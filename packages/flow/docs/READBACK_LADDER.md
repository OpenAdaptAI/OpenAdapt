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

## A fresh session on desktop and RDP/Citrix

`effects.kind: fresh_session` reads the record somewhere that never held
the typed value, so a form buffer or a client cache can't confirm a save
that wasn't persisted. That is tier 2, rung R2, which also meets the
Regulated profile's floor.

```yaml
effects:
  kind: fresh_session
  fresh_session:
    surface: rdp                 # or desktop
    rdp:
      host: 10.0.0.5
      username_env: CHECK_RDP_USER       # a separate, read-only account
      password_env: CHECK_RDP_PASSWORD
      acting_username_env: RUN_RDP_USER  # refused if it is the same account
    start:                       # optional: open the app in the new session
      - {action: key, key: "win+r"}
      - {action: type, text: "C:\\App\\app.exe"}
      - {action: key, key: enter}
    ready_text: Patient search
```

- **desktop** starts a new process of the app after the save: `command`
  runs on the machine Flow runs on, or `start` steps open it through the
  run's own desktop. A launch that exits within `handoff_grace_s` was handed
  to the running window (a single-instance app) and halts the check. The new
  process is stopped afterwards.
- **rdp** opens a second RDP session with the account in `username_env` and
  `password_env`, and closes it afterwards. It refuses the run's own
  account when `acting_username_env` names it. Credentials never appear in
  reasons or reports.

In the new session Flow runs `start`, waits for `ready_text`, then replays
the effect's learned reopen (the admitted search by a run input or by the
reference number, which is read in the acting session). The read-only guard
covers every step.

Limits:

- It is tested only on scripted backends over the MockMed fault server. No
  live desktop app, RDP host or Citrix session runs in CI.
- A relaunched desktop process runs as the same OS user and may share local
  caches or files with the acting process. A single-instance app that keeps
  its launcher running isn't detected.
- The learned reopen must work from the new session's start screen (for
  example a search box that is always shown); otherwise add `start` steps.
- Citrix: a second published-app session from the same broker may share a
  server-side cache. Qualify it before relying on it.
- A second concurrent session may use a license seat.
