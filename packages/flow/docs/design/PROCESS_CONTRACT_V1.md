# ProcessContract v1 design

ProcessContract v1 extends the existing parent with code children, human
children, and verified artifact edges. The implementation stays in Flow. There
is no second App scheduler, case database, or success vocabulary.

## Design decision

An OpenAdapt App is a sealed ProcessContract v1 directory. The name describes
the package a user installs. The runtime contract remains ProcessContract.

This choice puts more work into one parent, but it prevents two worse costs:
duplicate pause and resume rules, and an audit record split between Flow and a
new orchestrator. A general workflow platform would add a canvas, scheduler,
and service boundary before one tax procedure has proved that they are needed.

## Contract ownership

Flow owns the process graph, the durable projection, the event hash chain, and
the terminal receipt. Each child keeps its narrower authority.

The Flow child owns GUI qualification and effect evidence. The code admission
owns the exact archive, lockfile, runtime, permissions, typed I/O contracts,
effect contract, oracle contract, and qualification campaign. The attended
runtime owns operator authority and completion. The authentication verifier
owns session proof. No child can grant another child more authority.

The portable wire models live in `openadapt-types` 0.13.0. Flow adds only the
graph fields needed to connect them.

## Execution state

The runner derives a package digest from canonical contract JSON and the live
digests of each referenced admission, manifest, source archive, and
authentication template. A new run creates an opaque execution ID and the
first event. Each child completion adds one event with its receipt digest. The
state projection records the same event head.

Resume checks the full chain before it reads the next child. It also checks the
process digest. This gives the local prototype deterministic recovery without
adding Temporal or another service.

The ledger stores no chat state. It needs no authoring model to resume.

## Code boundary

The built-in executor accepts only Python and the `trusted_local` isolation
profile. It verifies the Ed25519 admission, archive digest, lockfile digest,
Python major and minor version, runtime-environment digest, direct entry point,
and declared output limits. It invokes the script with an argument vector.
There is no shell and no dependency install.

`trusted_local` means the operator admits the exact code and accepts the host
boundary. It isn't a claim that Python can enforce network or process-spawn
policy by itself. A container or VM executor must prove those stronger
profiles before Flow will run them.

Transform code returns `COMPLETED_UNVERIFIED`. A verifier is another sealed
code child with its own oracle and qualification digests. Its fixed output
lists the artifact digests and human receipt digests it checked, plus the
reached oracle tier. The parent requires exact set matches. One omitted or
extra digest refuses the result.

## Artifact boundary

Artifact identity is content based. The shared `ArtifactRefV1` records storage
boundary and data class but no path. The local projection keeps the resolver
path inside the run directory. The runner checks that path and re-hashes the
file before each transfer.

An edge names producer, logical output, consumer input, and verifier. The
runner can give pending bytes to that verifier. No other child receives them
until the verifier receipt marks the exact digest verified.

The graph doesn't pass credentials. Secrets remain named permissions in the
code contract or external references in the deployment boundary.

## Human and authentication boundary

A human child issues `HumanDecisionTaskV1` from the same HMAC-backed task
contract used by the attended surfaces. The task has a closed action set. A
human completion must bind the process digest, run, pause, request, decision,
and transition receipt.

The command-line adapter reads one closed local completion envelope. The
envelope is transport only. It gives no authority by itself. The signed shared
receipts remain authoritative, so a plain Done file cannot resume the process.

Authentication adds a second receipt because a general completion receipt
doesn't contain session freshness or principal-class evidence. The second
receipt stays value-free and binds the exact surface. A browser receipt cannot
resume a Windows, RDP, or Citrix task.

The local provider supplies the live run binding. That provider is part of the
trusted execution boundary. A JSON file supplied by an untrusted caller isn't
enough.

Source-time capture exclusion remains a separate requirement. The current
contract demands its receipt digest but doesn't implement Capture behavior.
The tax recording procedure must start after authentication until the shared
Capture, Desktop, and attended-runtime path is qualified.

## Outcome rules

Only all-verified children and all-verified artifact edges can produce a
signed `VERIFIED` parent receipt. The parent derives its oracle tier from the
lowest child tier. It also requires zero model use. A signed human review or
decision is the declared effect. Authentication needs its live verifier. Human
actuation remains `COMPLETED_UNVERIFIED` until a downstream verifier names its
receipt digest.

Delivery uncertainty keeps the exact `RECONCILIATION_REQUIRED` result. It
stops the graph. The parent never changes it to `HALTED_BEFORE_EFFECT`, because
that would assert knowledge that the runtime doesn't have.

An admitted transform can finish without a verifier. The process cannot. If a
declared output remains pending at the end, the parent returns
`FAILED_PLATFORM` and emits no success-shaped receipt.

## Package and author portability

The package contains JSON contracts, signed admissions, archives, and Flow
bundle references. None of those fields names an authoring vendor. Tests assert
digests and receipts rather than prompts.

MCP, Agent Skills, Desktop, and a command line can all invoke the same parent.
They are access surfaces. The process ledger stays on the runner.

The first acceptance target is still the tax procedure: three complete runs,
zero silent incorrect completions, no retained password or MFA, resume after
each human task, a digest for every produced file, and reconciled final totals.
A second unrelated procedure must reuse the same child types before this format
gets a larger product surface.
