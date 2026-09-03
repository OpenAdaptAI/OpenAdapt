# OpenAdapt Agent design

An exact Agent release gets its product state from the signed Production
admission record. A missing, expired, revoked, mismatched, or unverifiable
admission means **not actively admitted**. The validator doesn't restore an
older admission or assign a fallback lifecycle label.

OpenAdapt compiles demonstrated GUI workflows into programs. This package invokes them over MCP.

`openadapt-agent` is the default runtime interface for a calling agent. It
is not a second workflow engine, and it is not "OpenAdapt the agent." Flow
owns compilation, policy, identity, verification, durable execution, repair,
and audit. The CLI remains. Headless does not mean the UI is gone.

## Roles

| Role | Who | What they do |
|---|---|---|
| Operator | Calling agent (Claude Code, MCP client, orchestrator) | Discover, bind parameters, invoke, read outcomes |
| Authority | Named human | Admit the compiled program (watch if the agent clicked), certify policy, resolve identity / effect / judgment halts |
| Auditor | Compliance | Sample seals, revoke admission |

Computer-use agents are the user of OpenAdapt. They are not the executor
inside OpenAdapt. Never summarize halt as success. A local agent may click
during the demonstration while the person watches. The calling agent must
not be the sole source of a production demonstration of a novel GUI path.

Halt packets and typed agent-continue (missing parameter / retryable
transport only) are not in this package yet. Identity, effect, expired
policy, novel UI, and admission still require a human. Do not let a
planner resolve those classes.

`openadapt-agent` exposes two complementary interfaces:

1. MCP tools over local stdio.
2. Portable Agent Skills.

## Architecture

```text
MCP client / Agent Skill
          │
          │ local stdio + exact JSON schemas
          ▼
openadapt_agent.mcp          (local stdio only; HTTP shim forbidden)
          │
          ├── openadapt_agent.bridge
          │     ├── bundle discovery and typed run tools
          │     ├── PHI-safe Needs Attention projection
          │     ├── action-specific operator decisions
          │     └── structured success / halt / refusal results
          │           │
          │           ├── new run ──► openadapt-flow run subprocess
          │           └── attended ─► openadapt-flow durable API
          │
          ├── openadapt_agent.authoring   (--authoring first demo, stdio)
          │     observe / start_record / click / halt
          │     local type (agent-driven Recorder.type_text)
          │     pause Continue → record_observed (never type_text)
          │           │
          │           ▼
          │     openadapt_flow.authoring.AuthoringSession
          │
          └── openadapt_agent.mailbox     (authoring connect, outbound HTTPS)
                parse openadapt://runner / pack URL
                POST claim oab_ → poll wait=0 → Allow-per-sub
                Continue → record_observed (never type_text)
                overlay chrome stays Desktop-only
```

The MCP adapter is intentionally thin. Tool descriptions and dispatch
live in a transport-independent bridge so they can be tested without
starting stdio.

## Why the package keeps both MCP and Agent Skills

Flow's skill emitter produces the portable workflow folder. This package
wraps it and appends agent-facing invocation, halt, and attended-action
guidance without regenerating the workflow. The resulting skill includes
the compiled bundle and is protected workflow data, not a sanitized
artifact; operators install it only into an agent authorized for the
same data boundary.

Flow's per-bundle MCP emitter has a different purpose: a self-contained
demo server for one workflow. `openadapt-agent serve` is the governed
multi-bundle local surface. It uses Flow's `run` admission path and
returns durable evidence rather than invoking permissive replay.

The repository therefore remains `openadapt-agent`, not
`openadapt-mcp`: MCP is one transport; Agent Skills are an equally
supported surface.

## Tool registration

The server always registers:

- `list_workflows`
- `get_workflow`
- `get_run_report`
- `list_needs_attention`
- `get_attention_item`

