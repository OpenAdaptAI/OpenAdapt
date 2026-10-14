# OpenAdapt Agent design

An exact Agent release gets its product state from the signed Production
admission record. A missing, expired, revoked, mismatched, or unverifiable
admission means **not actively admitted**. The validator doesn't restore an
older admission or assign a fallback lifecycle label.

The live target ledger is
https://openadapt.ai/production-lifecycle.json. It currently has seven
target admissions. Evidence class is `remote-safe-synthetic`. The live
workflow ledger is
https://openadapt.ai/production-workflow-admissions.json. It currently
lists seven synthetic admissions (`0.0.0-synthetic`). That isn't a
customer job. Standard and Regulated need an active workflow admission
for the exact bundle version. Demo and the synthetic sandbox may run
without one.

Your AI agent decides what to enter. OpenAdapt enters it in the app and checks that it saved.

`openadapt-agent` is the default runtime interface for a calling agent. It
is not a second workflow engine, and it is not "OpenAdapt the agent." Flow
owns compilation, policy, identity, verification, durable execution, repair,
and audit. The CLI remains. Headless does not mean the UI is gone.

## Roles

| Role | Who | What they do |
|---|---|---|
| Calling agent | Your AI agent (Claude Code, an MCP client, an orchestrator) | Chooses a workflow, sends inputs and its own `request_id`, acts on the outcome |
| Operator | A person at the computer that runs workflows | Starts the server, answers paused runs, checks records the result says to check |
| Authority | Named person | Approves the compiled workflow, certifies policy, resolves identity, record-check, and judgment stops |
| Auditor | Compliance | Reviews receipts, revokes admission |

"Operator" always means a person. Computer-use agents are the user of
OpenAdapt. They are not the executor inside OpenAdapt. A local agent may
click during the demonstration while the person watches. The calling agent
must not be the sole source of a production demonstration of a novel GUI
path. Identity, record-check, expired-policy, novel-UI, and admission stops
still require a person. A calling agent never resolves those classes; it
reads `needs_review` or `not_sure_if_saved` and waits.

`openadapt-agent` exposes two complementary interfaces:

1. MCP tools over local stdio.
2. Portable Agent Skills.

## Architecture

