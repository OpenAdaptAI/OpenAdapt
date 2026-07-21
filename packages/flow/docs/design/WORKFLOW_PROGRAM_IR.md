# Workflow-program IR: control flow, effects, induction, capability-adaptive compilation

**Status:** Implemented (bundle schema v2). This document specifies the IR that
ships in `openadapt_flow/ir.py` and the runtime that interprets it, and it
records the roadmap for the parts that still need a real customer workflow to
finish.

**Scope:** the compiled artifact and its execution model. A bundle is no longer
only a linear action list. It is a parameterized workflow program: a state
machine with typed parameters, guards, loops, branches, subflows, system-of-record
effects, and exception handlers. Today's linear workflow is the degenerate case
of that program, so old bundles keep running unchanged.

**Audience:** maintainers and integrators who need the precise shape and
semantics of a bundle, and researchers deciding whether the IR is worth writing
up.

**History:** this began as a design RFC (PR #61) that proposed the IR before any
code existed. Most of it is now built (PRs #61, #81, #188, and the effect and
induction work that followed). This revision rewrites the document to describe
what exists, marks the small remainder as roadmap, and adds a formal semantics
section and a schema summary.

---

## 1. Summary

A recording captures observed input events plus before-and-after frames. One
recording underdetermines what the operator meant. Programming-by-demonstration
(PBD) research spent decades on the same problem and reached one answer: treat a
demonstration as evidence, not as a specification, and compile it toward a
program with control flow. That program is the artifact PBD was always trying to
recover.

The IR encodes that program directly:

1. A **workflow program**: states, guarded transitions, an action leaf per
   action state, typed parameters, loops over worklists, branches, subflows,
   `wait_until` predicates, exception handlers, and per-action risk. A linear
   `Workflow` is the degenerate program (one entry state, unconditional
   fall-through, no loops), so the model is backward compatible.
2. **Typed effects** verified against a system of record, not the screen. A
   vision postcondition asks whether the pixels look like a save happened. An
   effect asks whether the intended record is actually present, exactly once,
   with the right field values.
3. **Capability-adaptive actuation**: one semantic action carries an optional API
   binding and a structural locator, so it can run as a deterministic API call,
   a structural DOM or accessibility action, or the visual resolution ladder,
   each satisfying the same contract.
4. **An induction pipeline** that turns one or more demonstrations into a
   validated program or an honest refusal, with all model use confined to
   compile time.

The runtime that walks this IR makes zero model calls on the healthy path and
costs nothing per run. Control flow is added around the hardened action leaf; it
does not replace it.

---

## 2. Why one demonstration underdetermines intent

The compiler already knows a single trace is ambiguous. Most of its work exists
to guess which observed facts are invariant and which are incidental.

- **Which literals are parameters.** The recorder tags a typed value as a
  parameter only when the demo author marks it. The compiler then strips those
  values from every assertion so they do not ossify into the bundle (the
  `exclude_texts` machinery and the `lint_param_leakage` build gate). The inverse
  question, "was this untagged value actually a parameter the author forgot to
  mark," is not answerable from one trace. The demo shows `note = "Follow-up in
  2 weeks"`; nothing in one run says whether the patient, the encounter type, or
  a priority filter was meant to be fixed or free.
- **What is incidental.** Volatility mining (`openadapt_flow/volatility.py`)
  exists to decide which observed screen text is incidental (clocks, counters,
  near-dates) versus invariant (a date of birth), because one frame cannot tell
  you. It is a heuristic proxy for a question that only multiple traces or the
  operator can answer.
- **Absent results.** A successful trace never shows the failure branch. The
  transactional-fault study found 5 of 7 write-fault classes silently mishandled
  (`docs/LIMITS.md`). The demo shows what did happen, not what should happen when
  the result is absent or wrong.
- **Loops over worklists.** A demo that clears one referral from a queue of
  fifteen does not, by itself, say "repeat for every open task." The iteration is
  invisible in a straight line.
- **Essential versus incidental order.** A trace imposes a total order. The
  intent is usually a partial order: fill three independent fields in any order,
  but save last.
- **Expected versus exceptional popups.** A survey modal that sometimes appears
  after Save is not drift. It is a conditional branch the operator handles. One
  trace cannot tell an error popup apart from a normal optional step.

### The lineage: recover the program, not the trajectory

This is the founding premise of programming-by-demonstration, and the field's
arc is trajectory to program.

- **Straight-line record-and-replay (Ringer et al.).** Reliable low-level replay
  of one recorded trace, which is where openadapt-flow's action leaf sits. In the
  web-automation lineage, straight-line replay was the input to a generalizer,
  not the end product.
- **Rousillon / Helena (Chasins et al.).** Generalized straight-line replay into
  loops over relational data: demonstrate scraping one row, induce "for every
  row, for every page."
- **WebRobot (PLDI 2022).** Synthesizes loopy programs from a demonstrated action
  sequence by proposing a loop body consistent with the prefix and validating it
  against continued demonstration.
- **PROLEX.** Recovered the operator's intended program from a single
  demonstration in 81% of solved cases, which is evidence that one-demo-to-program
  is tractable when paired with disambiguation.
- **Skill-DisCo.** Compiles parameterized finite-state-machine subgraphs from
  multiple traces, with typed parameters, pre and postconditions, and side
  effects. That is close to the target IR of section 5, learned from a small set
  of traces.
- **Agent Workflow Memory / Agent Skill Induction.** Induce reusable branching
  skills from experience and reuse them as higher-level actions, which is the
  case for emitting skills and MCP tools as parameterized programs rather than
  fixed replays.

The action leaf is an unusually strong straight-line replayer with a safety net.
The research consensus is that the straight line is step one and the value is in
the generalizer that turns it into a program.

---

## 3. The action leaf (unchanged, hardened)

The program IR sits above the existing action machinery. A `Step` is still the
unit of action, and its fields are unchanged.

- `ActionKind`: `click`, `double_click`, `type`, `key`, `wait`, `scroll`.
- `Anchor`: redundant evidence for locating a target, consumed by the resolution
  ladder in descending order of fidelity: a structural locator (DOM selector or
  role plus name, or a Windows UIA `AutomationId`), then a local template match,
  a global template match, an OCR text anchor, landmark geometry, and an optional
  grounding model as the last rung.
- Identity evidence rides on the anchor: `context_text` (an OCR band), a
  `structured_identity` string, a PHI-free `IdentityTemplate` (salted-hash and
  shape, no plaintext name, date of birth, or MRN in the artifact), and an
  optional `identifier_crop` for the pixel-compare identity tier on pixel-only
  substrates.
- Safety metadata on the step: `risk` (`reversible` or `irreversible`),
  `identity_armed`, `identity_unarmed_reason`, and `identifier_crop_missing_reason`
  record and audit the identity gate coverage before a run.

Nothing in the state machine changes the action leaf. It adds control flow around
it. A structurally-resolved target still flows through the same click path, so
the pre-click identity gate and the irreversible-risk gate still fire.

---

## 4. Typed parameters, predicates, guards, effects

These are the Phase 1 pieces. Each is additive: a bundle that declares none of
them loads and replays exactly as a v0 bundle.

### 4.1 Typed parameters

`Workflow.param_specs` is a map of name to `ParamSpec`, alongside the frozen
`Workflow.params` dict. A `ParamSpec` carries a `type` (`ParamKind`), the recorded
demo value in `example` (which doubles as the replay default), `required`, and
`choices` for an enum.

`ParamKind` is `string`, `date`, `enum`, `number`, or `entity_ref`. An
`entity_ref` names an entity to be re-resolved by the identity ladder at run time,
not a literal to substitute. It is the typed form of the "which patient" fix: the
parameter selects a record by identity rather than by a recorded pixel position.
Phase 1 stores the type for validation and emit; run-time re-resolution of an
`entity_ref` inside a loop is described in section 5.3.

### 4.2 Predicates

A `Predicate` is a deterministic condition over the current frame and run
parameters. It is evaluated with zero model calls, and an unknown kind fails safe
(does not hold). `PredicateKind`:

| Kind | Meaning |
|------|---------|
| `anchor_resolves` | the embedded `anchor` resolves on the current frame via the model-free ladder (the closed-loop scroll stop condition, now first-class) |
| `text_present` | `text` is present on the current frame (tolerant OCR presence check) |
| `text_absent` | `text` is not present on the current frame |
| `param_equals` | the run's value for `param` equals `value` (string compare) |
| `and` / `or` / `not` | boolean composition over `operands` |

A predicate is used two ways: as a `Step.wait_until` readiness predicate that the
replayer polls (bounded by `timeout_s`, halting on timeout, never proceeding
anyway), and as the condition inside a guard.

### 4.3 Guards

A `Guard` is a precondition on a step. Its `predicate` is evaluated on the step's
entry frame. When it does not hold, `on_unmet` decides: `halt` (the default, the
safe direction for an unmet precondition) stops the run naming the step, and
`skip` makes the step a no-op success. `skip` is the expected-but-optional case:
dismiss a survey modal only when it appeared. Full multi-way branching is the
state machine in section 5.

### 4.4 Effects (system of record)

`Step.effects` is a list of typed `Effect` contracts verified against the real
system of record after the action runs, not against the screen. This closes the
transactional gap the vision postconditions are blind to. Verification is done by
the run's configured `EffectVerifier`; a non-confirmed verdict halts the run. The
design and substrates are specified in `docs/design/EFFECT_VERIFIER.md`.

`EffectKind` today is `record_written` and `field_equals`:

- `record_written`: a record matching the `match` selector must exist exactly
  `expected_count` times (default once). This catches missing, phantom,
  duplicate, and double-click writes. Options tighten it: `count_new_only` counts
  only records new relative to a pre-action snapshot, `idempotency_key` collapses
  a retried submission to one record, and `forbid_collateral_loss` refutes when a
  concurrent actor's row silently vanished (a lost update).
- `field_equals`: the matched record must carry `field == value`, a read-back
  that catches a partial save that persists the row but drops a field.

Each selector value and each `value` is a `ValueExpr`: a static `literal` or a
run `param` reference. A parameterized workflow therefore verifies the record it
actually wrote this run, not the record baked in at demonstration time. A v1
bundle's bare string is coerced to `ValueExpr(literal=...)` and behaves
identically.

Two mining outputs make effects usable without an API. A `ReadbackSpec` records
an on-screen read-back oracle mined from the demonstration; its `different_path`
variant re-opens the record by an independent navigation before reading, which
defeats the "the form still shows what I typed but nothing persisted" phantom
save and is safe enough to be the default oracle. When the compiler cannot derive
a real binding it emits a placeholder effect with `needs_operator_confirmation`
set, and the runtime treats a placeholder as fail-safe (halt) until an operator
completes the binding rather than trusting a fabricated endpoint.

### 4.5 API binding

`Step.api_binding` is an optional `ApiBinding`: a declarative description of the
API call that performs the step's write without the GUI. When a step carries a
binding and the run configures an `ApiActuator`, the runtime performs the write by
calling the API deterministically (zero cost, zero model calls), confirms it with
the same `EffectVerifier` that gates a GUI write, and skips the GUI resolve and
act for that step. The binding is REST/JSON first but shaped so a FHIR, MCP, or
tool call fits the same model (`kind` selects the substrate). It is additive: a
bundle with no binding, or a binding with no actuator configured, actuates through
the GUI ladder exactly as before. The API tier is an optimization whose safe
fallback is the GUI, never a gate that can block a runnable step.

---

## 5. The state machine

These are the Phase 2 pieces: the control flow a linear list cannot express. They
are built additively on the Phase 1 parts. A state's action is a Phase 1 `Step`, a
transition's guard is a Phase 1 `Predicate`, and a branch reuses the same
model-free predicate evaluation. `Workflow.program` is optional: when it is `None`
the runtime executes the linear `steps` loop, and a linear bundle lifts
mechanically to the degenerate single-path graph.

### 5.1 States

`StateKind` and its payload:

| Kind | Payload | Behavior |
|------|---------|----------|
| `action` | `step: Step` | perform the hardened action leaf, then take a transition |
| `branch` | (none) | perform no action; pick an outgoing edge by guard |
| `loop` | `loop: LoopSpec` | iterate a worklist, running a body subflow per row |
| `subflow_call` | `subflow: str` | invoke a reusable named subflow, then continue |
| `terminal` | `outcome`, `reason` | end this (sub)graph |

A `State` also carries `transitions` (outgoing edges) and `on_exception` (a local
handler; a failed action routes there instead of aborting the whole run). A
terminal's `outcome` is `success` (complete normally; a subflow returns to its
caller), `halt`, or `escalate` (both stop the entire run with `success=False`).

