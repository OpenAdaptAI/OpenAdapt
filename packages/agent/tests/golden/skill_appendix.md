
## Invoking via MCP (openadapt-agent)

If the operator runs the local `openadapt-agent` MCP server over this
skill's bundle directory:

```bash
openadapt-agent serve --bundles <path-to-this-skill-folder>/bundle --allow-run
```

this workflow appears as the opaque MCP tool `run_<opaque-workflow-id>`
(required parameters: `note`). Recorded demonstration values are
never placed in the tool schema and every declared parameter is required
by default. Prefer the MCP tool when it is available: it executes
through the governed `openadapt-flow run` path (fail-closed admission
gates) rather than the permissive `replay` demo path, and it returns a
PHI-safe structured outcome instead of raw CLI output. Inspection and
PHI-safe Needs Attention tools are always available; `run_<opaque-workflow-id>` exists only
when the operator started the server with `--allow-run`.

## Halt semantics (IMPORTANT)

A run has exactly one of these outcomes — report it faithfully:

- **success** — exit code 0 AND `report.json` marks the run successful.
  Only then may you tell the user the workflow completed.
- **halt** (exit code 1 / MCP status `halt`) — the run executed and
  STOPPED at an unhandled state. This is the system working as designed
  (halt instead of guessing), but it is NOT a success: the workflow's
  effect did not fully happen. Protected evidence remains in the local
  OpenAdapt operator experience; default MCP results contain only opaque
  IDs, fixed messages, and count/boolean metrics. Surface the halt to the
  user; do not retry blindly and never claim success.
- **governed refusal** (exit code 2 / MCP status `refused`, `openadapt-flow
  run` only) — an admission gate refused the bundle before execution;
  NOTHING was executed. The printed coverage report names the failing
  gate.

Never summarize a halted, refused, or timed-out run as if it succeeded.

## Needs Attention

When a run halts, `list_needs_attention` and `get_attention_item` return
an opaque, PHI-safe queue card. Do not ask for or place credentials,
challenge answers, screenshots, observed text, or other protected values
in an attended-action payload.

If the operator enabled attended actions, use only the exact action tool
that matches their explicit decision:

- `continue_attention` only after the local operator says they completed
  the paused task in the live application. Flow revalidates the outcome
  and resumes after it; it never performs that completed action again.
- `skip_attention` only for an allowed, declared skip.
- `teach_attention` to request a corrective demonstration.
- `escalate_attention` to preserve the pause for qualified assistance.

Reload the item immediately before acting, pass its exact capability
digest, and use one stable idempotency key for retries of that same
request. The MCP server will separately elicit the person's confirmation;
a client without form elicitation must direct the operator to Flow's
attended console/CLI instead. Elicitation is host-mediated explicit
confirmation, not cryptographic human-presence or identity proof. Never
infer or answer that elicitation yourself, auto-retry an uncertain
delivery, or substitute one action for another.