```text
MCP client / Agent Skill
          │
          │ local stdio + exact JSON schemas (inputSchema / outputSchema)
          ▼
openadapt_agent.mcp          (local stdio only; HTTP shim forbidden)
          │
          ├── openadapt_agent.bridge         tool specs + dispatch
          │     ├── list_workflows / run_workflow / get_run   (the contract)
          │     │     ├── cards     workflow cards: name, purpose, done_means, typed inputs
          │     │     ├── service   validate → request gate → worker thread → wait
          │     │     ├── runs      durable records + Flow IdempotencyLedger
          │     │     └── contract  transaction_outcome → four outcomes, closed reasons
          │     │           │
          │     │           ├── operator bundles ─► openadapt-flow run subprocess
          │     │           └── sandbox ─────────► Flow run gate + Replayer on MockMed
          │     ├── older tools: get_workflow, get_run_report, run_<id> (deprecated)
          │     ├── PHI-safe Needs Attention projection
          │     └── action-specific attended decisions ─► openadapt-flow durable API
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

`openadapt-agent emit-skill` writes a skill from the workflow card: name,
purpose, what done means, typed inputs, how to call `run_workflow`, and how
to read the four outcomes. A skill is loaded into model context, which for
most clients means a hosted model, so it never carries recorded example
values, step intents, observed text, typed secrets, local paths, or a
replay recipe that would fall back to recorded values. Each description
names its own workflow and inputs. `--include-bundle` copies the compiled
bundle next to `SKILL.md`; that copy is protected workflow data.

Flow's per-bundle MCP emitter and `openadapt-flow emit-skill` have a
different purpose: self-contained replay demos for one workflow.
`openadapt-agent serve` is the governed multi-bundle local surface. It
uses Flow's `run` admission path and returns durable evidence rather than
invoking permissive replay.

The repository therefore remains `openadapt-agent`, not
`openadapt-mcp`: MCP is one transport; Agent Skills are an equally
supported surface.

## The result contract

Every agent-facing result comes from `openadapt_agent.contract`. It has
four terminal outcomes, each mapped to one decision the calling agent
makes:

| `outcome` | Meaning | `safe_to_retry` |
|---|---|---|
| `done` | Saved and checked by reading the record back | false |
| `needs_review` | Stopped before saving; nothing written; a person decides | false |
| `not_sure_if_saved` | A save may have gone through; a person checks the record | false |
| `did_not_run` | Nothing was written | true when a retry may succeed |

`running` is the one non-terminal value. The plain result is a pure
function of Flow's `transaction_outcome`, the process facts (did Flow
start, did it exit cleanly, did it time out), the state of any durable
pause, and a consistency check of the persisted report:

| Flow evidence | `reason` | `outcome` |
|---|---|---|
| consistent `VERIFIED`, exit 0 | `saved_and_checked` | `done` |
| `HALTED_BEFORE_EFFECT` | from the pause category, for example `record_not_confirmed` | `needs_review` |
| `REJECTED_POLICY`, `CANCELED`, `FAILED_PLATFORM` with an open durable pause | from the pause category | `needs_review` |
| the same with no open pause | `policy_refused`, `canceled`, `platform_error` | `did_not_run` |
| any proven-no-effect outcome whose pause a person rejected | `stopped_by_person` | `did_not_run` (no retry) |
| `RECONCILIATION_REQUIRED` | `save_not_confirmed` | `not_sure_if_saved` |
| `COMPLETED_UNVERIFIED` | `not_checked` | `not_sure_if_saved` |
| `ROLLED_BACK` | `change_reversed` | `not_sure_if_saved` |
| timeout after start, inconsistent or missing report, coarse `HALTED` alone | `timed_out`, `result_unreadable` | `not_sure_if_saved` |
| a server that stopped mid-run | `interrupted` | `not_sure_if_saved` |
| exit 2 before execution | `not_ready_to_run` with closed `failed_checks` | `did_not_run` |
| Flow could not be launched | `platform_error` | `did_not_run` |
| the app isn't reachable, checked before anything is sent (sandbox) | `app_unreachable` | `did_not_run` |
| invalid inputs, unknown workflow, runs disabled | `invalid_input`, `unknown_workflow`, `runs_disabled` | `did_not_run` |

Only `done` says the change was saved. `record_changed: "no"` and
`safe_to_retry: true` appear only when the evidence proves nothing was
written. A coarse `HALTED` without a transaction outcome is uncertain,
never "nothing changed". A failed Needs Attention lookup counts as an open
pause, so the run waits for a person. `proof` (`local`, `sealed`,
`simulated`, or `none`) is separate from the outcome: a verified local run
is `done` with `proof: "local"`, never an error. A partner whose policy
requires a signed receipt checks `proof`.

Every sentence in `what_happened` and `next_action` is fixed copy keyed by
a closed reason code. No observed screen text, recorded value, input value,
caller-supplied name, or local path reaches a result.

### Requests, retries, and records

`request_id` is required on `run_workflow`. Each attempt reserves
`<sha256(request_id)>:<attempt>` in openadapt-flow's `IdempotencyLedger`
(namespace `openadapt-agent/run-workflow/v1`, under
`<runs-dir>/openadapt-agent/`) before anything runs. A lost race reads back
the winner. The same `request_id` returns the latest attempt's result with
`replayed: true`. A new attempt starts only when the latest one ended in a
reason from `RETRYABLE_REASONS`, each of which proves nothing was written,
and at most five times. A `request_id` reused with different inputs is
refused as `request_id_conflict`. If Flow's ledger can't be opened, local
exclusive-create claim files give the same at-most-once guarantee for that
runs directory.

Every call leaves a record at
`<runs-dir>/openadapt-agent/runs/<run_id>.json`, refusals included, so
`get_run` can always answer. Records hold the contract result only. Inputs
are never stored; a keyed fingerprint of `(workflow, inputs)` is.

`run_workflow` runs a new attempt on a worker thread and waits up to
`wait_seconds` (default 60, maximum 600). A run still going returns
`outcome: "running"` with its `run_id`. `get_run` waits the same way. A
record left `running` by a process that is gone reads as `interrupted`. A
`needs_review` result is re-read from Flow on each `get_run`, so a
person's Continue or Reject shows up. A re-read never turns a reviewed run
into a retryable result: Flow clears a continued pause just before it
replaces the pre-resume report, so the run keeps waiting until the new
report lands.

### Workflow cards

`list_workflows` returns one card per workflow. Types and choices come from
Flow's `param_specs`. Words come from an optional operator-written
`openadapt-card.json` next to `workflow.json`. Card text is
length-limited and refused when it looks like it carries an identifier (a
run of five or more digits, an email address, a phone number). A refused
card falls back to the default card with a local warning that never
repeats the text. Every declared input stays required unless
`--allow-synthetic-recorded-defaults` is set.

## Modes and tool registration

| Start | Mode | Tools |
|---|---|---|
| `serve` (no flags), `--mode sandbox`, `--tutorial`, `--allow-run` without `--bundles` | sandbox | `list_workflows`, `run_workflow`, `get_run` |
| `--bundles DIR` | production, read-only | `list_workflows`, `get_run`, `get_workflow`, `get_run_report`, `list_needs_attention`, `get_attention_item` |
| `--mode production --bundles DIR` (or `--allow-run`) | production | the above plus `run_workflow` and deprecated `run_<opaque-id>` |
| `--mode attended --bundles DIR` (or `--allow-run --allow-attended-actions`) | attended | the above plus Reject, Teach, Escalate; Continue and Skip with a qualified `--config` |
| `--bundles DIR --allow-attended-actions` (older flags, no runs) | attended, read-only | the read-only tools plus the same decisions on paused runs |

The read-only tools return PHI-safe projections: workflow cards, opaque
ids, availability, status, and count or boolean metrics. They do not
return workflow labels, recorded values, step intents, report bodies,
observed text, local paths, subprocess output, or exception text. The
attention tools use the same boundary plus typed categories, artifact
ids, and non-authorizing capability metadata.

The deprecated per-workflow `run_<opaque-id>` tools keep their schemas and
legacy `status` field and add the contract fields. They take no
`request_id`, so their descriptions say a retry can write twice. Missing
or unknown parameters are rejected before the subprocess starts. A
per-call URL is accepted only if the operator separately enabled
`--allow-url-override`. No run path passes
`--approve-unverified-writes`.

Two server-start options are intentionally separate from ordinary
operation:

- `--allow-protected-export` includes raw labels, values, intents, local
  paths, reports, stdout/stderr, and detailed local errors. It is for an
  explicitly trusted MCP client inside the same protected data boundary.
- `--allow-synthetic-recorded-defaults` permits omitted parameters to
  use demonstrated values. It requires run authority and is only for
  synthetic demos; it never places those values in a tool schema.

`--authoring` registers first-demo tools over the same local stdio
server. Probe names match hosted MCP: `observe`, `start_record`,
`click`, `halt`. Local stdio may also include `type` for agent-driven
typing through Flow's Recorder. Hosted MCP remains pause-only. Human
type during `pause_for_input` is persisted with `Recorder.record_observed`
on the pause-target node, never `type_text`. `compile` wraps Flow
`compile_recording` and returns `needs_human_admit`; an agent click never
paints `VERIFIED`. `admit` is the one-token human ok of the pre-filled
draft; the human does not fill schema, authority, effect, environment, or
digest. If the Flow session has no `admit`, the tool fails closed and does
not mint a Seal or write an unsigned ledger row.

`--authoring` does not imply `--allow-run`. `--bundles` is optional iff
`--authoring` (or the sandbox). The
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

Each operator-bundle run shells out to the `openadapt-flow` installed in
the same Python environment. The command uses Flow's fail-closed `run`
verb, so its certification, identity, effects, encryption, integrity, and
egress gates remain authoritative.

The bridge reports `done` (legacy `status: "success"`) only when all of
these hold:

1. Flow exits with code 0.
2. The persisted report has `transaction_outcome: VERIFIED` and
   `execution_outcome: VERIFIED`.
3. The persisted report has a consistent `success: true` value and a
   consistent outcome envelope.

A production-eligible `VERIFIED` run is `done` with `proof: "local"`.
Local MCP mints no Seal, and reporting a verified write as an error would
invite a retry and a duplicate write. A legacy report with only the
`success` flag is `not_sure_if_saved` ("finished, not checked"). Standard
and Regulated need an active workflow admission for the exact bundle
version. Demo and the synthetic sandbox may run without one.

Exit 2 is a governed refusal before execution, mapped to closed
`failed_checks` codes. A timeout is uncertain, never a rollback. Report
evidence always outranks a process exit code.

Parameters are passed in a mode-`0600` temporary JSON file and removed
after the run. Server-fixed target, policy, deployment configuration,
timeout, and extra Flow arguments cannot be changed by an MCP call.

The runner retains detailed reports and subprocess diagnostics locally,
but its default MCP projection contains only the contract fields, opaque
ids, fixed copy, and schema-limited metrics. Every outcome path shares
this projection, so an exception cannot turn into an egress channel.

## Sandbox

With no `--bundles`, the server serves one synthetic workflow,
`add_triage_note`, on MockMed, the synthetic clinic app that ships with
openadapt-flow. The `sandbox_case` input selects what the app does:
`normal` (done), `duplicate_record` (needs_review), `false_saved_banner`
and `timeout_after_save` (not_sure_if_saved), and `app_offline`
(did_not_run). Results always carry `mode: "sandbox"` and the engine name.

- The `flow` engine needs the `tutorial` extra. In a background thread at
  start, Flow records the workflow once (reused afterwards), compiles it
  with mined record checks, binds the mined `note` read-back to the `note`
  parameter (the operator review Flow leaves open after mining), and
  certifies it under `clinical-write`. Each run is admitted by Flow's run
  gate under the standard profile and executed by Flow's `Replayer` with
  the caller's note, a right-record check, and an independent read of
  MockMed's record store. This is the same path Flow's own tutorial uses.
- The `simulated` engine returns the same contract results without opening
  any app. Results open with "Simulated sandbox result" and `done` carries
  `proof: "simulated"`.

`--sandbox-engine auto` picks `flow` when Playwright and a compatible Flow
are installed, and falls back to `simulated` when the app can't start. The
sandbox keeps its records under `~/.openadapt/agent-sandbox` (or
`OPENADAPT_AGENT_SANDBOX_DIR`, or `--runs-dir`), so it never writes into
the project a client was launched from.

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
/ Claude.ai cannot talk to localhost. Send those tabs to
https://openadapt.ai/start. They still can't click the user's GUI.
Pip users run `openadapt-agent
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
- the four-outcome contract: every reason against the output schema, real
  Flow 1.35.1 outcome shapes, pause-aware mapping, and the rule that only
  proven no-effect results allow a retry;
