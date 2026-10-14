# Claude Code calls a recorded workflow

This example records one form fill in a small expenses app, compiles it with
openadapt-flow, and serves it to Claude Code as an MCP tool through
[openadapt-agent](https://github.com/OpenAdaptAI/openadapt-agent). Claude Code
passes a description and an amount. OpenAdapt fills in the form in a headless
browser, with no model calls, and then reads the app's SQLite table itself. The
tool returns `success` only when exactly one new row with those two values is
there.

![Claude Code adds an expense through the tool, then the new row is read from SQLite](demo.gif)

## Run it

You need macOS or Linux, Python 3.10 to 3.12 (or [uv](https://docs.astral.sh/uv/)),
and [Claude Code](https://code.claude.com/docs) for the last step. The OpenAdapt
side makes no model calls and needs no API key.

```bash
./demo.sh                       # install, start the app, record, compile
claude --mcp-config mcp.json    # then ask: Add an 18.75 expense for parking
./demo.sh rows                  # the newest rows, read from SQLite
```

The first `./demo.sh` installs `openadapt-flow[browser]==1.35.1` and
`openadapt-agent==2.0.1` into `.venv` and downloads Chromium for Playwright
(about 150 MB). After that, a rebuild takes about 15 seconds. The app listens
on port 8642 (set `PORT` to use another one) until `./demo.sh down`.

To record the demonstration yourself in a visible browser, run
`./demo.sh --by-hand`. To send one prompt without the interactive session, run
`./demo.sh ask "Add an 18.75 expense for parking"`.

## What happens

1. `app.py` serves a form at `http://127.0.0.1:8642/` and writes each expense to
   `work/expenses.db`. It uses only the Python standard library and knows
   nothing about OpenAdapt.
2. `build.py` fills in the form once in a headless browser, through Flow's
   recorder (`--by-hand` lets you do it instead). While it records, it reads
   the `expenses` table (read-only) to see which row the save added.
3. The compiler turns the recording into a 5-step program with two parameters,
   `description` and `amount`. From the row it saw appear, it proposes a check
   with the recorded values ("Team lunch", "42.50") written in.
   [`effect_contract.yaml`](effect_contract.yaml) replaces that proposal with a
   reviewed check that uses the parameters: after the click on **Add expense**,
   exactly one new row must have the given description and amount.
4. `openadapt-flow certify` checks the program against
   [`policy.yaml`](policy.yaml). Before each click, the program checks that the
   target still matches the recording. For **Add expense**, that includes the
   "Filed under Dana Demo, Engineering" text next to the button.
5. `mcp.json` starts `openadapt-agent serve`, which exposes a
   `run_workflow_<id>` tool with `description` and `amount` parameters, plus
   read-only tools such as `get_run_report`. Each call runs
   `openadapt-flow run`, which fills in the form and then runs the SQL check.
   In our runs, the browser part took about 12 seconds per call.

## When the app says "saved" but doesn't save

```bash
./demo.sh break                 # the app keeps saying "Expense saved." but writes nothing
claude --mcp-config mcp.json    # ask for another expense
./demo.sh rows                  # no new row
./demo.sh fix                   # back to normal
```

The page shows the same success banner, so a check that reads the screen would
pass. The SQL check finds no new row, so the tool returns `halt` instead of
`success`.

![The same call against the broken app returns halt, and the table doesn't change](halt.gif)

## Files

| File | What it is |
| --- | --- |
| `app.py` | The expenses app (standard library and SQLite) |
| `build.py` | Records, compiles, attaches the SQL check, certifies, and writes `mcp.json` |
| `effect_contract.yaml` | The reviewed check that runs after the save |
| `policy.yaml` | The rules the compiled program must pass before it runs |
| `demo.sh` | Installs, starts and stops the app, and runs the build |
| `demo.tape`, `halt.tape` | [VHS](https://github.com/charmbracelet/vhs) scripts for the two recordings |

Everything the scripts create goes into `work/`, `.venv/`, and `mcp.json`.
`./demo.sh clean` removes them. In an openadapt-flow checkout, run it before
`scripts/check_release_consistency.py`, which rejects those generated files.

## Limits

- The runs use Flow without an execution profile. The Standard profile also
  needs a signed qualification for the workflow, which this local demo doesn't
  create.
- `mcp.json` passes `--allow-protected-export`, so Claude Code can read the full
  local run report, including the typed values. The data here is made up.
  Without that flag the tool returns only the status and step counts.
- The bundle isn't encrypted at rest (`--allow-unencrypted`).
- The SQL check compares text. The app stores the amount as typed, so `42.5`
  and `42.50` are different values.
- In our tests, a restyle that changed only colors and borders still ran. One
  that also renamed the form's element ids halted at the first click. Record
  again after a change like that.
- Whether Claude Code asks before it calls the tool depends on your permission
  mode. The recordings used auto mode, which allowed the call.
- Tested on macOS (Apple silicon) with Python 3.12, openadapt-flow 1.35.1,
  openadapt-agent 2.0.1, and Claude Code 2.1.299.

To make new recordings after `./demo.sh`, run `vhs demo.tape` and
`vhs halt.tape`.
