# openadapt-agent v2 — Design

**Status: Experimental.** New, unproven code. Nothing in this document
claims production readiness; the honest-limits section below is part of
the design, not a disclaimer bolted on.

## What this package is

`openadapt-agent` v2 is the **agent-facing bridge** for
[openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow): it takes
a directory of *compiled workflow bundles* (the output of
`openadapt-flow compile` / `induce`) and exposes them to other agents —
Claude Code, Claude Desktop, or any MCP client — as:

1. an **MCP server** (`openadapt-agent serve`, stdio transport), and
2. **Agent Skills** (`openadapt-agent emit-skill`).

It makes real the main OpenAdapt README's promise that "compiled
workflows can also be emitted as Agent Skills or MCP servers."

v1 of this package (a legacy execution wrapper for model-driven GUI
agents: safety gates, HITL confirmation, session management) is
deprecated and its modules were removed in v2; the last v1-line release
remains on PyPI as `openadapt-agent 0.1.0`.

## Relationship to openadapt-flow's own emitters (wrap, don't duplicate)

flow ≥1.9 already ships `openadapt_flow.emit` with two emitters and CLI
verbs:

| flow surface | what it produces | gap for agent use |
| --- | --- | --- |
| `emit-skill` (`emit_skill`) | portable skill folder: `SKILL.md` + copied `bundle/`, CLI `replay` invocation | no MCP guidance; invokes the *permissive* `replay` path; no halt-semantics instructions for the consuming agent |
| `emit-mcp` (`emit_mcp_server`) | standalone per-bundle FastMCP `server.py` that runs the `Replayer` **in-process** | single workflow per server; permissive replay path (no `run` admission gates); web/Playwright backend only; always executable (no read-only mode); result carries only a success flag — no structured halt evidence |

This package **wraps** rather than duplicates:

- `openadapt-agent emit-skill` calls flow's `emit_skill()` verbatim and
  then *appends* two sections to the generated `SKILL.md`: how to invoke
  the workflow through the MCP server, and the halt semantics the
  consuming agent must respect. flow's frontmatter, step list, parameter
  table, and CLI usage are untouched.
- `openadapt-agent serve` does **not** build on `emit_mcp_server`,
  deliberately: that emitter's design point is a self-contained,
  in-process, single-workflow demo server. The bridge's design point is
  the opposite — multi-workflow, out-of-process, **governed**. The two
  coexist; the DESIGN gap table above is the justification. If flow later
  grows a governed multi-bundle server, this package should shrink to a
  wrapper around it.

## Architecture

```
MCP client (Claude Code / Desktop, stdio)
        │  tools/list, tools/call
        ▼
openadapt_agent.mcp        thin adapter: bridge ⇄ mcp SDK low-level Server
        ▼
openadapt_agent.bridge     tool specs + dispatch (no mcp imports; unit-testable)
   ├─ openadapt_agent.bundles   discovery + metadata via Workflow.load (flow IR)
   └─ openadapt_agent.runner    one subprocess per run:
              openadapt-flow run BUNDLE --run-dir … --params-file … [--url/--config/--policy …]
                                └── flow's fail-closed admission gates + replayer
```

Execution is **never reimplemented**. Every run tool call is a subprocess
invocation of `openadapt-flow run` — flow's fail-closed deployment verb —
so certification, identity arming, effect contracts, encryption-at-rest,
and integrity pinning apply exactly as they would from a terminal. The
bridge has no code path that could bypass them.

### Tools

Read-only (always registered):

- `list_workflows` — discovered bundles (slug, name, params, step count,
  load errors), plus whether run tools are enabled.
- `get_workflow` — step intents, declared params with recorded example
  values, and certification status (evaluated via `openadapt-flow
  certify` when the operator configured `--policy`/`--config`; honestly
  `null` otherwise).
- `get_run_report` — the persisted `report.json` for a `run_id` this
  server produced. Constrained to the server's runs directory (single
  path component, resolved containment check).

Run tools (registered only under `--allow-run`):

- `run_<slug>` per loadable bundle. Input schema is derived from the
  bundle's declared parameters: each is an optional string whose default
  is the recorded example value (the same fallback the flow CLI applies);
  `additionalProperties: false`. A `url` property exists only when the
  operator passed `--allow-url-override`.

### Outcome mapping (honesty invariant)

`openadapt-flow run` exit-code contract → MCP result `status`:

