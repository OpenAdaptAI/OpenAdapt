
## Invoking via MCP (openadapt-agent, Experimental)

If the operator runs the EXPERIMENTAL `openadapt-agent` MCP server over
this skill's bundle directory:

```bash
openadapt-agent serve --bundles <path-to-this-skill-folder>/bundle --allow-run
```

this workflow appears as the MCP tool `run_demo_triage` (parameters:
`note`). Prefer the MCP tool when it is available: it executes
through the governed `openadapt-flow run` path (fail-closed admission
gates) rather than the permissive `replay` demo path, and it returns a
structured outcome instead of raw CLI output. Read-only tools
`list_workflows`, `get_workflow`, and `get_run_report` are always
available; `run_demo_triage` exists only when the operator started the server
with `--allow-run`.

## Halt semantics (IMPORTANT)

A run has exactly one of these outcomes — report it faithfully:

- **success** — exit code 0 AND `report.json` marks the run successful.
  Only then may you tell the user the workflow completed.
- **halt** (exit code 1 / MCP status `halt`) — the run executed and
  STOPPED at an unhandled state. This is the system working as designed
  (halt instead of guessing), but it is NOT a success: the workflow's
  effect did not fully happen. Evidence lives in the run directory's
  `report.json` (structured `halt` record: where it stopped, what it
  observed, what completed before) and `REPORT.md`. Surface the halt and
  its evidence to the user; do not retry blindly and never claim success.
- **governed refusal** (exit code 2 / MCP status `refused`, `openadapt-flow
  run` only) — an admission gate refused the bundle before execution;
  NOTHING was executed. The printed coverage report names the failing
  gate.

Never summarize a halted, refused, or timed-out run as if it succeeded.