The first three return PHI-safe projections: opaque IDs, parameter
names/types, availability, status, and count/boolean metrics. They do not
return workflow labels, recorded values, step intents, report bodies,
observed text, local paths, subprocess output, or exception text. The
attention tools use the same boundary plus typed categories, artifact
IDs, and non-authorizing capability metadata.

`--allow-run` registers one `run_workflow_<opaque-id>` tool per loadable
workflow. `--allow-run` with no `--bundles` generates the public synthetic
MockMed bundle at serve time and registers `run_local_quickstart`.
`--tutorial` is the same generator without implying run tools. Every declared parameter
is required by default, and recorded demonstration values never enter the
schema. Missing or unknown parameters are rejected before the subprocess
starts. A per-call URL is accepted only if the operator separately enabled
`--allow-url-override`. The tutorial path still uses Flow's fail-closed
`run` verb. It does not pass `--approve-unverified-writes`.

Two server-start modes are intentionally separate from ordinary
operation:

- `--allow-protected-export` includes raw labels, values, intents, local
  paths, reports, stdout/stderr, and detailed local errors. It is for an
  explicitly trusted MCP client inside the same protected data boundary.
- `--allow-synthetic-recorded-defaults` permits omitted parameters to
  use demonstrated values. It requires run authority and is only for
  synthetic demos; it never places those values in a tool schema.

`--allow-attended-actions` registers Reject, Teach, and Escalate. Continue and
Skip are registered only when a deployment configuration lets Flow
construct its bound live executor.

`--authoring` registers first-demo tools over the same local stdio
server. Probe names match hosted MCP: `observe`, `start_record`,
`click`, `halt`. Local stdio may also include `type` for agent-driven
typing through Flow's Recorder. Hosted MCP remains pause-only. Human
type during `pause_for_input` is persisted with `Recorder.record_observed`
on the pause-target node, never `type_text`. `compile` wraps Flow
`compile_recording` and returns `needs_human_admit`; an agent click never
paints `VERIFIED`.

`--authoring` does not imply `--allow-run`. `--bundles` is optional iff
`--authoring` (or the existing `--tutorial` / implied-tutorial path). The
published run recipe in `server.json` still requires `--bundles` and
stays `transport: stdio`. Authoring is a first demo; there is no bundle
yet.

Observe is a fail-closed PHI projection (`openadapt.authoring.observe/v1`):
no `value`, `text`, window `title`, screenshot, OCR, URL, or backend
pixels. Windows native, Citrix, and RDP are `COACH_ONLY` in v1.

The session object is Flow's public `openadapt_flow.authoring` module
(`AuthoringSession(backend, out_dir, backend_kind=…)` when F1 is
importable). Until that module is importable, `serve --authoring` fails
closed with an explicit dependency error. Windows native, Citrix, and
RDP construct a coach-only stand-in and never spawn `win_agent`. Observe
is fail-closed to the T1 wire (`additionalProperties: false`, node ids
`n_` + 8 hex, 200 nodes / 32 KiB). Capture's projector is used when
importable. If Desktop has advertised authoring IPC, overlay stays
Desktop-owned; stdio `--authoring` does not speak the D2 protocol.
`authoring connect` is the outbound mailbox client for hosted chat apps.
Tests cover the stdio tool surface with a fake session and an F1-shaped
session, and the mailbox client against a mocked wait=0 poll.

## Governed runs

Each run tool shells out to the `openadapt-flow` installed in the same
Python environment. The command uses Flow's fail-closed `run` verb, so
its certification, identity, effects, encryption, integrity, and egress
gates remain authoritative.

For a precise Flow report, the bridge reports success only when all conditions hold:

1. Flow exits with code 0.
2. The persisted report has `execution_outcome: VERIFIED`.
3. The persisted report has a consistent `success: true` value.
4. The report is not production-eligible. Local MCP never mints a Seal,
   so production-eligible `VERIFIED` is unsigned success and is failure.