### 5.2 Transitions

A `Transition` is a guarded edge to a `target` state, with an optional
human-readable `label`. Its `guard` is a `Predicate`, or `None` for an
unconditional edge (the `TRUE` edge, and the only edge a degenerate linear program
has). A state's transitions are evaluated in order and the first whose guard holds
wins. Multiple non-`None` guards make a multi-way branch.

### 5.3 Relations and loops

A `Relation` is a worklist a `loop` iterates: a `name`, a variable-length list of
`rows` (each a map of param name to value), and a `description`. Rows may be
inlined in `Workflow.data_sources` (the authored or compiled case) or supplied at
run time (`Replayer.run(worklists=...)`) for a genuinely data-dependent queue.

A `LoopSpec` binds a `relation` and a `body` subflow that runs once per row, the
row's fields merged into the run params for that iteration. A zero-row worklist
runs the body zero times. Iteration is bounded by `max_iterations` (default 1000):
a worklist longer than the bound halts fail-safe, never running unbounded. When
the loop variable is an `entity_ref` param, it re-resolves through the identity
ladder each pass, so iteration N acts on the right row rather than a recorded
pixel position.

### 5.4 Program graph and the degenerate lift

A `ProgramGraph` is a directed graph of states with a single `entry`, used both as
the top-level program (`Workflow.program`) and as a reusable subflow
(`Workflow.subflows[name]` or a loop body).