- `request_id` replay, conflicts, retry limits, interrupted runs, and
  reviewed runs a person resolves;
- `outputSchema` and `structuredContent` on MCP SDK 1 and SDK 2;
- workflow cards: Flow types, operator text, identifier guard, closed input
  problems;
- every sandbox case with the simulated engine, and, in the opt-in
  `sandbox-e2e` job, with real Flow, a real browser, and MockMed;
- success/halt/refusal/timeout outcome mapping for the older tools;
- MCP serialization and thread ownership;
- Agent Skill emission without recorded values, step intents, or secrets,
  and one description per workflow;
- `--authoring` probe tools (`observe`, `start_record`, `click`, `halt`)
  and local `type`; observe projection drops values/titles/screenshots
  and extra keys, caps the wire at 32 KiB, and uses `n_` + 8 hex node
  ids; pause Continue uses `record_observed` rather than `type_text`;
  compile returns `needs_human_admit`; `admit` is the one-token human
  ok; `--authoring` does not enable
  run tools; `server.json` stays stdio with `--bundles` required;
  `authoring connect` parses `openadapt://runner` / pack URLs, claims
  `oab_`, polls `wait_seconds: 0`, prompts Allow-per-`sub`, and Continue
  uses `record_observed` (never `type_text`).

CI runs on Python 3.10, 3.11, and 3.12. It also builds the wheel and
sdist, verifies MIT metadata and license inclusion, and refuses package
artifacts containing repository-only copyleft benchmark material.