Write tools advertise `requires_seal: true` on MCP `_meta`. If the tool
returns unsigned success, treat it as failure.

For a legacy report without `execution_outcome`, the bridge preserves the
compatible rule: exit code 0 plus `success: true`. Local unsigned replay
is free. Production success without a Seal is failure.

Exit 1 is a halt. Exit 2 is a governed refusal before execution. A
timeout is explicitly uncertain rather than a rollback. Report evidence
always outranks a process exit code.

Parameters are passed in a mode-`0600` temporary JSON file and removed
after the run. Server-fixed target, policy, deployment configuration,
timeout, and extra Flow arguments cannot be changed by an MCP call.

The runner retains detailed reports and subprocess diagnostics locally,
but its default MCP projection contains only an opaque workflow/run ID,
fixed status copy, and schema-limited metrics. Success, halt, refusal,
timeout, and infrastructure-error paths share this same projection, so
an exception cannot accidentally turn into an egress channel.

## Needs Attention

Flow creates a signed attended capability for a specific durable pause.
The capability binds:

- run and pause identity;
- workflow and bundle version;
- exact step or interpreter cursor;
- checkpoint lineage and expected next transition;
- verification and delivery state;
- allowed actions and expiration.

Only the capability digest and allowed-action summary cross the
agent-facing queue projection. The HMAC, protected evidence, and local
paths stay inside the Flow runtime boundary.

Each mutation is an action-specific MCP tool with
`additionalProperties: false`. Its payload contains only:

- opaque attention ID;
- exact capability digest;
- stable idempotency key;
- one explicit boolean operator confirmation.

There is no field for challenge answers, credentials, screenshots,
observed text, or arbitrary approval prose.

The payload's confirmation boolean is not treated as proof of human
presence. Before dispatch, the MCP server performs a second,
action-specific form elicitation and requires an explicit accept plus
confirmation from the local operator. Elicitation is a host-mediated
explicit-confirmation signal, not cryptographic proof that a particular
person clicked, nor identity proof. Flow separately records the effective
local OS account as the operator. A client that does not advertise
form elicitation cannot execute attended mutations through MCP; the
operator uses Flow's existing attended console/CLI instead, where all
five capabilities remain available. This is a transport authorization
choice, not a read-only conversion. Tool annotations also mark Continue
and Skip as destructive, idempotent, and open-world. Reject is destructive
and idempotent but not open-world because it dispatches no new application
action. These hints let the host apply
its own approval policy. Annotations and elicitation do not replace
Flow's signed capability, live revalidation, idempotency, or durable
audit.

### Continue

Continue means the human already completed the paused task in the live
application. Flow:

1. reloads and validates the exact signed pause under a filesystem
   lease;
2. verifies the human-completed postconditions and independent effects
   in the deployment-bound live session;
3. commits a human-completed checkpoint;
4. resumes from the next transition.

The completed action is never actuated again. If delivery may have
crossed the boundary without a terminal receipt, retries are refused
until reconciliation.

### Skip

Skip is not a generic bypass. It exists only when the signed capability
and compiled workflow declare a safe, non-consequential skip. Flow
rechecks that guard against current state. Consequential, stale,
ambiguous, or undeclared skips are refused.

### Reject

Reject terminates the current run and permanently prevents resume. The
rejection dispatches no new application action. Earlier steps in the run can
still have effects, so the operator must inspect the protected local report
and transaction outcome. Use Escalate instead when a qualified operator can
still inspect and continue the run.

### Teach

Teach records an audited request for a corrective demonstration. The
durable pause remains intact. The existing Flow teach/revision pipeline
owns capture, regression evaluation, evidence banking, and promotion;
the MCP client cannot directly rewrite a bundle.

### Escalate

Escalate records a durable, audited request for qualified assistance and
leaves the pause available for later resolution.

## Idempotency and thread ownership