`lift_to_program(workflow)` mechanically lifts a linear `Workflow` to the
degenerate straight-line graph: each `Step[i]` becomes an `action` state with a
single unconditional transition to `Step[i+1]`, ending in a `success` terminal.
The graph interpreter over this lift replays byte-for-byte identically to the
linear replayer. That equivalence is what makes the whole migration reversible:
every v0 bundle is a valid program with an empty control-flow layer.

---

## 6. Operational semantics

This section states precisely how the runtime walks a program, so the IR has a
defined meaning independent of the current implementation details. It reflects the
interpreter in `openadapt_flow/runtime/replayer.py`.

### 6.1 The interpreter walk

Execution starts at `program.entry` and walks state by state. For the current
state:

- **terminal.** Stop this graph. `success` completes: at the top graph the run
  ends successfully, and inside a subflow it returns control to the caller. `halt`
  and `escalate` stop the entire run with success set false and record the
  terminal `reason`.
- **action.** Run the state's `Step` through the shared per-step pipeline
  (resolve target, pre-click identity gate, act, verify postconditions and
  effects). If the step succeeds, record a durable checkpoint (section 8), then
  select the next transition. If it fails and the state has an `on_exception`
  handler and the failure is not a safety halt, route to the handler and mark the
  result `exception_handled`. Otherwise raise a halt. A safety refusal (identity,
  effect, or postcondition) is never caught by `on_exception`; the `safety_halt`
  flag forces the run to stop.
