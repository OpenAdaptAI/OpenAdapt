# Governed MCP tool observations

This contract covers portable tools emitted by `emit-mcp`: web, Windows, macOS,
Linux, RDP and Citrix use one adapter and the ordinary governed `run` CLI. The
compiled JIT MCP emitter uses its separate in-process compiler, admission and
record/receipt path; it does not inherit this adapter's assurance or status.

Supply the deployment, Standard profile and existing qualification authority at
server startup. Call parameters remain data. Any non-null legacy URL override is
refused before a child starts, including an empty string or the configured URL.
The deployment owns the target and read-back configuration. Existing source,
instance, qualification, admission and currentness checks are mandatory in `run`.
A challenge grants none of those authorities.

The current adapter supports only an ordinary serial Standard local invocation.
Regulated, managed/qualification-case, repair, worker-pool and multi-surface
producer modes are unsupported and refuse before launch where their configuration
is observable. An older CLI rejects the unknown challenge flag before actions.
Missing or unsupported completion after launch remains uncertain. No fallback
browser, direct Replayer execution or automatic retry is provided.

Every tool result has `success: false`, `production_eligible: false` and
`retry_allowed: false`:

| Outer outcome | What the adapter observed |
| --- | --- |
| `REFUSED` | This tool launched no child. `execution_may_have_occurred` is false. |
| `UNCERTAIN` | A child launch was attempted. Actual effects may have occurred; reconcile before another attempt. `transaction_outcome` is `RECONCILIATION_REQUIRED`. |

A matching local result may also contain a closed `runtime_reported` object with
the runtime's outcome, transaction outcome, success, eligibility and completion
claims. They remain claims. No provider error, stdout/stderr, arbitrary result
string, qualification field or cost counter is copied into this object. Missing,
malformed, stale or mismatched reports have no reflected runtime claim.
`producer_assurance` is a closed machine enum: `NOT_LAUNCHED`, `UNATTRIBUTED`, or
`LOCAL_OWNED_PROCESS_UNAUTHENTICATED`.

The adapter creates a fresh nonsecret challenge for each call. The common
producer derives the descriptor from the actual parsed deployment, resolved
backend, effective read-back/actuation sections and profile, then includes that
context at initial RunReport creation, before persistence or sealing. The adapter
compares it to its independently captured request and target. The input digest
stays separate. Credential references can affect configuration identity; resolved
credential values are never read for this descriptor. The challenge does not
replace the runtime RunID, idempotency identity or qualification run identity.
An old cached report is not stamped with a fresh context.

This is attribution inside a trusted local OS identity/process boundary. It does
not authenticate a plaintext report, a child/package, or another writer with the
same OS identity. The adapter does not verify an independently pinned producer
signature or offline Seal. Authenticated decryption of a sealed set does not by
itself provide a separately pinned producer identity. The outer production hold
therefore applies to both plaintext and sealed reports. A separately governed,
pinned authenticated-producer/report contract is a prerequisite before stronger
outer claims can be enabled; embedded keys and self-asserted flags cannot supply
it. No key creation, registry or signing mechanism is added here.

Evidence is source-local and synthetic. No genuine qualification, effect on a
real application, authenticated production report or complete MCP-stream
qualification is established by this repair.
