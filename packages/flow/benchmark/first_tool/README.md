# First tool from a real coding agent

One measured run of the [FIRST_TOOL.md](../../docs/FIRST_TOOL.md) guide, on 2026-10-24, with a real agent.

## Setup

- Agent: Claude Code in print mode (`claude -p`), model `claude-opus-5-5`. The agent could call only the OpenAdapt MCP tools (`--allowedTools "mcp__openadapt__*"`), and the `openadapt-workflow-tools` skill was installed in its project folder.
- Server: `openadapt-agent serve --teach-browser` from this branch, headless Chromium.
- App: `openadapt-agent example-app --port 8799` (port 8765 was in use on the test machine).
- Prompts: the two prompts from the guide, pasted verbatim except for the port. The second prompt ran in a new `claude -p -c` process, so the server loaded the taught tool from disk.
- The host's load average was above 80 during the run, which slows every step.

## Turn 1: teach

The agent loaded the skill, then called `teach_start` (`start_date` 2026-01-01, `end_date` 2026-01-31, `region` East), `teach_act` five times (type From date, type To date, select Region, click Show report, click Export CSV), and `teach_finish` with `between` date, `equals` region and `min_rows` 1.

| Measure | Value |
| --- | --- |
| Result | `status: ready`, tool `taught_export_sales_report` |
| Checks on run 1's file | columns match; 18 rows; every date from 2026-01-01 to 2026-01-31; every region East |
| Replay with no model | same bytes as run 1, 2.1 s |
| Agent turns | 11 |
| Agent time | 43.7 s (50 s wall clock, including client start) |
| Agent cost | $0.25 |

## Turn 2: call with new values

The agent called `taught_export_sales_report` with 2026-03-01, 2026-03-15 and West.

| Measure | Value |
| --- | --- |
| Outcome | `done` |
| File | `sales_2026-03-01_to_2026-03-15.csv`, 11 rows, identical to the portal's orders for that range and region |
| Checks | columns match; 11 rows; every date from 2026-03-01 to 2026-03-15; every region West |
| Model calls by the tool | 0 |
| Tool time | 11.6 s |
| Agent turns | 3 |
| Agent time | 30.1 s (37 s wall clock) |
| Agent cost | $0.30 |

## An earlier run

An earlier run the same day, before the `equals` check existed, also ended `ready` and `done` (35 s and 26 s of agent time). In that run the agent pointed out that the tool did not check the region column. That led to the `equals` check.

## Not measured

- How long `pip install openadapt` and the first Chromium download take.
- Cursor, Codex and Gemini CLI running the flow. `openadapt setup` was checked against the real `claude` and `codex` commands in a throwaway home folder: Claude Code reported the server as connected, and Codex listed it. The Cursor and Gemini CLI config files are covered only by tests.
- Any app other than the example portal.