- **branch.** Perform no action. Select a transition purely by guard. No matching
  edge routes to `on_exception` if present, otherwise halts.
- **loop.** Resolve the worklist (a run-time `worklists` entry overrides the
  inline `data_sources` relation; an undefined relation is a config halt, and a
  defined but empty relation legitimately runs the body zero times). If the row
  count exceeds `max_iterations`, halt. Otherwise walk the body subflow once per
  row with the row merged into the params in scope, then select the loop state's
  transition.
- **subflow_call.** Walk the named subflow to completion (an undefined subflow
  halts), then select the calling state's transition.

Falling off the top graph with no explicit terminal is treated as a clean
`success`.

### 6.2 Transition selection

Given a state's `transitions`:

1. No transitions: return nothing (fall off the current graph, or return from a
   subflow).
2. All guards are `None`: take the first target with no screenshot. This is what
   lets the degenerate all-unconditional chain replay with no extra frame settles,
   the byte-identical linear lift.
3. Otherwise settle the frame once, evaluate guards in order, and take the first
   target whose guard holds.
4. Guards exist but none hold on the current screen: halt fail-safe (never guess
   an edge).

### 6.3 Predicate evaluation

A predicate is evaluated over the current frame and run params with zero model
calls. `anchor_resolves` runs the model-free resolution ladder and holds when the
anchor resolves. `text_present` and `text_absent` are tolerant OCR presence
checks. `param_equals` is a string compare against the run's param value. `and`,
`or`, and `not` compose `operands`. An unknown kind does not hold, which makes an
unrecognized guard fail toward halt rather than toward an unintended edge.