| exit | meaning | status |
| --- | --- | --- |
| 0 + report.success | executed, every step verified | `success` |
| 0, no/failed report | inconsistent evidence | `error` / `halt` (report outranks exit code; never success) |
| 1 | executed and stopped | `halt` — includes the structured `halt` observation (state, reason, PHI-scrubbed observed texts, completed intents) or the failing step, plus `run_dir`/`report_path` evidence pointers |
| 2 | governed refusal — nothing executed | `refused` — stdout tail carries the gate coverage report |
| timeout | process killed at operator-set deadline | `timeout` — with a warning that the target may be partially executed |

The invariant, enforced in `classify_outcome` and covered by tests:
**`success` requires both exit 0 and a persisted report with
`success: true`.** A halt is surfaced as a halt with an evidence pointer,
never as success, and never silently retried.

## Security model

- **Execution is opt-in.** Without `--allow-run` the server registers
  read-only tools only; a forged `run_*` call is refused by dispatch as
  well (belt and braces).
- **The operator fixes the blast radius at start time.** Target URL,
  deployment config, policy, timeout, extra run args, and the flow CLI
  itself are server flags — an MCP client cannot alter them per call.
  Per-call URL override requires the separate `--allow-url-override`.
- **Governance lives in flow, not here.** The server cannot bypass
  policy/identity/effect gates because it only ever invokes the governed
  CLI; there is no in-process replay path.
- **Parameter hygiene.** Params travel via a mode-0600 temp file passed
  as `--params-file` (flow's managed-runner mechanism), never argv, and
  the file is deleted after the run. Unknown parameters are rejected
  before any subprocess starts.
- **Report access is scoped.** `get_run_report` accepts a single path
  component and verifies the resolved path stays inside the server's runs
  directory.
- **Operator of record.** The server performs no authentication. The MCP
  client's user is the operator of record for anything a run writes;
  stdio transport means the server runs with that user's local
  privileges, on that user's machine.
- **Per-call timeout.** Every run subprocess is killed at the configured
  deadline and reported as `timeout` (not success). Note flow may leave
  the target mid-workflow; the outcome says so.
- **Encrypted bundles** stay encrypted: metadata loads through
  `Workflow.load` (key from `OPENADAPT_BUNDLE_KEY`), and a bundle that
  cannot be loaded is listed with its `load_error` and gets no run tool.

## What v2.0 does NOT do

- **No remote transport.** stdio only. No SSE/HTTP, no TLS story, no
  network authentication or multi-tenant isolation. Do not port-forward
  this at a network boundary.
- **No auth beyond the local user.** Anyone who can spawn the process can
  call the tools it registers.
- **No scheduling, queueing, or concurrency control.** One subprocess per
  tool call; concurrent calls are concurrent flow runs.
- **No execution semantics of its own.** No retries, no healing, no
  branching — all of that is flow's job.
- **No teach/resume/approve bridging yet.** Halts are surfaced with
  evidence; resolving them (`openadapt-flow teach`, `approve`, `resume`)
  remains a human CLI workflow.
- **No production track record.** This code is new and unproven; it has
  unit tests with the flow CLI mocked and a local end-to-end smoke, and
  nothing more.

## Testing strategy

- Unit tests mock `subprocess.run` with a stub that emulates the flow
  CLI's exit-code/report contract; fixture bundles are written through
  flow's own IR (`Workflow.save`) so schema drift breaks tests here
  instead of hiding.
- Covered: schema generation from a fixture bundle, run→success mapping,
  run→halt mapping (exit 1 and exit-0-with-failed-report both), exit 2 →
  structured refusal, timeout handling, `--allow-run` and
  `--allow-url-override` gating, run-report path containment, skill
  emission golden-file.
- The MCP layer is exercised by constructing the low-level server and
  invoking its `tools/list` handler; the stdio transport is covered by a
  manual smoke script (`scripts/smoke_client.py`), not CI.
- What the real-CLI smoke has actually proven so far: flow's bundled demo
  compiled → served → `tools/list` + `list_workflows` + `get_workflow`
  over real stdio, and `run_demo_triage` → structured `refused` carrying
  the real gate coverage report (the demo bundle cannot pass the
  fail-closed gates, by design). The `success`/`halt` mappings are proven
  only against the stubbed CLI contract, not yet against a
  gate-satisfying real deployment bundle.
