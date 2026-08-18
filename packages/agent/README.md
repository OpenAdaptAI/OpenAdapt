# OpenAdapt Agent

[![Lifecycle: Beta](https://img.shields.io/badge/lifecycle-Beta-2563eb)](https://github.com/OpenAdaptAI/openadapt-agent)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)](https://www.python.org/downloads/)

`mcp-name: io.github.OpenAdaptAI/openadapt-agent`

The local agent bridge for
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).
It presents compiled, governed workflows to MCP clients and Agent Skills
without turning an agent into a second automation runtime.

`openadapt-flow` remains the authority for execution, policy, identity,
verification, durable pauses, repair, and audit. This package adds two
agent-facing surfaces:

- `openadapt-agent serve` exposes compiled workflows and their local
  **Needs Attention** queue over MCP stdio.
- `openadapt-agent emit-skill` emits a portable Agent Skill for a
  compiled workflow, with the result and halt semantics an agent must
  follow.

Healthy workflow calls run through Flow's governed `run` command. A
halted or refused run is returned as a structured halt or refusal, never
as a fabricated success.

## Install

```bash
pip install openadapt-agent
```

Or run it without installing — the intended MCP-client entry point:

```bash
uvx openadapt-agent serve --bundles /path/to/bundles          # read-only
uvx openadapt-agent serve --bundles /path/to/bundles --allow-run
```

Python 3.10–3.12 is supported. The package installs a compatible
`openadapt-flow` runtime and the official MCP SDK.

## Serve compiled workflows

Create a governed local tutorial bundle through the canonical launcher, then
serve that bundle directory:

```bash
python -m pip install --upgrade 'openadapt[browser]'
openadapt quickstart --out /tmp/openadapt-agent-demo

openadapt-agent serve \
  --bundles /tmp/openadapt-agent-demo/bundle \
  --runs-dir /tmp/openadapt-runs
```

For example, register it with an MCP client that accepts a local stdio
command:

```bash
claude mcp add openadapt-workflows -- \
  openadapt-agent serve \
    --bundles /tmp/openadapt-agent-demo/bundle \
    --runs-dir /tmp/openadapt-runs
```

The client receives the read-only `list_workflows`, `get_workflow`,
`get_run_report`, `list_needs_attention`, and `get_attention_item` tools. The
quickstart stops its synthetic application after the verified tutorial run, so
this retained bundle is an inspection example, not a second runnable tutorial.

The deployment-configured example below adds `--allow-run`. It registers one
typed `run_workflow_<opaque-id>` tool per loadable bundle. Declared parameters
are required; recorded demonstration values never appear in the schema and are
not silently reused.

By default every MCP response is safe to render outside the protected
workflow-data boundary. Workflow labels and intents, recorded values,
bundle/run paths, raw reports, observed text, stdout, stderr, and local
exception messages remain on the OpenAdapt machine. The client receives
opaque workflow/run IDs, fixed outcome copy, declared parameter
names/types, and count/boolean metrics.

Two explicit development switches are deliberately separate:

- `--allow-protected-export` sends raw local metadata and evidence to the
  MCP client. Use it only when that client is trusted and inside the same
  protected data boundary.
- `--allow-synthetic-recorded-defaults` lets omitted parameters reuse
  recorded values. It requires `--allow-run` and is only for synthetic
  demonstrations; production runs require every declared parameter to
  prevent a wrong-record action.

## Connect the attended workflow

Add Flow's qualified deployment configuration to let a local operator
finish an exception and continue the same durable run:

```bash
openadapt-agent serve \
  --bundles /opt/openadapt/bundles \
  --runs-dir /var/lib/openadapt/runs \
  --allow-run \
  --allow-attended-actions \
  --config /etc/openadapt/deployment.yaml \
  --headed
```

The queue summary is deliberately safe to show in an agent UI: it uses
opaque IDs, typed categories, counts, and signed-capability metadata
without returning observed text, workflow values, reports, or local
paths. Protected evidence stays in the local OpenAdapt operator
experience.

Attended actions are separate, exact tools:

| Tool | What happens |
| --- | --- |
| `continue_attention` | The operator confirms they completed the paused task in the live app. Flow revalidates its postconditions and independent effects, checkpoints it as human-completed, and resumes after it. It does not perform the completed action again. |
| `skip_attention` | Flow applies only an already-declared, non-consequential skip. A stale, undeclared, consequential, or ambiguous skip is refused. |
| `reject_attention` | Terminates this run and dispatches no new action. Earlier run actions can still have effects, so review the protected local report and transaction outcome. Use Escalate if a qualified operator can still continue the run. |
| `teach_attention` | Records an audited request for a corrective demonstration. Flow's existing revision and regression gates decide what can be promoted. |
| `escalate_attention` | Records an audited escalation and leaves the exact durable pause intact for a qualified operator. |

Every action requires:

- the opaque queue-item ID;
- the item's exact current capability digest;
- a caller-stable idempotency key; and
- an action-specific `true` confirmation in the tool request.

Before submitting that request to Flow, the server also opens an MCP
form elicitation with action-specific language and requires the local
operator to accept and confirm it. This is a host-mediated explicit
confirmation signal, not cryptographic proof that a particular person
clicked it or proof of that person's identity. Flow separately records
the effective local OS account as the operator. Clients without form
elicitation cannot execute attended actions through this MCP bridge; the same
Continue, Skip, Reject, Teach, and Escalate capabilities remain available through
Flow's attended console/CLI. MCP destructive/idempotent/open-world
annotations give the host an additional approval signal. Neither those
hints nor the elicitation replaces Flow's signed capability, live
revalidation, idempotency, or audit contract.

The direct fallback uses the same run directory and deployment boundary:

```bash
openadapt-flow console \
  --attend \
  --allow-actions \
  --bundles /opt/openadapt/bundles \
  --runs /var/lib/openadapt/runs \
  --config /etc/openadapt/deployment.yaml \
  --headed
```

Flow rechecks the signed capability, run identity, bundle version,
checkpoint lineage, authorization, live state, and effect evidence at
decision time. Stale capabilities and uncertain delivery are refused.
Retries with the same idempotency key return the prior terminal decision
instead of repeating it.

With `--allow-attended-actions` but no deployment `--config`, the safe
Reject, Teach, and Escalate transitions remain available; Continue and Skip are
not registered until Flow can construct the deployment-bound live
verifier and backend.

## MCP tools

| Tool | Registration |
| --- | --- |
| `list_workflows` | Always |
| `get_workflow` | Always |
| `get_run_report` | Always |
| `list_needs_attention` | Always |
| `get_attention_item` | Always |
| `run_workflow_<opaque-id>` | `--allow-run` |
| `reject_attention`, `teach_attention`, `escalate_attention` | `--allow-attended-actions` |
| `continue_attention`, `skip_attention` | `--allow-attended-actions` plus a qualified deployment `--config` |

## Run outcomes

Every `run_workflow_<opaque-id>` call returns one of these outcomes:

| `status` | Meaning |
| --- | --- |
| `success` | The process exited successfully and the persisted report records `execution_outcome: VERIFIED`. Legacy reports must record `success: true`. |
| `halt` | Execution halted, completed without sufficient verification, or completed a rollback. It is not a verified success; protected evidence remains local. |
| `refused` | A governed admission gate refused the bundle before execution. Nothing ran. |
| `timeout` | The process exceeded its deadline. The target may be partially executed and must be inspected before retrying. |
| `error` | The CLI, report, or other execution infrastructure was inconsistent. |

`get_run_report` returns a PHI-safe status and count-only summary for a
run created by this server. The persisted report remains in the local
operator experience unless protected export was explicitly enabled. A
client must never summarize `halt`, `refused`, `timeout`, or `error` as
success.

## Emit an Agent Skill

```bash
openadapt-agent emit-skill \
  /tmp/openadapt-agent-demo/bundle \
  --out ~/.claude/skills
```

This wraps Flow's own skill emitter, preserves its portable bundle, and
adds MCP invocation, Needs Attention, and result-handling guidance.
The emitted skill is not a sanitized derivative: it includes the
compiled bundle and Flow-generated workflow guidance. Treat the folder
as protected workflow data and install it only into an agent/client
authorized for that same data boundary.

## Trust boundary

This is intentionally a **local stdio bridge**. The process inherits the
local user's OS permissions, and that user is recorded as the operator
for attended decisions. Do not expose its stdin/stdout as an unauthenticated
network service.

Remote transport, account identity, tenant isolation, fleet policy, and
managed execution belong to OpenAdapt Cloud. They are not duplicated in
this package.

Other fixed boundaries:

- Run tools and attended mutations are disabled unless explicitly
  enabled when the server starts.
- Target, deployment policy, timeout, and model-egress posture are fixed
  at server start rather than supplied by each MCP call.
- Parameters use a mode-`0600` temporary file, not process arguments.
- Workflow and run IDs are opaque in the default MCP surface.
- Recorded parameter values never enter tool schemas. Parameters are
  required unless the server is explicitly put into synthetic-default
  demo mode.
- Protected reports, names, values, paths, subprocess output, and
  exception text stay local unless the operator explicitly enables
  protected export for a trusted client in the same data boundary.
- Attended action schemas accept no free-text challenge answers or
  protected evidence.
- Attended mutations require protocol-native form elicitation; a boolean
  supplied by an autonomous tool caller is not sufficient on its own.
  Elicitation is host-mediated confirmation, not cryptographic human-presence
  or identity proof.
  Clients without elicitation use Flow's existing attended console/CLI;
  the capability is not converted to read-only.
- A timeout is not a rollback. Inspect the durable run before retrying.

See [docs/DESIGN.md](docs/DESIGN.md) for the complete contract.

## Package history

The pre-v2 codebase was a wrapper for model-driven GUI agents. Its
execution responsibilities now live in `openadapt-flow`; its source
history remains in this repository. The current package name is retained
because this repository bridges both MCP and Agent Skills; it is not an
MCP-only package.

## Install as an MCP server (registries)

This package is the **public, official OpenAdapt MCP capability**: inspect
a compiled bundle and, opt-in, run it under governance. A user's compiled
workflow bundle is their **private artifact** — it is supplied at launch
via `--bundles` and is never embedded in the package or any registry
listing. See [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) for that
distinction and the publish/submission plan.

Machine-readable launch manifests live at the repo root:

- [`server.json`](server.json) — official [MCP registry](https://github.com/modelcontextprotocol/registry) manifest (PyPI package, `uvx` runtime hint, stdio transport).
- [`manifest.json`](manifest.json) — portable MCPB configuration used to build the one-click local bundle accepted by [Smithery](https://smithery.ai) and MCPB-compatible desktop clients.
- [`llms.txt`](llms.txt) — a concise, link-first summary for AI assistants.

Registry-launched installs start **read-only by default**; execution
tools are registered only when the operator adds `--allow-run`.

Publishing is automated: [`.github/workflows/release.yml`](.github/workflows/release.yml)
builds, runs the license/boundary gate, and — only on a `vX.Y.Z` tag or a
published Release — ships to PyPI (Trusted Publishing, OIDC) and the MCP
registry (`mcp-publisher login github-oidc`), secret-free. It runs a dry
run (no publish) on PRs and manual dispatch. See
[`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) for the one-time founder
setup.

Documentation: [docs.openadapt.ai](https://docs.openadapt.ai).

## Development

```bash
pip install -e ".[dev]"
ruff check src tests scripts
pytest -q
python -m build
python scripts/check_release_artifacts.py dist
python scripts/check_dist.py dist/*
npx -y @anthropic-ai/mcpb@2.1.2 validate manifest.json
npx -y @anthropic-ai/mcpb@2.1.2 pack . openadapt-agent.mcpb
```

## License

MIT. See [LICENSE](LICENSE).