### 6.4 Effect verdicts

An `EffectVerifier` returns one of three verdicts, mirroring the identity gate:

- `CONFIRMED`: the effect is present and correct. Proceed.
- `REFUTED`: the system of record affirmatively contradicts the effect (missing,
  duplicated, wrong value, or collateral loss). Halt; never accept as success.
- `INDETERMINATE`: the system of record is unreachable or unreadable, so the
  effect cannot be certified. Halt; never assume success.

Both non-confirmed verdicts set `should_halt`. There is no "probably fine": an
unverifiable consequential write halts, exactly as an unreadable identity band
does. A declared effect with no verifier configured is a deployment error that
halts, never a silent unverifiable write.

### 6.5 Invariants

- **Determinism and zero model calls on the healthy path.** Guards, branches,
  loops, and subflow dispatch are deterministic. The healthy run makes zero model
  calls and costs nothing per run. Any model use is compile-time only, plus the
  bounded and counted Tier-2 recovery in section 8.
- **Bounded execution.** Loops are bounded per loop by `max_iterations`, subflow
  recursion is bounded by an interpreter depth limit, and each `wait_until` is
  bounded by its `timeout_s`. No construct runs unbounded.
- **Fail-safe direction.** Every underdetermined or unverifiable point halts. An
  unmet guard defaults to halt, a dead-end branch halts, an unknown predicate does
  not hold, and a placeholder effect halts until an operator completes it.
- **Safety refusals are not exceptions.** An identity, effect, or postcondition
  refusal sets `safety_halt`, and an `on_exception` handler cannot turn it into a
  successful terminal.

---

## 7. Schema summary

The models in `openadapt_flow/ir.py` are the normative schema, and
`Workflow.model_json_schema()` emits the machine-readable JSON Schema from them.
The sketch below is the shape, not a second source of truth.

```text
Workflow
  schema_version: int = 2
  name: str
  params: dict[str, str]                 # frozen v0 name -> example
  param_specs: dict[str, ParamSpec]      # typed, additive
  secret_params: list[str]
  steps: list[Step]                      # linear body; the degenerate program
  program: ProgramGraph | null           # the state machine (null => linear)
  subflows: dict[str, ProgramGraph]      # reusable subgraphs / loop bodies
  data_sources: dict[str, Relation]      # inline worklists
  manifest: BundleManifest | null        # v2 integrity + provenance
  contains_phi / phi_scrubbed / encrypted: bool

ParamSpec
  name: str
  type: "string" | "date" | "enum" | "number" | "entity_ref"
  example: str | null                    # recorded demo value = replay default
  required: bool = true
  choices: list[str]                     # enum only

Predicate
  kind: "anchor_resolves" | "text_present" | "text_absent"
      | "param_equals" | "and" | "or" | "not"
  anchor / text / param / value / intent: optional per kind
  operands: list[Predicate]              # and / or / not
  timeout_s: float = 5.0                 # wait_until bound

Guard { predicate: Predicate, on_unmet: "halt" | "skip" }

Step
  id, intent, action(ActionKind)
  anchor: Anchor | null
  text / param / key / scroll_dx / scroll_dy: optional per action
  secret: bool
  expect: list[Postcondition]            # vision assertions
  effects: list[Effect]                  # system-of-record assertions
  api_binding: ApiBinding | null
  wait_until: Predicate | null
  guard: Guard | null
  risk: "reversible" | "irreversible"
  identity_armed / identity_unarmed_reason / identifier_crop_missing_reason

Effect
  kind: "record_written" | "field_equals"
  match: dict[str, ValueExpr]            # record selector
  field / value: for field_equals
  expected_count: int = 1                # record_written
  idempotency_key / key_field / count_new_only / forbid_collateral_loss
  readback: ReadbackSpec | null          # on-screen oracle
  needs_operator_confirmation: bool
  risk, probe, timeout_s

ValueExpr { literal: str | null, param: str | null }   # exactly one meaningful

ProgramGraph { entry: StateId, states: dict[StateId, State] }

State
  id, kind: "action" | "branch" | "loop" | "subflow_call" | "terminal"
  step: Step | null                      # kind = action
  loop: LoopSpec | null                  # kind = loop
  subflow: str | null                    # kind = subflow_call
  transitions: list[Transition]          # empty on terminal
  on_exception: StateId | null
  outcome: "success" | "halt" | "escalate" | null   # kind = terminal
  reason: str

Transition { guard: Predicate | null, target: StateId, label: str }
Relation   { name: str, rows: list[dict[str, str]], description: str }
LoopSpec   { relation: str, body: SubflowId, var: str, max_iterations: int = 1000 }
```

