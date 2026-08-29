# From trace to program

A single demonstration under-specifies intent, so openadapt-flow does not stop
at replaying one. These capabilities layer onto the same $0, model-free runtime.

- **A workflow *program*.** Beyond the linear v0
  bundle, the IR (`openadapt_flow/ir.py`) expresses a parameterized program:
  states and guarded transitions, loops over worklists, subflows, typed
  parameters, predicates, and exception paths (`ProgramGraph` / `State` /
  `Transition` / `LoopSpec` / `Guard` / `Predicate` / `ParamSpec`). The flat
  trajectory is the degenerate case, so the migration is backward-compatible.
  Design: [`design/WORKFLOW_PROGRAM_IR.md`](design/WORKFLOW_PROGRAM_IR.md).
- **A data-driven loop from one demonstration.** `for-each` wraps a single
  linear bundle's body in one governed LOOP that runs once per record of a
  worklist (CSV or JSON), binding each record's columns to the workflow's
  parameters. Every iteration keeps the same gates: bounded by a hard
  `--max-iterations` cap, identity-checked and effect-verified per record,
  halting on an ambiguous or refuted write instead of skipping it. The
  column-to-parameter mapping is explicit and validated, so an unmapped
  column, a bound parameter with no value, or a ragged worklist fails at
  authoring time rather than emitting a bad bundle. This turns a replay of
  one recorded path into governed execution over a queue:
  `openadapt-flow for-each bundle --records worklist.csv --out queue-bundle`.
- **Compose separately recorded bundles.** `compose` takes named compiled
  child bundles and a handoff contract and writes a parent artifact that
  `certify` and `run` execute. Each child keeps the surface it was recorded
  on. The parent runs children in `--child` order, or an explicit `--after`
  DAG, and starts a child only after every predecessor ends `VERIFIED` (or an
  `--allow-halt` class you named). Handoffs copy effect-bound parameter
  values from the predecessor's confirmed effect receipt. Missing evidence
  HALTs. The launcher form is `openadapt flow compose`.
- **See what a demonstration compiled into.** `visualize` renders a
  program-graph view of a bundle before it runs: the ordered steps, the
  resolution ladder each step will try, where an identity gate is armed,
  which writes carry an effect check, and every point the run can halt. Emit
  a self-contained offline HTML page, Mermaid for docs, or the shared JSON
  graph spec that the Cloud and desktop surfaces render
  (`openadapt-flow visualize bundle -o graph.html`). See
  [`VISUALIZE.md`](VISUALIZE.md).
- **Multi-trace induction that refuses when it isn't sure.** `induce_program`
  aligns several demonstrations of the same task to recover the shared
  parameters, loops, and branches, deterministic and model-free at its core.
  When a branch condition or a value stays underdetermined it *quarantines* the
  program (`certified` is `False`) instead of guessing, and `disambiguate`
  surfaces the ambiguity as concrete multiple-choice questions rather than
  inventing an answer.
- **Effect verification against the system of record.** The screen can lie: an
  optimistic UI, a duplicate submit, a partial save all read as success. A step
  may declare typed `effects`, and when a run is given an `EffectVerifier` the
  replayer checks the *real* record out of band, before and after the action,
  halting on a refuted or unverifiable write, still with zero model calls. The
  oracle is pluggable: SQL, REST (`RestRecordVerifier`), FHIR
  (`FhirEffectVerifier`), or a document hash (`DocumentHashVerifier`). A
  fault-model study found the screen-only oracle silently mishandles 5 of 7
  transactional fault classes; all five halt through the real replayer once
  effects are declared ([`../benchmark/fault_model/FAULT_MODEL.md`](../benchmark/fault_model/FAULT_MODEL.md),
  [`design/EFFECT_VERIFIER.md`](design/EFFECT_VERIFIER.md)). Two honest
  preconditions bound this: the compiler does **not** yet infer effects from a
  demonstration (they are authored per deployment against the app's system of
  record), and a run with **no** verifier configured falls back to the screen
  oracle. The net exists only when both are supplied; without them the write is
  exactly as silent as before.
- **An API actuator tier.** Where the target app exposes a real API, driving its
  GUI to make the write is the wrong tool. A step carrying an `ApiBinding`, with
  an `ApiActuator` configured, performs the write by calling the API
  deterministically and confirms it with the same `EffectVerifier`, the `api`
  leaf of the capability ladder (API, then DOM/UIA, geometry, OCR, template,
  VLM, human). It is an optimization whose safe fallback is always the GUI.
- **Policy: lint and certify.** `lint` reports a bundle's coverage gaps (unarmed
  clicks, vacuous postconditions, under-classified risk) with a severity each;
  `certify` enforces a policy and exits nonzero, refusing a bundle before it
  deploys. Runnable is not the same as certified safe. Certification is
  **optional and opt-in** (an uncertified bundle still runs), and a policy only
  defines what a bundle must satisfy, so the honest claim is that *a certified
  workflow can be configured to halt* on the conditions its policy names, not
  that any workflow always halts.
- **Governed healing.** Every fix under drift lands in the bundle as a reviewable
  diff, and a step classified irreversible will not act on a low-confidence
  match. Structure and the identity gate govern the heal; they are not bypassed
  by it.
- **Durable checkpoint / resume.** A run checkpoints verified progress
  (`openadapt_flow/runtime/durable/`) so a halt becomes a durable pause the
  operator can approve and resume from the last verified state (`resume` /
  `approve`), not a restart, and explicitly not "hand the rest to a free-form
  agent."
- **PHI-free identity.** The wrong-patient identity check can run against a
  salted-hash, shape-preserving `IdentityTemplate` instead of a plaintext
  name / DOB / MRN band, so a compiled bundle need carry no readable PHI while
  still enforcing identity (`openadapt_flow/runtime/identity_template.py`).

Compiled workflows can also be emitted as Agent Skills or MCP servers
(`emit-skill` / `emit-mcp`), so other agents can invoke them.

## Answer a halt from a phone

An attended run can project one signed operational-halt task to an
authenticated phone view. The task identifies one exact tenant, runner, run,
pause, capability, bundle, event sequence, expiry, and idempotency scope. A
negotiated V2 task also binds the qualification project, revision, contract,
and exact step. It carries only closed enums, bounded counts, digests, and a
reviewed remote-safe entity class. A custom or missing class becomes the
signed neutral `record` or `item` label. The runtime does not infer that label
from a screenshot, OCR, a parameter, an application name, or a model.

The phone shows only the actions in the sealed pause capability. Depending on
the exact halt, those can include verify and resume, skip, reject, teach,
escalate, or reconcile. A tap does not actuate the target application and does
not prove success. The hosted route supplies an AAL2-authenticated principal
bound to the exact tenant and runner. The customer-controlled runner matches
the current pause again, acquires the action lease, reads a fresh live state,
and repeats the required session, identity, target, postcondition, and effect
checks before it continues. Reconcile performs a read-only effect check after
uncertain delivery; it never repeats the possibly completed action.

The hosted lane uses outbound HTTPS and sends no screenshot. The separate
Desktop portal can show protected evidence through a customer-operated HTTPS
boundary. Try the shared hosted interface with synthetic application data at
[app.openadapt.ai/demo/attention](https://app.openadapt.ai/demo/attention), and
read the exact delivery and data-boundary contract in
[`DECISION_DELIVERY.md`](DECISION_DELIVERY.md). A declared finite
business-policy choice is a different state and receipt contract; see
[`BUSINESS_DECISIONS.md`](BUSINESS_DECISIONS.md).
