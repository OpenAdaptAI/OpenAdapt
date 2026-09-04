# OpenAdapt Agent

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)](https://www.python.org/downloads/)

`mcp-name: io.github.OpenAdaptAI/openadapt-agent`

OpenAdapt compiles demonstrated GUI workflows into programs. This package invokes them over MCP.

`pip install openadapt` includes this package. Point a local MCP client at `openadapt-agent serve --allow-run` and the calling agent can invoke a compiled GUI program on this computer. Healthy runs make no model calls. Flow still runs the program. Policy, identity, verification, pauses, repair, and audit stay in [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). `--allow-run` is an explicit opt-in. Admission stays fail-closed.

The calling agent operates. A named human is authority. They watch if the agent clicked, then they admit. Compliance samples Seals, and that is a different job than running the program. Computer-use agents are the user of OpenAdapt. They are not the executor inside OpenAdapt.

Don't name this skill computer use.

## Install

Python 3.10 through 3.12. Local Claude Code, Cursor, Codex, and Grok CLI speak stdio MCP. Author in the chat you already use.

```bash
claude mcp add openadapt -- \
  uvx --from 'openadapt-agent[tutorial]' openadapt-agent \
  serve --allow-run
```

The server generates the public synthetic tutorial at serve time. It isn't in the wheel.

```bash
python -m pip install --upgrade openadapt
openadapt flow tutorial
openadapt-agent serve --allow-run
```

`openadapt quickstart --break-it` is the halt demo. After the verified run, the same certified bundle hits a backend that paints a success banner and rejects the write. An independent system-of-record read catches the lie. The record did not change.

Local unsigned replay is free. If the tool returns unsigned success, treat it as failure. Production success without a Seal is failure. HALTED does not mint a Seal.

A first demo over stdio uses a second MCP server. `--authoring` does not enable run tools. First-demo compile needs a human ok before the tool is callable, and that ok is the whole approval.

```bash
claude mcp add openadapt-authoring -- \
  uvx --from 'openadapt-agent[tutorial]' openadapt-agent \
  serve --authoring
```

`--bundles` is omitted. Probe tools are `observe`, `start_record`, `click`, and `halt`. This process stays stdio. Pass `--url` to pin a fresh Playwright Chromium with empty cookies. Windows native, Citrix, and RDP stay coach-only here, as does any macOS or Linux session without a unique frontmost window.

Private customer bundles still use `--bundles`. Those stay on the operator's disk and are never shipped here.

```bash
uvx openadapt-agent serve --bundles /path/to/bundles
uvx openadapt-agent serve --bundles /path/to/bundles --allow-run
```

## ChatGPT.com and Claude.ai

Hosted ChatGPT.com and Claude.ai can't talk to localhost stdio. Don't tell those tabs to run `openadapt-agent serve`. Send them to https://openadapt.ai/start.

Those tabs still can't click the user's GUI.

Once a job exists at `https://openadapt.ai/j/{id}`, Desktop **Connect this computer** claims `openadapt://runner`. Copy from that page and paste into the chat.

Pip users start the mailbox client:

```bash
openadapt-agent authoring connect \
  'openadapt://runner?pack=p.…&bind=oab_…&origin=https://openadapt.ai'
```

That claims `oab_`, polls `/j/{id}/runner/poll` with `wait_seconds: 0`, prints Allow (`y/n`) per chat account, and on pause prints `Sign in in the app, then press Enter`. Continue records with `record_observed`. It never types your password. Credentials stay on the user's computer. Don't paste a login into a vendor VM.

The job page should offer `openadapt connect <url>` next to Open OpenAdapt. This repo implements `openadapt-agent authoring connect`. Overlay chrome, launchd, the OS URL handler, and Keychain lease persistence stay Desktop-only. See [`docs/MAILBOX_CLI.md`](docs/MAILBOX_CLI.md).

## Outcomes

Every `run_workflow_<opaque-id>` call returns one of `success`, `halt`, `refused`, `timeout`, or `error`.

| `status` | Meaning |
| --- | --- |
| `success` | The process exited successfully and the persisted report records `execution_outcome: VERIFIED`. Legacy reports must record `success: true`. |
| `halt` | Execution halted, completed without enough verification, or completed a rollback. Not a verified success. If `execution_outcome` is `HALTED`, tell the user the record did not change. Protected evidence stays local. |
| `refused` | A governed admission gate refused the bundle before execution. Nothing ran. |
| `timeout` | The process exceeded its deadline. The target may be partly executed. Inspect it before retrying. |
| `error` | The CLI, report, or other execution infrastructure was inconsistent. |

Don't summarize any of them as success.

A timeout isn't a rollback. Don't retry until you've looked at the durable run.

`get_run_report` returns a PHI-safe status and a count-only summary for a run this server created. The persisted report stays in the local operator experience unless you enabled protected export.

## Trust boundary

Don't expose this process's stdin and stdout as an unauthenticated network service. It inherits the local user's OS permissions, and Flow records that OS account as the operator for attended decisions.

By default every MCP response is safe to render outside the protected workflow-data boundary. Labels, recorded values, paths, raw reports, observed text, stdout, stderr, and local exception messages stay on the OpenAdapt machine. The client gets opaque IDs, fixed outcome copy, declared parameter names and types, and count or boolean metrics.

`--allow-protected-export` sends raw local metadata and evidence to the MCP client. Use it only when that client is trusted and inside the same protected data boundary.

`--allow-synthetic-recorded-defaults` lets omitted parameters reuse recorded values. It requires `--allow-run`. Synthetic demonstrations only. Production runs require every declared parameter so a wrong-record action cannot hide in a default.

Remote transport, account identity, tenant isolation, fleet policy, and managed execution belong to OpenAdapt Cloud. They aren't duplicated here.

The complete contract is in [docs/DESIGN.md](docs/DESIGN.md).

## Serve a bundle

`--allow-run` with no `--bundles` records and compiles the synthetic MockMed workflow, certifies it, then keeps that app up so a governed run can hit a live system of record. `--tutorial` is the same path without implying run tools.

```bash
openadapt-agent serve --allow-run --runs-dir /tmp/openadapt-runs
```

The client gets `list_workflows`, `get_workflow`, `get_run_report`, `list_needs_attention`, and `get_attention_item`. Those are read-only until `--allow-run`. The synthetic tutorial registers `run_local_quickstart`. Add `--allow-run` and the server registers one typed `run_workflow_<opaque-id>` tool per loadable bundle. Declared parameters are required. Recorded demonstration values never appear in the schema and are not reused unless you ask for that.

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
| `observe`, `start_record`, `click`, `halt`, `compile`, `admit` | `--authoring` |

`--headed` keeps the attended web session visible. The MCP server is stdio. The target app still has a window.

## Finish a halt

`continue_attention` does not perform the paused action again. Point the server at Flow's qualified deployment config if a local operator needs to finish an exception and continue the same durable run:

```bash
openadapt-agent serve \
  --bundles /opt/openadapt/bundles \
  --runs-dir /var/lib/openadapt/runs \
  --allow-run \
  --allow-attended-actions \
  --config /etc/openadapt/deployment.yaml \
  --headed
```

The queue summary is safe to show in an agent UI. It carries opaque IDs, typed categories, counts, and signed-capability metadata. Observed text, workflow values, reports, and local paths stay off that surface.

| Tool | What happens |
| --- | --- |
| `continue_attention` | The operator confirms they completed the paused task in the live app. Flow revalidates postconditions and independent effects, checkpoints it as human-completed, and resumes after it. It does not perform the completed action again. |
| `skip_attention` | Flow applies only an already-declared, non-consequential skip. A stale, undeclared, consequential, or ambiguous skip is refused. |
| `reject_attention` | Ends this run and dispatches no new action. Earlier steps can still have effects, so read the protected local report. Escalate if a qualified operator can still continue. |
| `teach_attention` | Records an audited request for a corrective demonstration. Flow's revision and regression gates decide what can be promoted. |
| `escalate_attention` | Records an audited escalation and leaves the exact durable pause intact. |

Every mutation needs the opaque queue-item ID, the item's current capability digest, a caller-stable idempotency key, and an action-specific `true` confirmation. Before the server submits that to Flow, it opens an MCP form elicitation and the local operator has to accept it. That's a host-mediated confirmation, not cryptographic proof of who clicked. Flow records the effective local OS account as the operator.

Clients without form elicitation cannot execute attended actions through this bridge. The same Continue, Skip, Reject, Teach, and Escalate capabilities stay available through Flow's attended console:

```bash
openadapt-flow console \
  --attend \
  --allow-actions \
  --bundles /opt/openadapt/bundles \
  --runs /var/lib/openadapt/runs \
  --config /etc/openadapt/deployment.yaml \
  --headed
```

`--allow-attended-actions` without `--config` still registers Reject, Teach, and Escalate, and it withholds Continue and Skip until Flow can construct the deployment-bound live verifier and backend. `--headed` is required for web Continue and Skip.

## Emit an Agent Skill

`openadapt-agent emit-skill` wraps Flow's own skill emitter and writes a folder you can drop in `~/.claude/skills`.

```bash
openadapt-agent emit-skill \
  /tmp/openadapt-agent-demo/bundle \
  --out ~/.claude/skills
```

The wrapper keeps Flow's portable bundle and appends MCP invocation, halt handling, Needs Attention, and attended-action guidance. The frontmatter description is the same sentence as `server.json` and `llms.txt`. The skill is named from the workflow slug, never "computer use". If the tool returns HALTED, tell the user the record did not change.

A first-party copy lives at [`skills/openadapt-gui-write/SKILL.md`](skills/openadapt-gui-write/SKILL.md). A folder emitted from a private bundle isn't a sanitized derivative. It includes the compiled bundle, so it's protected workflow data.

## Product state

An exact Agent release enters Production only through an active signed admission. That admission expires and can be revoked. A missing, expired, revoked, mismatched, or unverifiable admission produces **not actively admitted**. The validator doesn't restore an older admission or assign a fallback lifecycle label.

Check the [current signed Production record](https://openadapt.ai/production-lifecycle.json). That ledger currently has seven target admissions. Evidence class is `remote-safe-synthetic`.

Standard and Regulated also need an active workflow admission for the exact bundle version. Demo and the synthetic tutorial may run without one. The public workflow ledger is [production-workflow-admissions.json](https://openadapt.ai/production-workflow-admissions.json). It lists seven synthetic admissions (`0.0.0-synthetic`). That isn't a customer job.

## Package history

Before v2 this repository wrapped model-driven GUI agents. That was the wrong product. The execution path now lives in `openadapt-flow`. The current name stays because the package bridges MCP and Agent Skills.

A user's compiled workflow is their private artifact. Supply it at launch with `--bundles`. It is never embedded in the package or a registry listing. See [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md).

The MCP registry reads [`server.json`](server.json). Smithery packs from [`manifest.json`](manifest.json). [`llms.txt`](llms.txt) exists because hosted assistants scrape a file, not this README. Registry-launched installs start read-only. Execution tools wait for `--allow-run`.

## Development

`pytest -q` covers README identity and the MCP registry manifests. It also pins the PHI-safe default.

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