---

## 8. Tiered runtime

The runtime is three tiers, because with control flow the cost of handing the rest
of a workflow to a free-form agent after a halt is unbounded and unsafe.

- **Tier 1, deterministic fast path.** Execute a state's action via its highest
  viable implementation (section 9), check the effect deterministically. This is
  the linear replayer for the degenerate case: no model, no cost. Most states
  resolve here.
- **Tier 2, bounded recovery of one transition.** When a single transition fails
  to resolve or verify, a model may propose a patch for that one transition only.
  It operates on the current state and current frame, not the remaining program;
  it proposes a heal patch (a reviewable diff, the same format as `HealEvent`)
  rather than free-running; the patch still passes the deterministic effect and
  identity gate before any consequential action; and it is bounded to one
  transition with a call budget, every call counted in `RunReport.model_calls`.
- **Tier 3, durable checkpoint, pause, resume.** When Tier 2 cannot safely
  recover, or a state is an approval or an irreversible state with an unmet
  precondition, the run durably checkpoints at the last verified state and pauses.
  It does not proceed and does not delegate. A human approves or edits, and the
  run resumes from the checkpoint rather than from the top. A verified action
  state is a natural checkpoint. On resume, an action whose declared effects were
  all already confirmed before the pause is not re-executed, so a confirmed
  consequential write is never performed twice.

The non-goal is explicit: do not hand the whole remaining workflow to a free-form
agent after a halt. That is the "agent silently writes wrong state" failure the
project is instrumented against. Recovery is local and gated; escalation is
durable and human-checkpointed. The blast radius of any model call is one
transition, and the blast radius of any halt is zero, because it is checkpointed
and resumable.

The structured halt record (`HaltObservation` on `RunReport.halt`) captures where
the run stopped, the unexpected on-screen text it had no branch for
(PHI-scrubbed), and the intents that completed before the halt. It has the same
shape as a learning trace, so the halt-to-learn loop can lift it into the trace
corpus with no reshaping.

---

## 9. Capability-adaptive compilation

The identity ladder already uses "the same semantic check, different fidelity per
substrate": a structured-text tier on DOM, UIA, and AX, and pixel or OCR tiers on
pure-pixel substrates. Capability-adaptive compilation generalizes that from
identity to the whole action.

A semantic action carries a contract (what the step means and how to know it
worked) and can run through backend-specific implementations, ordered by fidelity,
first viable wins:

1. `api`: call the app's API or DB write via `Step.api_binding`; the effect is
   probed against the system of record. This closes the transactional gap.
2. `dom_uia`: DOM (Playwright), Windows UIA, or macOS AX structural selection via
   the anchor's `StructuralLocator`, with a structured identity check and a
   structured effect check.
3. `vision_rdp`: the resolution ladder plus the OCR and pixel identity ladder plus
   vision postconditions. The pure-pixel and Citrix or RDP fallback.

The contract is the what (save this encounter for this patient; a record must
exist afterward). The implementation is the how (API call versus UIA invoke versus
vision click and OCR). The same program compiles to an API-backed implementation
where the app exposes one, a UIA implementation on a native client, and the
current vision implementation on a Citrix session, each satisfying the same
`record_written` effect at very different reliability and cost. The anchor plus
resolution ladder is now one implementation of the `vision_rdp` leaf, not the
universal mechanism.