Flow persists attended decisions before crossing a delivery boundary.
Repeating the same action with the same idempotency key returns its
terminal decision. Reusing a key for different content is refused.

Continue and Skip use a persistent deployment-bound backend. Some
backends, including Playwright, are thread-affine and their synchronous
APIs cannot run inside MCP's asyncio event loop. Flow's public
`AttendedActionService` therefore creates, uses, and closes the live
executor on one dedicated non-async owner thread. The bridge submits
exact signed requests through that service while the event loop remains
responsive. Run subprocesses and read-only projections use ordinary
worker threads.

Flow serializes live attended actions and applies per-pause filesystem
leases. A second process cannot silently duplicate an in-flight
decision.

## Identity and transport

The MCP server uses local stdio. The process inherits the OS user's
permissions, and the effective local OS account is recorded as the
attended operator. POSIX uses the effective UID account; Windows uses
the process/thread token-backed `GetUserNameW` API rather than the
caller-controlled `USERNAME` environment variable. A blank operator
identity fails closed.

This process must not be port-forwarded or exposed as an unauthenticated
network service. An HTTP / Streamable-HTTP **listener** in this MIT package
remains forbidden, including when `--authoring` is set. Hosted ChatGPT.com
/ Claude.ai cannot talk to localhost. Pip users run `openadapt-agent
authoring connect` — an **outbound** mailbox client (claim `oab_`, poll
`wait_seconds: 0`, Allow-per-`sub`) copied from Desktop
`engine/authoring_runner.py` when that engine is not importable. Overlay
chrome, launchd, and the `openadapt://` URL handler stay Desktop-only.
See `docs/MAILBOX_CLI.md`. OpenAdapt Cloud owns remote authentication,
multi-tenancy, tenant-scoped authorization, fleet policy, and managed
execute. `--authoring` does not add those, and it does not imply
`--allow-run`.

## Dependency boundary

The attended bridge uses Flow's public durable action contract and public
`AttendedActionService`, constructed from a public `DeploymentConfig`.
The package pins the Flow minor release containing that contract so a
later refactor cannot silently change backend construction, thread
ownership, or cleanup semantics.

Agent applies only explicit server-start URL, visibility, and egress
overrides to that typed deployment config. Replayer, backend, policy,
verification, owner-thread, resume, and audit logic are never copied
into this repository.

## Test and release contract

Tests cover:

- exact tool registration and schemas;
- opaque workflow/run IDs, required parameters, and no recorded defaults
  in MCP schemas;
- adversarial PHI/secret/path/exception strings across workflow
  discovery, success, halt, refusal, timeout, error, and report lookup;
- explicit protected-export and synthetic-default modes;
- MCP form elicitation and action annotations;
- PHI-safe queue projections and path traversal refusal;
- stale capability, unknown field, and false-confirmation refusal;
- idempotent Continue without re-actuation;
- Reject, Teach, and Escalate without a live service;
- delegation to Flow's public service context;
- compatibility with Flow's public, thread-owned attended service;
- success/halt/refusal/timeout outcome mapping;
- MCP serialization and thread ownership;
- Agent Skill emission;
- `--authoring` probe tools (`observe`, `start_record`, `click`, `halt`)
  and local `type`; observe projection drops values/titles/screenshots
  and extra keys, caps the wire at 32 KiB, and uses `n_` + 8 hex node
  ids; pause Continue uses `record_observed` rather than `type_text`;
  compile returns `needs_human_admit`; `--authoring` does not enable
  run tools; `server.json` stays stdio with `--bundles` required;
  `authoring connect` parses `openadapt://runner` / pack URLs, claims
  `oab_`, polls `wait_seconds: 0`, prompts Allow-per-`sub`, and Continue
  uses `record_observed` (never `type_text`).

CI runs on Python 3.10, 3.11, and 3.12. It also builds the wheel and
sdist, verifies MIT metadata and license inclusion, and refuses package
artifacts containing repository-only copyleft benchmark material.
