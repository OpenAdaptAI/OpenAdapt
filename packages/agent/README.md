# OpenAdapt Agent

> [!IMPORTANT]
> **Status: Repurposed (v2, Experimental).** This package is now the
> **agent-facing bridge** for
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow): it
> exposes compiled workflow bundles as **MCP servers** and **Agent
> Skills**, so other agents (Claude Code, Claude Desktop, any MCP client)
> can invoke governed workflows as tools. It is new, unproven code — do
> not treat it as production-ready.
>
> **Legacy v1 users:** the pre-pivot package (execution wrapper for
> model-driven GUI agents: safety gates, HITL confirmation, session
> management, audit logging) is deprecated and its modules were removed
> in v2. Pin the last v1-line release,
> [`openadapt-agent==0.1.0` on PyPI](https://pypi.org/project/openadapt-agent/0.1.0/),
> or browse the pre-pivot source at the last v1 commit on the `main`
> history. Execution responsibilities moved to the governed runtime in
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)](https://www.python.org/downloads/)

The bridge that makes the OpenAdapt README's promise — *"compiled
workflows can also be emitted as Agent Skills or MCP servers"* — real:

- **`openadapt-agent serve`** — an MCP server (stdio) over a directory of
  compiled workflow bundles. Each bundle becomes a `run_<slug>` tool with
  a JSON schema derived from its declared parameters; every call shells
  out to the governed `openadapt-flow run` CLI (fail-closed admission
  gates), and returns success **or a structured halt/refusal with an
  evidence pointer — never a fabricated success**. Read-only tools
  (`list_workflows`, `get_workflow`, `get_run_report`) are always on;
  run tools require the explicit `--allow-run` flag.
- **`openadapt-agent emit-skill`** — a Claude Agent Skill folder for one
  bundle. Wraps flow's own `emit-skill` and appends MCP-invocation and
  halt-semantics guidance for the consuming agent.

Design, security model, and what v2.0 deliberately does **not** do:
[`docs/DESIGN.md`](docs/DESIGN.md).

## Install

```bash
pip install "openadapt-agent>=2.0.0.dev0"   # not yet published; from source:
pip install git+https://github.com/OpenAdaptAI/openadapt-agent@feat/mcp-agent-bridge
```

Requires Python 3.10–3.12 (inherited from `openadapt-flow`). Installing
pulls in `openadapt-flow` (the governed runtime this package drives) and
the official `mcp` SDK.

## Quickstart: compile flow's bundled demo, serve it, call it

```bash
# 1. Record + compile flow's canonical demo (MockMed, ships with flow)
openadapt-flow demo-record --out /tmp/rec
openadapt-flow compile /tmp/rec --out /tmp/bundles/triage --name "Demo Triage"

# 2. Serve the bundle directory over MCP (stdio)
openadapt-agent serve --bundles /tmp/bundles --allow-run
```

Register it with Claude Code:

```bash
claude mcp add openadapt-workflows -- \
  openadapt-agent serve --bundles /tmp/bundles --allow-run
```

Claude Code then sees `list_workflows`, `get_workflow`,
`get_run_report`, and `run_demo_triage` (with a `note` parameter
defaulting to the recorded value). Omit `--allow-run` to expose the
read-only tools only.

> **Expect the demo run to be `refused` — that is the demo.** Run tools
> execute through flow's fail-closed `run` verb, and the freshly compiled
> demo bundle does not satisfy its admission gates (identity arming,
> effect contracts, encryption at rest), so `run_demo_triage` returns a
> structured `refused` outcome carrying the gate coverage report —
> nothing executes. That is the governed behavior this bridge exists to
> preserve. A bundle prepared for a real deployment (armed identity,
> effect contracts, `--config deployment.yaml` / `--policy`) runs to
> `success` or `halt`. To watch the demo actually execute without the
> gates, use flow's permissive demo verb directly:
> `openadapt-flow replay /tmp/bundles/triage`.

Emit an Agent Skill instead of (or as well as) serving MCP:

```bash
openadapt-agent emit-skill /tmp/bundles/triage --out ~/.claude/skills
```

## How results come back

Every `run_<slug>` call returns a structured outcome:

| `status` | meaning |
| --- | --- |
| `success` | exit 0 **and** the persisted `report.json` marks the run successful |
| `halt` | the run executed and stopped at an unhandled state — includes flow's structured halt observation (where, why, what was observed on screen, what completed first) and pointers to `report.json`/`REPORT.md`. **Not a success.** |
| `refused` | an `openadapt-flow run` admission gate refused the bundle (exit 2); **nothing was executed** |
| `timeout` | the run exceeded the operator-set deadline and was killed; the target may be partially executed |
| `error` | infrastructure problem (CLI missing, evidence inconsistent) |

`get_run_report` returns the full `report.json` for any run this server
made, so the consuming agent can show the evidence rather than summarize
around it.

## Honest limits (v2.0, Experimental)

- **No auth story beyond local stdio.** The server trusts whoever spawned
  it; the MCP client's user is the operator of record. No remote
  transport, no TLS, no multi-tenancy — do not expose this across a
  network boundary.
- **Unproven in production.** Unit tests mock the flow CLI; one local
  end-to-end smoke exists. No pilot has run through this bridge.
- **No halt-resolution bridging.** Halts are surfaced with evidence;
  fixing them (`openadapt-flow teach` / `approve` / `resume`) is still a
  human CLI workflow.
- **Timeout ≠ rollback.** A killed run may leave the target mid-workflow;
  the outcome says so, but nothing undoes partial work.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests && pytest
```

## License

MIT — see [LICENSE](LICENSE).