---

## 10. The induction pipeline

The single-trace compiler is the bootstrap of a pipeline that turns one or more
demonstrations into a validated program or an honest refusal. The stages, and
their status:

```text
  demo_1                        additional traces / variants (optional)
    |                                        |
    v                                        v
 [1] bootstrap -> [2] candidates -> [3] disambiguation -> [4] induction -> [5] validate -> emit
     one interp.     generalizations   ask the operator     shared graph      or QUARANTINE
```

1. **Bootstrap (built).** Compile one demo into the degenerate straight-line
   program. This is today's compiler output, lifted. Zero behavior change.
2. **Candidate interpretations (built for the structural parts).** Enumerate
   generalizations consistent with the seed: which literals are parameters, which
   repeated sub-sequences are loop bodies over a relation, which post-action
   popups are branches versus drift, which orderings are essential. Enumeration is
   structural and needs no model call.
3. **Interactive disambiguation (built, `compiler/disambiguation.py`).** Where
   candidates disagree, do not guess. Ask the operator a concrete multiple-choice
   question grounded in a real screenshot or step. Each question maps to a schema
   decision: an `on_exception` or guard policy, a loop over a worklist, a guarded
   branch versus an exception, or a `ParamSpec` versus a baked literal. The answers
   are stored with the bundle as an audit trail.
4. **Multi-trace induction (built, `compiler/induction.py`).** When additional
   traces or variants are provided, infer the shared control-flow graph across
   traces. Divergences localize the branches and parameters: a second trace that
   picks a different row proves the row is a parameter, and a trace that hits the
   survey modal proves that edge exists. Alignment infers params (values that vary
   across traces), loops (a repeated body whose count differs), branches (a
   divergent step under a detectable condition), and optional steps, all
   deterministic with zero model calls.
5. **Validate or quarantine (built).** Validate the induced program on held-out
   traces and synthetic perturbations. Emit a program bundle when it is determined
   and validates. Quarantine (emit nothing, `certified=False`) when intent stays
   underdetermined, and route it to the disambiguation flow. This is the
   program-level analog of the identity ladder's refuse-rather-than-guess stance.
   A wrong branch on an irreversible node is exactly the failure class the whole
   repo is organized to avoid.

### Compile-time model use, once, not per run

The runtime is model-free by design and by benchmark. The induction pipeline
preserves that. Any model use is at compile time, once, to label steps with
human-readable intent, propose risk classes, and propose parameter, loop, and
branch candidates for the operator to confirm. An optional `Proposer` only
proposes; its output is flagged and never trusted. The model shapes the program
once; replay stays deterministic. This must not regress the runtime's zero-cost,
zero-call property.

---

## 11. What is built versus what needs a customer

### Built

- Typed params, predicates, guards, and `wait_until` (Phase 1).
- The full state machine: branch, loop, subflow, exception paths, and the
  degenerate lift (Phase 2).
- `entity_ref` params re-resolving through the identity ladder inside a loop.
- Effects with `record_written` and `field_equals`, `ValueExpr` param binding,
  the duplicate and collateral-loss guards, the on-screen read-back oracle, and
  the placeholder-halts-until-confirmed rule.
- `api_binding` and the API actuator tier, with the GUI as the safe fallback.
- The disambiguation, multi-trace induction, held-out validation, and quarantine
  stages.
- The tiered runtime: bounded local recovery and durable checkpoint, pause, and
  resume, with idempotent resume of confirmed writes.
- Bundle schema v2: integrity manifest, provenance, certification stamp, and
  optional encryption at rest for both `workflow.json` and the image crops.

### Needs a real customer workflow to finish well

- **Effect probes for a real system of record.** What "the encounter was actually
  written" means (which API, which DB row, which idempotency key) is
  app-specific. The schema slot exists; the probe contents are not generic.
  MockMed can only fake a persistence boundary.
- **The actual branch and loop logic of real workflows.** Which popups are normal
  versus error, which selections are loops over a real worklist, and what the
  exception and compensation paths should do are the operator's intent, and
  inducing them (and asking the right disambiguation questions) needs real
  demonstrations of a real process.
- **Compensations for irreversible states.** What it means to undo a save is
  domain-specific and often impossible. This is where the `irreversible` plus
  approval gate matters most and where a wrong design does real harm.
