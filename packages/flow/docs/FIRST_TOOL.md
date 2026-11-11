# Turn a browser task into a tool for your AI agent

Your coding agent does a browser task once. OpenAdapt records it and gives the agent a new tool that repeats the task with new values, with no model calls, and checks the result. This guide uses a small reports portal that runs on your computer, so the first run is real and nothing leaves your machine.

This version teaches read-only tasks that end in a downloaded file, such as a CSV export. While it records and when the tool runs, the browser sends no POST, PUT, PATCH or DELETE request and stays on the site you started on.

## Set up

You need Python 3.10, 3.11 or 3.12 and one of Claude Code, Cursor, Codex or Gemini CLI.

```bash
python -m pip install --upgrade openadapt
```

```bash
openadapt setup
```

`openadapt setup` finds the agents installed on this computer. For each one, it registers the OpenAdapt MCP server and installs a skill that tells the agent how to teach a task and call it again. It prints every change it makes. Run it again and it changes nothing. `openadapt setup --dry-run` lists the changes without making them, and `openadapt setup --undo` reverses them.

Restart your agent so it loads OpenAdapt.

Start the example portal in a second terminal and leave it running:

```bash
openadapt agent example-app
```

It serves made-up sales orders from 2026-01-01 to 2026-06-30 at http://127.0.0.1:8765/reports. It has date and region filters and an **Export CSV** link.

## Teach the export

**Question:** Can my agent export this report for any date range I name, without doing the clicks again each time?

**Prompt to paste:**

```text
Use OpenAdapt to turn this into a tool. Go to http://127.0.0.1:8765/reports
and export the sales report as CSV for 2026-01-01 to 2026-01-31, region East.
The from date, the to date and the region change each time. A right file has
every date inside the range, only that region, and at least one row.
```

**What you get:** The agent calls `teach_start`, works through the page with `teach_act` (open the report, type the two dates, pick the region, select **Export CSV**), and calls `teach_finish` with your checks. OpenAdapt checks the downloaded CSV, then runs the recorded steps once more with no model to show they produce the same file. The agent then has a new tool, such as `taught_export_sales`, that takes `start_date`, `end_date` and `region`.

## Call it with new values

**Question:** Does the tool work for dates it has never seen?

**Prompt to paste:**

```text
Export the sales report for 2026-03-01 to 2026-03-15, region West, and tell me
where the file is.
```

**What you get:** The agent calls the taught tool. It returns `outcome: done`, the path of a new CSV in `~/Downloads/OpenAdapt/<tool name>/`, the number of rows, and the checks that passed, for example "every date is from 2026-03-01 to 2026-03-15" and "every region is West". `model_calls` is 0: the tool replays the recorded steps without a model.

## When the tool stops

The tool never guesses. It returns `outcome: stopped` and downloads nothing when:

- a control it needs is missing, for example when the portal rejects a from date that is after the to date and shows no **Export CSV** link;
- more than one control matches, so it can't tell which one was taught;
- a control has a different label than when it was taught.

It returns `outcome: needs_review` when a file downloads but fails a check. Only `done` means the file is right.

If the page changed, ask the agent to teach the task again; it passes `replace: true` to `teach_start`.

## Use it on your own work

Point the agent at your own page the same way: give it the URL, the task, the values that change, and what a right file looks like. Things to know:

- The browser starts with no cookies. For a page that needs a sign-in, give the server the values from environment variables (`openadapt-agent serve --teach-browser --sign-in username=APP_USER --sign-in password=APP_PASSWORD`); the agent types them by name and never sees them.
- A control that would save, send or delete is refused, and a form that sends a POST is refused.
- Taught tools are stored in `~/.openadapt/tools/<name>/program.json`. Files go to `~/Downloads/OpenAdapt/`. `openadapt-agent serve --teach-browser --tools-dir DIR --files-dir DIR` changes both.
- To watch the browser while the agent works, add `--headed` to the server command in your agent's MCP settings.

## Desktop apps, RDP and Citrix, and tasks that save

Read-only teaching needs a guard that stops every write before it leaves, and only a browser has one. A desktop app, or one you reach over RDP or Citrix, writes to its store over its own connection, so OpenAdapt teaches its tasks as saves: each save is read back from the app's system of record, and a person approves each one while it is taught. The same applies to a web task that saves.

Name the app in a deployment file (its `backend` section: `windows`, `macos`, `linux`, `rdp`, `citrix` or `web`), say how to read its records in a verify file, and set up your agent for that app:

```bash
openadapt setup --app deploy.yaml --verify verify.yaml --authorization teach-authorization.json
```

The authorization is a person's signed statement of what the agent may teach (`openadapt-flow teach-authorization`); without one, nothing the agent teaches is admitted. The skill tells the agent how to teach the save, confirm its check with a person, and admit it. See [COMPILE_AGENT.md](COMPILE_AGENT.md) and [TEACH_AUTHORIZATION.md](TEACH_AUTHORIZATION.md).
