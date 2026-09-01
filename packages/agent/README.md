# OpenAdapt Agent

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)](https://www.python.org/downloads/)

`mcp-name: io.github.OpenAdaptAI/openadapt-agent`

OpenAdapt is a compiled program for GUI writes with no API. This package invokes it over MCP.

This package is the default runtime interface for a calling agent. The CLI remains. Headless does not mean the UI is gone.

Three roles: the calling agent operates; a named human is authority; compliance samples seals. Computer-use agents are the user of OpenAdapt. They are not the executor inside OpenAdapt.

Flow still runs the program. Policy, identity, verification, pauses, repair, and audit stay in [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). A healthy call goes through Flow's governed `run`. HALTED, refused, timeout, and error come back as those outcomes. Don't summarize any of them as success. Never summarize halt as success.

## Install

Claude Code / Cursor, three lines. The server generates the public synthetic tutorial at serve time. It is not in the wheel.

```bash
claude mcp add openadapt -- \
  uvx --from 'openadapt-agent[tutorial]' openadapt-agent \
  serve --allow-run
```

`--allow-run` is an explicit opt-in. Admission stays fail-closed.

The halt demo is `openadapt quickstart --break-it`. After the verified run, the same certified bundle hits a backend that paints a success banner and rejects the write. An independent system-of-record read catches the lie. The record did not change.

Local unsigned replay is free. If the tool returns unsigned success, treat it as failure. Production success without a Seal is failure. Don't name this skill "computer use".

```bash
python -m pip install --upgrade openadapt
openadapt quickstart
openadapt-agent serve --allow-run
```

Python 3.10 through 3.12. `pip install openadapt` now includes this package. `pip install openadapt-agent` is the library install if you already have a private compiled bundle. The halt demo is still `openadapt quickstart --break-it`.

```bash
uvx openadapt-agent serve --bundles /path/to/bundles          # read-only
uvx openadapt-agent serve --bundles /path/to/bundles --allow-run
```

## Author a first demo (Claude Code)

Local Claude Code (or Cursor, Codex, Grok CLI) is the first authoring UI
over stdio. Hosted ChatGPT.com / Claude.ai cannot talk to localhost; they
need the outbound mailbox client below, not `serve --authoring`.

```bash
claude mcp add openadapt-authoring -- \
  uvx --from 'openadapt-agent[tutorial]' openadapt-agent \
  serve --authoring
```

`--bundles` is omitted. Probe tools are `observe`, `start_record`, `click`,
and `halt`. `--authoring` does not enable run tools. This process stays
stdio. Pass `--url` to pin a fresh Playwright Chromium with empty cookies.
Windows native, Citrix, and RDP are coach-only in v1.

## Connect this computer (ChatGPT.com / Claude.ai)

OpenAdapt is installed on this computer, so an agent can drive only through
OpenAdapt.

Desktop: the tray is already running. **Connect this computer** on the job
page (`openadapt://runner`) just works.

Pip: after `pip install openadapt`, start the same mailbox client:

```bash
openadapt-agent authoring connect \
  'openadapt://runner?pack=p.…&bind=oab_…&origin=https://openadapt.ai'
```

That claims `oab_`, polls `/j/{id}/runner/poll` with `wait_seconds: 0`,
prints Allow (`y/n`) per chat account, and on pause prints `Sign in in
the app, then press Enter`. Continue records with `record_observed`. It
never types your password. Pass `--url` for a fresh Playwright Chromium
with empty cookies.

The job page should offer `openadapt connect <url>` next to Open
OpenAdapt (meta-package alias; this repo implements
`openadapt-agent authoring connect`). Overlay chrome, launchd, and the
OS URL handler stay Desktop-only. See [`docs/MAILBOX_CLI.md`](docs/MAILBOX_CLI.md).

## Serve a bundle

`--allow-run` with no `--bundles` records, compiles, and certifies the
synthetic MockMed workflow, then keeps that app up so a governed run can hit
a live system of record. `--tutorial` is the same path without implying run
tools. Private customer bundles still use `--bundles`. Those stay on the
operator's disk and are never shipped in this package.

```bash
openadapt-agent serve --allow-run --runs-dir /tmp/openadapt-runs
```

The client gets `list_workflows`, `get_workflow`, `get_run_report`,
`list_needs_attention`, and `get_attention_item`. Those are read-only until
`--allow-run`. The synthetic tutorial registers `run_local_quickstart`.

Add `--allow-run` and the server registers one typed `run_workflow_<opaque-id>`
tool per loadable bundle. Declared parameters are required. Recorded
demonstration values never appear in the schema and are not reused unless you
ask for that.

By default every MCP response is safe to render outside the protected
workflow-data boundary. Labels, recorded values, paths, raw reports, observed
text, stdout, stderr, and local exception messages stay on the OpenAdapt
machine. The client gets opaque IDs, fixed outcome copy, declared parameter
names and types, and count or boolean metrics.

Two development switches, kept apart on purpose:

- `--allow-protected-export` sends raw local metadata and evidence to the MCP
  client. Use it only when that client is trusted and inside the same
  protected data boundary.
- `--allow-synthetic-recorded-defaults` lets omitted parameters reuse recorded
  values. It requires `--allow-run`. Synthetic demonstrations only. Production
  runs require every declared parameter so a wrong-record action cannot hide
  in a default.

## Finish a halt from the same server

Point the server at Flow's qualified deployment config if a local operator
needs to finish an exception and continue the same durable run:

```bash
openadapt-agent serve \
  --bundles /opt/openadapt/bundles \
  --runs-dir /var/lib/openadapt/runs \
  --allow-run \
  --allow-attended-actions \
  --config /etc/openadapt/deployment.yaml \
  --headed
```

The queue summary is safe to show in an agent UI: opaque IDs, typed
categories, counts, and signed-capability metadata. No observed text, no
workflow values, no reports, no local paths.

| Tool | What happens |
| --- | --- |
| `continue_attention` | The operator confirms they completed the paused task in the live app. Flow revalidates postconditions and independent effects, checkpoints it as human-completed, and resumes after it. It does not perform the completed action again. |
| `skip_attention` | Flow applies only an already-declared, non-consequential skip. A stale, undeclared, consequential, or ambiguous skip is refused. |
| `reject_attention` | Ends this run and dispatches no new action. Earlier steps can still have effects, so read the protected local report. Escalate if a qualified operator can still continue. |
| `teach_attention` | Records an audited request for a corrective demonstration. Flow's revision and regression gates decide what can be promoted. |
| `escalate_attention` | Records an audited escalation and leaves the exact durable pause intact. |

Every mutation needs the opaque queue-item ID, the item's current capability
digest, a caller-stable idempotency key, and an action-specific `true`
confirmation. Before the server submits that to Flow, it opens an MCP form
elicitation and the local operator has to accept it. That's a host-mediated
confirmation, not cryptographic proof of who clicked. Flow records the
effective local OS account as the operator.

Clients without form elicitation cannot execute attended actions through this
bridge. The same Continue, Skip, Reject, Teach, and Escalate capabilities stay
available through Flow's attended console:

```bash
openadapt-flow console \
  --attend \
  --allow-actions \
  --bundles /opt/openadapt/bundles \
  --runs /var/lib/openadapt/runs \
  --config /etc/openadapt/deployment.yaml \
  --headed
```

Flow rechecks the signed capability, run identity, bundle version, checkpoint
lineage, authorization, live state, and effect evidence at decision time.
Stale capabilities and uncertain delivery are refused. The same idempotency
key returns the prior terminal decision instead of repeating it.

`--allow-attended-actions` without `--config` still registers Reject, Teach,
and Escalate. Continue and Skip wait until Flow can construct the
deployment-bound live verifier and backend. `--headed` is required for web
Continue and Skip.

## MCP tools

| Tool | Registration |
| --- | --- |
| `list_workflows` | Always |
| `get_workflow` | Always |
| `get_run_report` | Always |
| `list_needs_attention` | Always |
| `get_attention_item` | Always |
| `run_workflow_<opaque-id>` | `--allow-run` |
| `run_local_quickstart` | `--allow-run` with no `--bundles` |
| `reject_attention`, `teach_attention`, `escalate_attention` | `--allow-attended-actions` |
| `continue_attention`, `skip_attention` | `--allow-attended-actions` plus a qualified deployment `--config` |
| `observe`, `start_record`, `click`, `halt` | `--authoring` |

## Run outcomes

Every `run_workflow_<opaque-id>` call returns one of these:

| `status` | Meaning |
| --- | --- |
| `success` | The process exited successfully and the persisted report records `execution_outcome: VERIFIED`. Legacy reports must record `success: true`. |
| `halt` | Execution halted, completed without enough verification, or completed a rollback. Not a verified success. If `execution_outcome` is `HALTED`, tell the user the record did not change. Protected evidence stays local. |
| `refused` | A governed admission gate refused the bundle before execution. Nothing ran. |
| `timeout` | The process exceeded its deadline. The target may be partly executed. Inspect it before retrying. |
| `error` | The CLI, report, or other execution infrastructure was inconsistent. |

`get_run_report` returns a PHI-safe status and a count-only summary for a run
this server created. The persisted report stays in the local operator
experience unless you enabled protected export. A client must never summarize
`halt`, `refused`, `timeout`, or `error` as success.

A timeout isn't a rollback. Don't retry until you've looked at the durable run.

## Emit an Agent Skill

```bash
openadapt-agent emit-skill \
  /tmp/openadapt-agent-demo/bundle \
  --out ~/.claude/skills
```

This wraps Flow's own skill emitter, keeps its portable bundle, and adds MCP
invocation, Needs Attention, and result-handling guidance. The frontmatter
description is the same sentence as `server.json` and `llms.txt`. The skill
is named from the workflow slug, never "computer use". If the tool returns
HALTED, tell the user the record did not change.

A first-party copy lives at [`skills/openadapt-gui-write/SKILL.md`](skills/openadapt-gui-write/SKILL.md).
A folder emitted from a private bundle isn't a sanitized derivative. It
includes the compiled bundle. Treat that as protected workflow data.

## Trust boundary

This is a local stdio bridge. It inherits the local user's OS permissions, and
that user is recorded as the operator for attended decisions. Don't expose its
stdin and stdout as an unauthenticated network service.

Remote transport, account identity, tenant isolation, fleet policy, and
managed execution belong to OpenAdapt Cloud. They aren't duplicated here.

Other fixed boundaries:

- Run tools and attended mutations stay off until you enable them at start.
- Target, deployment policy, timeout, and model-egress posture are fixed at
  server start, not supplied per MCP call.
- Parameters go through a mode-`0600` temporary file, not process arguments.
- Workflow and run IDs are opaque on the default MCP surface.
- Recorded parameter values never enter tool schemas, unless you put the
  server in synthetic-default demo mode.
- Protected reports, names, values, paths, subprocess output, and exception
  text stay local unless you enable protected export for a trusted client in
  the same data boundary.
- Attended action schemas accept no free-text challenge answers.
- Attended mutations require protocol-native form elicitation. A boolean from
  an autonomous tool caller is not enough on its own.

The complete contract is in [docs/DESIGN.md](docs/DESIGN.md).

## Product state

An exact Agent release enters Production only through an active signed,
expiring, and revocable release admission. A missing, expired, revoked,
mismatched, or unverifiable admission produces **not actively admitted**. The
validator doesn't restore an older admission or assign a fallback lifecycle
label. Check the
[current signed Production record](https://docs.openadapt.ai/production-lifecycle.json).

## Package history

Before v2 this repository wrapped model-driven GUI agents. That execution path
now lives in `openadapt-flow`. The current name stays because the package
bridges MCP and Agent Skills. It isn't an MCP-only package.

The public capability is the server. `serve --allow-run` generates the
synthetic MockMed bundle at serve time; it is not vendored. A user's compiled
workflow is their private artifact, supplied at launch with `--bundles` and
never embedded in the package or a registry listing. See
[`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md).

Machine-readable launch manifests sit at the repo root:
[`server.json`](server.json) (MCP registry),
[`manifest.json`](manifest.json) (MCPB / Smithery),
[`llms.txt`](llms.txt) (assistants). Registry-launched installs start
read-only. Execution tools wait for `--allow-run`.

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