- **`api`-tier implementations.** Whether an app exposes a usable API, and what
  its contract is, is per customer.

### Principal risks

- **Induction proposing a wrong branch on an irreversible node.** Mitigated by
  quarantine-on-underdetermined, the approval gate on irreversible states, and
  Tier-3 durable escalation.
- **Scope creep re-introducing per-run model cost.** Mitigated by the
  compile-time-only rule and the bounded, counted, gated Tier-2. The zero-cost,
  zero-call runtime is a hard invariant.
- **Over-engineering ahead of a real workflow.** Mitigated by the phasing: the
  customer-dependent probe contents and compensations are deferred until there is
  a real process to design against.

---

## 12. Compatibility and versioning

The bundle schema is v2 and every v2 field is additive over v1. A v1 bundle
migrates on read (`bundle_validation.migrate_bundle_dict`) and replays
byte-for-byte. A bundle that declares no `param_specs`, no `program`, no effects,
and no api bindings is a v0 linear bundle and runs through the linear path
unchanged.

The migration was reversible at every step because each phase was a superset of
the last and the degenerate lift is mechanical, so no bundle authored under an
earlier phase is stranded. The load path validates structure (missing entry,
dangling transition or handler target, kind and payload mismatch, missing subflow,
duplicate id, unreachable terminal, unsafe unconditional cycle) and, for a bundle
carrying a sealed digest, integrity.

### Recommended further formalization

The IR is precise enough to specify and to test. Concrete, low-risk next steps:

- **Publish the JSON Schema.** `Workflow.model_json_schema()` already produces it
  from the Pydantic models. Writing it to a versioned file under `docs/design/`
  or `schema/` on each release gives integrators a machine-readable contract and a
  diffable record of schema changes.
- **Conformance tests for the semantics in section 6.** The rules for transition
  selection, fail-safe halting, loop bounds, and effect verdicts are worth a small
  table-driven test suite so the interpreter and this document cannot drift.
- **A short state diagram per real workflow** once a customer program exists, to
  document the actual branch and loop structure rather than the MockMed sketch.

These are additive to the document and the tests. They do not change the IR.

---

## Appendix A. Code map

| Concern | Model | Runtime |
|---------|-------|---------|
| Artifact | `Workflow` (linear `steps` and optional `program`) | linear replayer or graph interpreter |
| Action leaf | `Step` plus `Anchor` | resolution ladder, identity gate |
| Vision assertions | `Postcondition` | vision postcondition check |
| Record assertions | `Effect` (`record_written`, `field_equals`) | `EffectVerifier` (rest, fhir, sql, file, onscreen) |
| API tier | `ApiBinding` | `ApiActuator` |
| Params | `ParamSpec` (typed; `entity_ref`) | per-run overlay; identity re-resolution |
| Predicates and guards | `Predicate`, `Guard` | model-free predicate evaluation |
| State machine | `ProgramGraph`, `State`, `Transition`, `Relation`, `LoopSpec` | graph interpreter, transition selection |
| Degenerate lift | `lift_to_program` | byte-identical linear replay |
| Recover | `HealEvent` | Tier-2 bounded patch |
| Halt and resume | `HaltObservation`, durable checkpoint | Tier-3 checkpoint, pause, resume |
| Integrity | `BundleManifest`, `BundleProvenance` | load-time validate and verify |

## Appendix B. Lineage references

- Ringer et al.: reliable straight-line web record-and-replay (the input, not the
  output, of a generalizer).
- Rousillon / Helena (Chasins et al.): generalizing straight-line replay into
  relational loops.
- WebRobot (PLDI 2022): synthesizing loopy programs from action demonstrations.
- PROLEX: recovering the intended program from a single demonstration (81% of
  solved cases).
- Skill-DisCo: parameterized FSM subgraphs with typed params, pre and
  postconditions, and side effects from multiple traces.
- Agent Workflow Memory / Agent Skill Induction: inducing reusable branching
  skills from experience.
- PBD disambiguation-dialog tradition (SMARTedit, version-space PBD, "Watch What I
  Do", "Your Wish is My Command"): resolving demonstration ambiguity by asking
  concrete questions.
</content>
