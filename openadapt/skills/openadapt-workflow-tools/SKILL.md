---
name: openadapt-workflow-tools
description: "Turn a task the person repeats in an app (a report export, a lookup, or a save to a record) into a tool you call again with new values. Works on web apps, desktop apps on Windows, macOS and Linux, and apps over RDP or Citrix. Use when the person asks to automate, repeat, or schedule a task in an app."
---

# Turn an app task into a tool

OpenAdapt's MCP server (`openadapt`) records a task you do once in an app and turns it into a tool named `taught_<name>`. Calling the tool repeats the task with new values and no model calls, then returns a checked result: a downloaded file, or a save verified in the app's system of record.

## Which tasks this server teaches

Read `teach_start`'s description and inputs before you ask the person anything. They tell you what this server was set up for:

- **`teach_start` takes a `url`:** this server teaches read-only tasks in a web app, ones that end in a download such as a CSV export. The browser sends no POST, PUT, PATCH or DELETE request and stays on the site you start on, so nothing in the app can change.
- **`teach_start` takes `record_fields` and `on_screen`:** this server teaches saves in the one app the operator set it up for: a web app, a desktop app, or an app over RDP or Citrix. Run 1 is a real save, so use a test record. Every save is read back from the app's system of record, and on a desktop or remote app a person approves each save in your client's form.

Read-only teaching needs a guard that stops writes before they leave. Only a browser has one, so read-only tasks are taught in web apps. A desktop or remote-session task is taught as a save, checked in the system of record.

If the person's app or task doesn't match what this server teaches, say so and stop. The operator sets up the app; you can't change it.

## Before you start, ask the person

1. **Where:** for a read-only web task, the page's URL. For a save, nothing: the server opens the app it was set up for.
2. **What:** the task in one sentence, for example "export the sales report as CSV" or "add this note to the open chart".
3. **What changes each time:** give each changing value a short name and this time's value, for example `start_date: 2026-01-01`. Every value must be different.
4. **What proves it worked:**
   - For a download: the file must download. Ask whether a column must fall between two of the values, whether a column must equal one value, and the fewest rows a right file has.
   - For a save: the record it belongs to (`record_fields`, such as `patient_id: p1`) and how the screen shows that record (`on_screen`, such as the name and MRN on the patient banner).

Don't ask for passwords. Sign-in values are held by the server: type them with `input: "<name>"` from `sign_in_inputs`; you never see them.

## Teach it

1. Call `teach_start` with what it asks for.
2. Read the app. `teach_observe` lists the controls with refs such as `[e3]`. When it lists none, the app is shown only as pixels (a remote session, for example): call `teach_screen`, if the operator turned it on, and act at `x`, `y` on what you see.
3. Call `teach_act` one action at a time, with an action from its `action` list (such as `click`, `type` or `press`). Type each changing value with `input: "<name>"`, not as text, so the tool takes it as a parameter. Refs change after every action; use the newest listing.
4. Finish:
   - Download: when `teach_act` returns `downloaded`, call `teach_finish` with the person's checks, for example `{"between": [{"column": "date", "from": "start_date", "to": "end_date"}], "min_rows": 1}`. When the steps reproduce the file with no model, it returns `status: ready` and `new_tool`.
   - Save: after the save, call `teach_finish`. It reads the system of record and returns a draft. Call `teach_confirm_checks`; the person confirms the check in your client's form. Then call `teach_admit` with `canary_inputs` that are safe to save on that record. When the canaries pass, the server adds the tool. Without an admission policy, it tells you the command a person runs instead.
5. Tell the person the tool's name and its inputs.

If `teach_act` refuses an action, don't work around it. The action would change data in a read-only task, it isn't on the task's record, or a person didn't approve it. Tell the person why.

## Call it

Call `taught_<name>` with `inputs`. Act on `outcome`:

| outcome | What happened | What to do |
| --- | --- | --- |
| `done` | The file downloaded and passed its checks, or the save is verified in the system of record (`verified: true`, `receipt_sha256`). | Give the person the `file` path or the receipt. |
| `needs_review` | The file downloaded but failed a check (`checks_failed`), the run stopped before saving (a person holds it), or the save was not verified (`REFUTED` or `RECONCILIATION_REQUIRED`). | Don't use the file, and never retry the save. Tell the person `what_happened`. |
| `stopped` | A step could not be done safely: for example a control is missing or matches more than one element. Nothing was downloaded. | Tell the person `what_happened`. If the page changed, teach the task again with `replace: true`. |
| `not_sure_if_saved` | The save may have gone through, but it could not be checked. | Never retry. Tell the person to look at the record before anything else. |
| `did_not_run` | The inputs were wrong, or the app could not start. | Fix the inputs and call again. |

Only `done` is a success.
