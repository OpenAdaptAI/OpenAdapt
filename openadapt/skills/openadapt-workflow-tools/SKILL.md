---
name: openadapt-workflow-tools
description: "Turn a browser task the person repeats (an export, a report download, or a save checked against a write contract) into a tool you call again with new values. Use when the person asks to automate, repeat, or schedule a browser task."
---

# Turn a browser task into a tool

OpenAdapt's MCP server (`openadapt`) records a browser task you do once and turns it into a tool named `taught_<name>`. Calling the tool repeats the task with new values and no model calls, then returns the downloaded file's path after checking it.

By default it only reads: the browser sends no POST, PUT, PATCH or DELETE request, and it stays on the site you start on. Teach tasks that end in a download, such as a CSV export.

When `teach_start` lists `contract` values, the operator has set up write contracts and you can also teach a save. Pass the contract that describes the save. Run 1 is a real write, so use a test record. `teach_finish` checks run 1's write in the system of record before it adds the tool, and every call of the tool returns a verified receipt or a refusal. Sign-in values are held by the server: type them with `input: "<name>"` from `sign_in_inputs`; you never see them. Don't teach sends or deletes.

## Before you start, ask the person four things

1. **Where:** the page's URL.
2. **What:** the task in one sentence, for example "export the sales report as CSV".
3. **What changes each time:** give each changing value a short name and this time's value, for example `start_date: 2026-01-01`, `end_date: 2026-01-31`. Every value must be different.
4. **What proves it worked:** the file must download. Ask whether a column must fall between two of the values (for example `date` between `start_date` and `end_date`), whether a column must equal one value (for example `region` equals `region`), and the fewest rows a right file has.

Don't ask for passwords. If the page needs a sign-in, tell the person this version can't teach it yet.

## Teach it

1. Call `teach_start` with `name`, `url`, `task` and `inputs`.
2. Read the `page` listing. Each control has a ref such as `[e3]`.
3. Call `teach_act` one action at a time: `click`, `type`, `select` or `press`. Type each changing value with `input: "<name>"`, not as text, so the tool takes it as a parameter. Refs change after every action; use the newest listing.
4. When `teach_act` returns `downloaded`, call `teach_finish` with the person's checks, for example `{"between": [{"column": "date", "from": "start_date", "to": "end_date"}], "equals": [{"column": "region", "input": "region"}], "min_rows": 1}`.
5. `teach_finish` runs the steps again with no model. When they reproduce the file, it returns `status: ready` and `new_tool`. Tell the person the tool's name and inputs.

If `teach_act` refuses an action, don't work around it. The control would change data, or it looks like another control on the page. Tell the person why.

## Call it

Call `taught_<name>` with `inputs`. Act on `outcome`:

| outcome | What happened | What to do |
| --- | --- | --- |
| `done` | The file downloaded and passed its checks, or the write is verified in the system of record (`verified: true`, `receipt_sha256`). | Give the person the `file` path or the receipt. |
| `needs_review` | The file downloaded but failed a check (`checks_failed`), or the write was not verified (`REFUTED` or `RECONCILIATION_REQUIRED`). | Don't use the file, and never retry the write. Tell the person `what_happened`. |
| `stopped` | A step could not be done safely, for example a control is missing or matches more than one element. Nothing was downloaded. | Tell the person `what_happened`. If the page changed, teach the task again with `replace: true`. |
| `did_not_run` | The inputs were wrong, or the browser could not start. | Fix the inputs and call again. |

Only `done` is a success.
