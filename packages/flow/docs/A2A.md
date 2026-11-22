# The A2A protocol adapter

`openadapt agent serve --a2a` serves compiled OpenAdapt programs to any agent
that speaks the [A2A (Agent2Agent) protocol](https://a2a-protocol.org/latest/specification/).
It implements A2A specification 1.0 through the official Python
[`a2a-sdk`](https://github.com/a2aproject/a2a-python) (1.3.1 or later,
Apache-2.0).

It is one transport for [agent-to-agent handoffs](HANDOFFS.md). The MCP,
HTTP and file transports need no extra package.

```bash
pip install 'openadapt[a2a]'        # or: pip install 'openadapt-flow[a2a]'
openadapt agent serve --bundles ./bundles --mode production --a2a
# Agent Card: http://127.0.0.1:8770/.well-known/agent-card.json
```

## What it serves

| A2A | OpenAdapt |
|---|---|
| Agent Card at `/.well-known/agent-card.json` | Built from the same public workflow list `list_workflows` returns. It never includes step text, recorded values or paths. |
| `supportedInterfaces` | `JSONRPC` at `/a2a/jsonrpc` and `HTTP+JSON` at `/a2a/rest`, protocol version 1.0. |
| One skill per workflow | Send a data part `{"skill": NAME, "inputs": {...}, "request_id": ID}`. |
| Skill `verify_handoff` | Send another agent's handoff and get the verdict ([HANDOFFS.md](HANDOFFS.md)). |
| `capabilities.extensions` | `https://openadapt.ai/extensions/handoff-receipt/v1`, not required ([HANDOFF_RECEIPTS.md](HANDOFF_RECEIPTS.md)). |
| Task artifact `run_result` | The run result (`outcome`, `safe_to_retry` unchanged), plus the `openadapt.handoff-document/v1` document with the run's signed receipt (`record/run/v1`) when Flow wrote one. Otherwise a text part says why no receipt is attached. |

Only outcome `done` is `TASK_STATE_COMPLETED`. `needs_review` is
`TASK_STATE_INPUT_REQUIRED`: a person at this computer decides. A message on
that task only re-checks the run; it never approves it. Every other outcome
is `TASK_STATE_FAILED`. Cancelling a task never stops a run part way through
a write; the server answers `TaskNotCancelableError`.

## When a run has no receipt

Flow writes `receipt.json` after every `openadapt-flow run` and `replay`.
Three kinds of run have none:

- **Sandbox runs.** `--mode sandbox` runs on a synthetic app, not through
  the Flow CLI.
- **Encrypted runs.** With `OPENADAPT_FLOW_AT_REST=strict`, the run folder is
  encrypted at rest and Flow writes no receipt.
- **Runs with receipts off.** Runs with `OPENADAPT_RECEIPT=off`.

For these, `get_handoff` refuses, and the A2A artifact carries the run result
without a handoff.

## Same rules as MCP

A request over A2A goes through the same gate as the same request over MCP
(`openadapt_flow/agent/contract_call.py`). That gate applies:

- the tool's input schema;
- the task ledger;
- the server's mode (runs need `--mode production`);
- the request-id ledger, so the same `request_id` never writes twice;
- intake grants;
- Flow's fail-closed admission.

Only these calls exist over A2A: `list_workflows`, `run_workflow`, `get_run`,
`get_handoff` and `verify_handoff`. Attended decisions, admission decisions
and approval answers need a person in a confirmation form, so A2A doesn't
serve them. `--authoring`, `--guard`, `--verify`, `--teach`,
`--teach-browser` and `--allow-protected-export` stay on MCP stdio.

## Network

- **Loopback by default.** It listens on 127.0.0.1:8770 by default
  (`--a2a-host`, `--a2a-port`).
- **A token off loopback.** Any other address needs `--a2a-token-file FILE`,
  a bearer token of 32 or more characters that every request must carry.
  Off loopback, the public card lists only `verify_handoff`, and the
  workflows are in the authenticated extended card. Set `--a2a-public-url`
  to the address other agents use.
- **No browser requests.** A request with a browser `Origin` header, or a
  `Host` the server doesn't answer for, is refused. A web page can't reach
  the port, even through DNS rebinding.

## Tests

`packages/agent/tests/test_handoff_e2e.py` runs the official `a2a-sdk` client
against this server over HTTP on 127.0.0.1. A program run goes through the
governed runner with Flow's run process stubbed, and Flow's receipt writer
signs the report. The tests cover:

- the receipt in the task artifact;
- a good handoff, verified;
- tampered, unknown-signer and re-stapled handoffs;
- the MCP gate's refusals, repeated over A2A;
- refusals of a browser `Origin` and a foreign `Host`;
- the token off loopback;
- a follow-up message that can't approve a run.

The same file covers the MCP tools.

The official A2A TCK was run against the sandbox server. 110 automated checks
passed and 11 failed. The Agent Card checks passed 9 of 11, with 0 failed and
2 skipped. For what failed and why, see
[benchmark/a2a_tck](../benchmark/a2a_tck/README.md).
