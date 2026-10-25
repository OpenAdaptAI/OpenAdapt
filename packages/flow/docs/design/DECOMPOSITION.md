# Decomposing the largest files

Status: design, reviewed and approved on 2026-10-24. Section 9 records the
review decisions. No code moves in the pull request that adds this document.

This document supersedes [REPLAYER_DECOMPOSITION.md](REPLAYER_DECOMPOSITION.md)
(August 2026). That plan was never executed. Since then `replayer.py` grew from
12,296 to 18,258 lines, and the mechanism it proposed for moving methods fails
mypy (section 4.1 shows the error and the replacement).

All numbers were measured with `ast` and `grep` on `origin/main` at
`1efcc6ddc` (2026-10-24). The scripts are described in section 8 so anyone can
repeat the measurements.

## Contents

1. [What replayer.py is today](#1-what-replayerpy-is-today)
2. [Target architecture](#2-target-architecture)
3. [Invariants the refactor keeps](#3-invariants-the-refactor-keeps)
4. [Plan for replayer.py](#4-plan-for-replayerpy)
5. [Plans for the other large files](#5-plans-for-the-other-large-files)
6. [Size ratchet](#6-size-ratchet)
7. [Coordination with work in flight](#7-coordination-with-work-in-flight)
8. [How the numbers were measured](#8-how-the-numbers-were-measured)
9. [Review decisions](#9-review-decisions)
10. [Duplicates to collapse](#10-duplicates-to-collapse)

## 1. What replayer.py is today

### 1.1 Shape

| Region | Lines | Contents |
|---|---:|---|
| Module preamble | 1–736 | Docstring, 51 imports, tuned constants, 15 module functions (281 lines), `_GraphStepContext`, `_ProgramHalt`, `EgressNotPermitted`, `_WebFormApprovalView` |
| `class Replayer` | 737–18,258 | 260 methods, 17,522 lines |

The file is one class. 36 methods are over 100 lines, and 14 are over 200:

| Method | Lines | Parameters |
|---|---:|---:|
| `_run_step_on_active_surface` | 1,316 | 10 |
| `_act_without_dialog` | 955 | 11 |
| `_run_unsealed_steps` | 930 | 19 |
| `_try_api_tier` | 447 | 8 |
| `__init__` | 423 | 45 |
| `revalidate_attended_completion` | 364 | 10 |
| `_revalidate_consequential_actuation` | 356 | 11 |
| `_handle_interstitials` | 323 | 8 |
| `_verify_identity_ocr` | 280 | 6 |
| `_interpret_program` | 237 | 10 |
| `_retain_program_transition_evidence` | 223 | 9 |
| `_verify_effects` | 222 | 6 |
| `_act_scroll` | 218 | 10 |
| `_verify_typed_input` | 209 | 15 |

### 1.2 Concerns

Each method was assigned to one concern by name (the rules are in
section 8). "Calls out" counts calls from the concern's methods into other
concerns. "Called in" counts calls from other concerns into it. "Attributes
read" counts the distinct `self.` attributes the concern reads.

| Concern | Lines | Methods | Calls out | Called in | Attributes read |
|---|---:|---:|---:|---:|---:|
| Identity checks (OCR band, signal quorum, pixel recheck, hover, look-again, look-alike params) | 2,037 | 29 | 6 | 11 | 17 |
| Durable pauses, resume, idempotency, attended revalidation | 1,906 | 28 | 48 | 17 | 31 |
| Program interpreter (graph walk, loops, transitions, business decisions, checkpoints) | 1,673 | 24 | 25 | 13 | 34 |
| Step driver (`_run_step*`, switch surface) | 1,523 | 4 | 64 | 3 | 17 |
| Acting, by action kind (click, drag, type, select, key, scroll, read, dialogs) | 1,501 | 11 | 33 | 1 | 13 |
| Run driver and report (`run`, `_run_unsealed_steps`, finalize, halt record) | 1,443 | 12 | 40 | 6 | 30 |
| API tier and web-form spans | 1,156 | 15 | 21 | 7 | 10 |
| Confirming the action (typed input, selected option, postconditions, step guards) | 1,133 | 27 | 14 | 25 | 6 |
| Consequential revalidation and delivery (atomic/guarded input, fresh actuation, delivery authorization) | 1,013 | 25 | 12 | 38 | 13 |
| Effect checks and read-back, including pipelined read-back | 836 | 23 | 6 | 43 | 10 |
| Approval and authorization refusals, execution snapshots | 587 | 20 | 1 | 50 | 31 |
| Qualification fault injection | 515 | 11 | 0 | 15 | 14 |
| Settling and interstitials | 500 | 5 | 7 | 5 | 15 |
| Construction (`__init__`) | 423 | 1 | 1 | 0 | — |
| Surfaces and control overlay | 302 | 10 | 2 | 19 | 11 |
| Render scale and layout drift | 223 | 7 | 4 | 3 | 11 |
| Target resolution (`_resolve_step`, drag end) | 154 | 3 | 1 | 8 | 4 |
| Healing, assets, step evidence | 101 | 4 | 1 | 21 | 6 |

Two groups stand out:

- **Leaves** call almost nothing else in the class: qualification faults,
  approval refusals, target resolution, healing and assets, surfaces, render
  scale, effects, identity. They can move first.
- **Drivers** call many concerns and are called by few: the step driver, the
  run driver, durable resume, acting and the program interpreter. Moving them
  is cheap, but splitting them needs design (section 2).

The resolution rungs themselves are not in this file. They live in
`runtime/resolver.py` as one `resolve()` ladder (1,000 lines). The replayer
calls it from `_resolve_step` and retries it with fresh frames.

### 1.3 The step pipeline that already exists

`_run_step_on_active_surface` is a pipeline written as one function. In order:

| Phase | Where (line) | What happens |
|---|---:|---|
| Admit | 7825–7860 | Per-item approval before the save step resolves; qualification action requirements |
| Choose mechanism | 7862–7988 | API binding and effects set up, read gate, `_try_api_tier`; on "not sent", fall back to the GUI span |
| Observe | 7990–8100 | Settle, control overlay, step guard and `wait_until`, fault mutation, render scale, layout drift |
| Locate | 8104–8250 | `_resolve_step_target`, retried with fresh settled frames until `timeout_s`; identity band still changing |
| Record pre-state | 8252–8400 | Resolve effects, fault mutation, effect-tier refusal, snapshot of the system of record |
| Authorize delivery | 8400–8530 | `_revalidate_consequential_actuation`, fresh actuation authorization |
| Deliver | 8538–8790 | `_act`, fresh actuation event, reacquisition and repeat revalidation |
| Settle and confirm | 8793–8885 | Settle, selected option, postconditions, undo pre-state |
| Record check | 8888–8980 | Deferred read-back, or `_verify_effects` against the system of record |
| Repair | 8984–9000 | `_heal_step` for a non-template rung |
| Classify | 9002–9071 | Ambiguity and stale-fingerprint halts as typed safety halts |

The phases are real. They are hard to see because the function keeps its
working values in about 40 locals and in instance attributes, and returns
from dozens of points.

### 1.4 State

- `__init__` assigns 109 instance attributes from 45 keyword arguments.
- 88 attributes are written outside `__init__`. They fall into four kinds:
  - **Run state**, reset in `_run_unsealed_steps`: run id, execution
    snapshots, governed asset hashes, worklists, qualification bindings,
    durable snapshots.
  - **Program state**, written by `_interpret_program` and
    `_record_program_checkpoint`: about 20 `_program_*`, `_frame_stack` and
    `_completed_effect_*` attributes.
  - **State carried from one step to the next**: `_last_click_point`,
    `_last_click_region`, `_scroll_missed_target`, `_active_delivery_region`,
    `_active_delivery_resolution`, `_pc_scaled_match`, `_pc_page_scale`,
    `_undo_prior_run_dir`, `_settle_seed`, `_actuation_epoch`. Some are read
    by the next step on purpose (the previous click). Nothing in the code
    says which.
  - **Hidden state**: `_identity_look_bindings` and `_hover_frames` are
    created through `self.__dict__.setdefault` (lines 10513, 15648, 15961,
    16199, 16352), so they are not declared anywhere.
- `self.backend` is replaced in three places (surface activation and render
  scaling) and read in 80 methods.

### 1.5 Backend capabilities

The replayer checks backend capabilities with 56 `isinstance(self.backend, …)`
calls across 13 protocols. The most common are `RemoteActuationBackend` (22),
`GuardedCoordinateActionBackend` (10) and `GuardedKeyboardActionBackend` (8).
`_act_without_dialog` is a chain of `if step.action is ActionKind.X:` blocks
(12 kinds), each with its own capability checks.

### 1.6 Refusals

27 methods named `*_refusal` or `*_error` decide whether a step may proceed:
profile, approval, read gate, lookup capability, delivery authorization,
managed dispatch, production qualification, qualification campaign, workflow
snapshot, governed authorization snapshot, fresh actuation, active program
frame, identity gate, effect tier, layout drift, and others. They are called
inline at different points of the step and run drivers. Their order is a
safety property, and today it exists only as the order of statements.

### 1.7 API and GUI paths repeat each other

The API tier and the GUI path each set up effects with the same two fault
mutations (lines 7902–7917 and 8260–8275, identical except
`actuation_path="api"` or `"gui"`). Each has its own identity refusal
(`_api_identity_refusal`, 134 lines, and `_identity_gate_error`, 190 lines)
and its own handling of a delivery whose outcome is unknown
(`_verify_uncertain_api_delivery`, 126 lines, and the durable pending-delivery
path). The record contract is the same in both: which record, which fields,
which effect. Only the mechanism differs.

### 1.8 What depends on the file's layout

- **Importers.** 157 files import from the module (155 in flow, 2 in agent).
  Almost all import `Replayer`. Others import `identity_read_contradicts`,
  `secret_env_var`, `better_identity_read`, `_DURABLE_RESUME_AUTHORITY`,
  `_ProgramHalt`, `EgressNotPermitted`, `_scroll_target_positioned`,
  `_uniform_page_scale`, `effect_endpoint_scope`, `effect_field_checks` and
  `_effect_read_wording`.
- **Private methods called from tests.** Tests call 39 private methods on a
  `Replayer` instance directly, most often `_verify_identity` (27 call
  sites), `_web_form_span_at` (10), `_compare_qualified_signal_text`,
  `_api_identity_refusal`, `_begin_surfaces` and `_lookup_capability_refusal`
  (6 each). Tests also assign fakes to instance attributes such as
  `replayer._verify_identity`.
- **Module globals patched by tests.**
  `openadapt_flow.runtime.replayer.resolve`,
  `openadapt_flow.runtime.replayer.identity_mod.verify_pixel_identity`,
  `_DIALOG_WAIT_S`, `_captured_context_observations` and
  `_act_answering_dialog`. A method that moves to another module reads its
  globals from that module, so these patches would silently stop applying.
- **CI pins the filename.** The strict mypy step in
  `.github/workflows/packages.yml` lists `openadapt_flow/runtime/replayer.py`
  by name, and `.github/CODEOWNERS` lists it as a safety path. Code moved to a
  new module leaves strict typing and code-owner review unless both lists
  change in the same pull request.
- **Finding: the safety coverage floor is not enforced.** The 85% floor on the
  safety path is defined in `packages/flow/.github/workflows/ci.yml`. Workflows
  under `packages/flow/.github/` do not run in the monorepo; only the root
  `.github/workflows` do. Nothing enforces the floor today.

## 2. Target architecture

### 2.1 Principles

1. **The record contract is the stable core.** A step says which record it
   acts on (identity), what must change (effects) and what must hold before
   and after (guards, postconditions). How the step is carried out (GUI, API,
   web form) and how the outcome is checked (screen, system of record) are
   replaceable mechanisms behind that contract.
2. **Safety order is explicit and fixed.** Phases and gates run in an order
   written down in one place and pinned by a test. Nothing that decides
   whether input is delivered is added by registration or discovery, because
   a registration mistake would fail open.
3. **Every phase fails closed.** A phase returns either "continue" or a
   finished, failed `StepResult`. An exception inside a phase becomes a halt,
   as today (`_run_step_on_active_surface` never raises).
4. **State is passed, not shared.** Per-step values travel in a step state
   object; per-run values in a run state object. Instance attributes hold
   configuration.
5. **A seam needs a reason.** Each seam below names the duplication it removes
   or the near-term need it serves. Where neither exists, the code stays as it
   is.

### 2.2 Shape

```text
Replayer                      public API unchanged: constructor, run(), revalidate_*,
  |                           recheck_deferred_readback, select_attended_program_transition
  |
  +-- RunDriver               linear steps | program interpreter
  |     run gates             durable claim, idempotency, execution snapshots,
  |                           qualification boundary, profile
  |
  +-- StepPipeline            fixed phase order, StepState passed through
  |     admit -> choose mechanism -> observe -> locate -> identity -> record pre-state
  |       -> authorize delivery -> deliver -> settle and confirm -> record check
  |       -> repair -> classify
  |
  |     Mechanism (per step)  GuiMechanism | ApiMechanism | WebFormMechanism
  |       GuiMechanism        resolver.resolve() ladder, ActionExecutor per ActionKind,
  |                           BackendCapabilities
  |     Checks                IdentityCheck, OutcomeCheck; source = screen | record
  |     DeliveryGates         ordered tuple, evaluated at the last point before input
  |
  +-- ReportBuilder           StepResult accounting, finalize, halt record
```

### 2.3 Seams

**S1. `StepState` and `RunState`.** Replace the per-step attributes listed in
1.4 and the 10 to 19 parameters threaded through the step functions with two
dataclasses. The hidden `__dict__` state becomes declared fields. Reason: the
next-step reads are implicit today, and every other seam needs a place to put
its values. This seam changes no behavior and has no new abstraction.

**S2. Step pipeline.** Turn the phases of 1.3 into functions that take
`StepState` and return `Continue` or a failed `StepResult`, run by a pipeline
with the order fixed in code. Reason: the 1,316-line function, and the API and
GUI paths that need to share phases (S3). Not done: making phases pluggable.

**S3. Mechanism behind one contract.** A `Mechanism` protocol with
`locate_and_check_identity`, `record_pre_state`, `deliver` and
`classify_unknown_delivery`. GUI, API and web-form spans implement it. Effect
setup, delivery authorization and the record check move out of the mechanisms
into shared phases. Reasons: the duplication in 1.7, and contract-first
execution, which runs the same workflow contract through whichever mechanism
the deployment allows.

**S4. Action executors and backend capabilities.** One `ActionExecutor` per
`ActionKind`, held in a dictionary that a test proves covers every kind (a
missing kind is a refusal, never a no-op). Backend capabilities are computed
once when a surface is activated into a frozen `BackendCapabilities` value
that replaces the 56 `isinstance` checks. Reasons: the 955-line if-chain, and
RDP and Citrix backends, which need to state in one place what they can do
safely.

**S5. Checks with a source.** `IdentityCheck` and `OutcomeCheck` interfaces,
each tagged with where its evidence comes from: the screen or the system of
record. Today identity is read only from the screen (2,037 lines of OCR,
pixel and hover logic), and outcomes come from the screen (postconditions) or
the system of record (`EffectVerifier`). Reason: checking identity through the
application's API or database, which is the contract-first direction. This
refactor adds the interface and the screen implementation only. A record
implementation for identity is a separate feature pull request with its own
safety review.

**S6. Delivery gates as an ordered tuple.** The refusals that decide whether
input is delivered (1.6) become `DeliveryGate` objects in a tuple that the run
profile builds. Fail-closed rules: an exception in a gate is a refusal, and a
governed profile with an empty tuple refuses to run. A test pins the tuple for
each profile. Reason: the order is a safety property and should be written
down once. Not done: gates registered by plugins.

**S7. Program interpreter and durable resume.** A `ProgramState` object
replaces the `_program_*` attributes, and the interpreter, checkpoints and
resume move into their own package. Reason: about 3,600 lines with their own
state, and the production lifecycle work that changes them often.

**S8. `ReportBuilder`.** `_record_linear_result`, `_account_result`,
`_finalize_report`, `_emit_halt` and the `_stamp_*` methods move behind one
object. Reason: the report schema is a public contract, so its construction
should be in one place.

### 2.4 The `runtime.core` boundary

`openadapt_flow/runtime/core/` holds the code that carries out a step through
a backend and nothing that decides whether it may. Governance reaches it only
as gates the caller passes in: `ActionContext.authorize` runs delivery
authorization and the qualification-environment check at the last point
before input, in the order the replayer always ran them.

Two import-linter contracts in `packages/flow/.importlinter`, run by
`lint-imports` in the flow lint job, keep it that way. Neither may import
`runner`, `hosted`, `approval_mode`, `jit`, the replayer or any
`qualification*` module:

- **Direct.** No module in `runtime.core` imports a governance module. This
  holds with no exceptions.
- **Indirect.** The same through every chain of imports, except 45 listed
  edges. Measured on `origin/main` at `ecc162e12`: a module that imports only
  `ir`, `backend` and the resolver reaches 249 of the package's 562 modules,
  44 of them governance. The routes run through the schema (`ir` imports
  `qualification`, `qualification_faults` and `approval_mode.model`), the
  effect verifier (`effects.effect` → `authorization` → `execution_profiles`
  → `durable` → the replayer → `runner`), and the policy, repair, fleet and
  durable modules. A new governance import in any module the core depends on
  fails the contract. Each cut removes a line; PR 5 removes the `ir` edges.

`tests/test_import_boundaries.py` keeps the forbidden list complete as
`qualification_*` modules are added, and proves every `ActionKind` is carried
out in exactly one place: `runtime.core.ACTION_EXECUTORS` (KEY, HOTKEY and
WAIT so far) or the replayer's list of kinds not yet moved.

### 2.5 Deliberately not done

- **Resolution rungs as registered strategies.** `resolver.resolve()` is
  already separate, and the rung order is part of the safety argument
  (`resolver.is_below_ocr` marks the rungs that need extra identity evidence).
  No second ladder is planned. Revisit when one is.
- **A configuration object for the constructor.** The 45 keyword arguments
  are public API used by 157 importers. Grouping them is worth doing, but it
  is an API change and not part of a behavior-preserving refactor.
- **Dependency injection frameworks, plugin discovery, or base classes for
  their own sake.**

## 3. Invariants the refactor keeps

Every pull request in sections 4 and 5 must keep these. The right column says
how the pull request proves it.

| Invariant | Proof in each pull request |
|---|---|
| Fails closed: a missing, unreadable or contradicted check halts the step | Full flow suite, including the realism tests (`test_realism_durable_resume`, `test_multi_surface_fail_closed`, `test_jit_fail_closed`); golden runs (3.1) include halting scenarios |
| No retry after an unconfirmed delivery: a save that may have been sent is never sent again without a person's explicit authorization | Resume tests, golden pause-and-resume scenario, backend call trace identical |
| Zero model calls on a healthy run | Golden runs assert `model_calls == 0` on every passing scenario |
| Report schema stable | Golden reports equal after normalizing time and run id; `RunReport.model_json_schema()` unchanged |
| Signed and hashed artifacts unchanged | Golden runs compare SHA-256 of every artifact file; bundle, contract and qualification digests unchanged |
| Approval gates need a signed human answer | Approval tests; no change to approval code paths outside the moves (and none before #1486 merges) |
| Same side effects in the same order | Golden runs record every backend call (method and arguments) and compare the trace |
| Import paths keep working | `replayer.py` re-exports every moved name; a test imports every name importable from the module on `main` before the first move |
| Strict typing and code-owner review keep covering the moved code | Same pull request updates the strict mypy list and CODEOWNERS to cover `runtime/replay/` |

### 3.1 Golden runs

A golden run executes a bundle against a real backend, then saves the
normalized report, the hash of every artifact and the backend call trace. The
baselines are produced on `main` before the first move and committed. Every
later pull request must reproduce them exactly. The scenarios use the real
Playwright backend against the local fixture apps the end-to-end tests already
use, not fakes, because fakes hid five fail-open bugs in the October realism
audit:

1. Linear run that passes, with typed input, a selected option and a scroll.
2. Identity mismatch that halts before input.
3. Effect refuted by the system of record.
4. API step that saves, and API step whose delivery is unknown and pauses.
5. Durable pause, then resume, with no second send.
6. Approval gate that pauses, and an answer that continues.
7. Program with a loop over a worklist.
8. Interstitial dialog dismissed.
9. Fault-injected qualification case that must halt.

The call trace comes from a recording wrapper around the backend, so it
captures what the replayer asked the backend to do, without depending on
timing.

## 4. Plan for replayer.py

### 4.1 Two stages

**Stage A moves code without changing it.** Each concern moves to a mixin
class in a new package, `openadapt_flow/runtime/replay/`, and `Replayer`
inherits from the mixins. Method bodies are byte-identical. Tests that call
private methods, or that assign fakes to instance attributes, keep working,
because the methods are still looked up on the instance.

The August plan proposed annotating each moved method with
`self: "Replayer"`. Mypy rejects that:

```text
mix.py:7: error: The erased type of self "host.Replayer" is not a supertype
of its class "mix.IdentityMixin"  [misc]
```

Instead, each mixin declares what it needs from the host as a `Protocol` that
only mypy sees:

```python
class _IdentityHost(Protocol):
    backend: Backend
    vision: Vision
    identity_vlm: IdentityVlm | None
    pixel_verify_enabled: bool
    def _asset_bytes(self, ...) -> bytes: ...

if TYPE_CHECKING:
    _Base = _IdentityHost
else:
    _Base = object

class IdentityChecks(_Base):
    def _verify_identity(self, ...):   # body unchanged
        ...
```

This was checked with mypy 1.11.2 using the strict flags from CI. The mixin
type-checks; at run time the class is a plain `type` whose method resolution
order is `Replayer`, the mixins, `object` (no `Protocol` metaclass); and when
`Replayer` assigns an attribute a type that no longer matches the declaration,
mypy reports an error. Drift is caught, not silent.

The host protocols are also the first deliverable of the design: each one
lists exactly what its concern reads, which is the input for Stage B.

Stage A also reduces merge conflicts. In the October merge-conflict review,
three API-step pull requests (#1370, #1371, #1372) each conflicted on
`replayer.py`. Concerns in separate files conflict only when two pull requests
change the same concern.

**Stage B changes structure without changing behavior.** It introduces the
seams of section 2.3. Each seam must reproduce the golden runs exactly,
including the call trace.

### 4.2 Rules for every pull request

Phase 2 is a few large pull requests, one per group of target modules (4.3),
rather than one pull request per concern.
A large pull request is still reviewable when its commits are small and each
commit is one of two kinds:

- **Move commit.** Relocation only: no edit besides imports, the mixin base
  and re-exports. `scripts/verify_moved_code.py` (added in PR 1) proves it:
  every function and method in the base file has an identical AST somewhere
  in the head package, and the commit adds no new function bodies. A reviewer
  checks the script output, not the moved lines.
- **Seam commit.** One seam from section 2.3, behavior-preserving. A reviewer
  reads it in full.

Every pull request also:

- keeps every commit green on its own (full flow suite, ruff, the strict mypy
  step and the golden runs), so the pull request can be bisected and, if
  needed, merged only up to a given commit;
- re-exports every moved module-level name from the old module;
- retargets test patches of moved module globals in the same commit (the
  script lists them);
- adds the new modules to strict mypy, CODEOWNERS and the safety-path
  coverage include list;
- adds characterization tests before moving any concern whose coverage is
  thin, in a commit before the move;
- lowers the size-ratchet budgets (section 6) to the new sizes;
- rechecks `gh pr list` before starting and before opening, leaves out any
  concern that an open pull request is changing (section 7), and rebases on
  fresh `main` without force-pushing a branch someone else has reviewed.

### 4.3 Phase 2: five pull requests

| PR | Target module group | Contents | Lines out of the big files | Wait for |
|---|---|---|---:|---|
| 1 | Safety net | Size ratchet (section 6); golden-run harness, scenarios of 3.1, baselines from `main`; characterization tests chosen from the coverage run; `scripts/verify_moved_code.py`; import-surface tests for `replayer`, `__main__`, `qualification` and `ir`; safety-path coverage reported as a measured number | 0 | — |
| 2 | Replayer step pipeline and strategies (`runtime/replay/`) | Move commits for the module helpers, qualification faults, healing and assets, render scale and layout drift, surfaces, effects and read-back, settling and interstitials, confirming the action, identity checks, consequential delivery, API tier and web forms, acting by kind. Seam commits S1 `StepState`, S4 `BackendCapabilities` and action executors, S2 phase pipeline, S3 `Mechanism`, S5 check interfaces | ~9,600 | #1504 (identity, delivery, acting); #1449 (interstitials) |
| 3 | Run gates (`runtime/replay/` gates, `runtime/program/`) | Move commits for approval and authorization refusals, durable resume and attended revalidation, the program interpreter, report finalization. Seam commits S6 `DeliveryGate` tuple per profile, S7 `ProgramState`, S8 `ReportBuilder` | ~4,600 | #1486, #1500, #1501 (approvals); #1435 (program interpreter); #1463 (lifecycle) |
| 4 | Command surfaces (`cli/`, Desktop handlers) | `__main__.py` split into `cli/<group>/<verb>.py` (5.1); `dispatch.py` split into handler groups (5.4) | ~16,000 | #1486, #1501, #1447 (`__main__.py`); #1486, #1500 (`dispatch.py`) |
| 5 | Schemas and qualification (`ir/`, `qualification/`) | `qualification.py` package with one integrity checker per case kind (5.2); `ir.py` split by domain (5.3) | ~11,000 | #1504, #1449, #1435, #1463 (`ir.py`); none for `qualification.py` |

PR 1 merges first, so the golden baselines come from `main` before any code
moves. PRs 2, 4 and 5 touch different files and can be reviewed in parallel
after it. PR 3 goes after PR 2, because the gates wrap the pipeline that PR 2
creates.

When a pull request in the "wait for" column is still open, the pull request
leaves out only the concerns that pull request changes, and a short follow-up
moves them after it merges. Nothing waits for an unrelated pull request.

After PRs 2 and 3, `replayer.py` keeps construction, `run`,
`_run_unsealed_steps` and the step driver's entry point: about 4,000 lines,
mostly the public API and the wiring of the pipeline.

### 4.4 Commit order inside PR 2

Least coupled first, so each move commit has the fewest cross-references:

1. Module helpers to `replay/_shared.py` (281 lines).
2. Qualification fault injection (515).
3. Healing, assets, step evidence; render scale and layout drift (324).
4. Surfaces and control overlay (302).
5. Effect checks and read-back, including pipelined (836).
6. Confirming the action: typed input, selected option, postconditions, step
   guards (1,133).
7. Settling and interstitials (500).
8. API tier and web-form spans (1,156).
9. Identity checks (2,037), consequential delivery (1,013), acting by kind
   (1,501).
10. Seams, in this order: S1 (every later seam needs a place for its values),
    S4, S2, S3, S5.

S3 and S5 are the seams that serve contract-first execution directly.

## 5. Plans for the other large files

### 5.1 `__main__.py` (11,970 lines)

- `build_parser` is one function of 3,546 lines that registers 113
  subcommands.
- 61 `_cmd_*` functions take 4,151 lines; the largest are `_cmd_run` (477),
  `_cmd_qualify` (447), `_cmd_resume` (354) and `_cmd_replay` (278).
- About 4,270 lines are shared helpers (backend flags, replay parameters,
  approval mode, deployment configuration, worklists, result pushing).
- 110 files import from it: `main` (88) and `build_parser` (41), plus about
  40 private helpers. Tests replace `cli._build_and_run_replayer`,
  `cli.Replayer`, `cli._cmd_record_capture`, `cli._attached_web_page` and
  others with `monkeypatch.setattr`.

Target: an `openadapt_flow/cli/` package that mirrors
`docs/commands/<group>/<verb>.md` (ten groups). Each command module has
`register(subparsers)` and `run(args)`. Shared helpers go to
`cli/_common/`. `__main__.py` keeps `main` and a `build_parser` that calls
each `register` in today's order, and re-exports every name that is imported
today.

Proof for each pull request: a snapshot of `--help` for every command and
subcommand stays byte-identical; the command-map test
(`scripts/build_command_map.py`) passes; patches on moved names are
retargeted in the same pull request.

Order: shared helpers first (they have no command), then leaf commands such
as `visualize`, `lint` and `known-ids`, then groups, and `run`, `replay`,
`resume`, `qualify` and `approve` last. Three open pull requests touch this
file (#1486, #1463, #1447).

### 5.2 `qualification.py` (5,775 lines)

- 26 classes and 63 module functions.
- `_case_run_report_integrity_error` is one function of 1,595 lines (lines
  2651–4245). It checks a qualification case's run report and branches on
  `QualificationCaseKind` (representative, ambiguity, weak effect and
  others).
- `evaluate_qualification` is 709 lines.

Target: a `qualification/` package with `models.py` (the data classes),
`project.py`, `evaluate.py` and `integrity/`, which has one checker per case
kind in a dictionary keyed by kind. A test proves every kind has a checker; a
kind without one is an integrity error, never a pass. The checks shared by all
kinds (report and input references, bindings, outcome envelope, environment,
surface) become one common checker that runs first.

Proof: qualification certificates and fault-contract digests unchanged
(`test_qualification_*` suites), the strict mypy list updated. No open pull
request touches this file today, so it can start in parallel with replayer
Stage A.

### 5.3 `ir.py` (5,765 lines)

94 Pydantic models and 14 functions. It is a schema module, and the size is
mostly declarations. The largest are `RunReport` (580), `Workflow` (514),
`Step` (340) and `StepResult` (182).

Target: an `ir/` package by domain (steps and actions, anchors and identity,
effects and API bindings, program graph, interstitials, reports and outcomes,
the workflow container), with `ir/__init__.py` re-exporting every public name
and calling `model_rebuild()` where forward references cross modules.

Proof: `model_json_schema()` of every model unchanged; bundle, contract and
report digests unchanged; serialized fixtures load and re-serialize
identically. Three open pull requests touch this file (#1463, #1449, #1435),
so it goes after them.

### 5.4 `dispatch.py` (5,686 lines, Desktop)

`EngineDispatcher` is one class with 198 methods. `_register` maps the
command names used by the frontend `engine.ts` to methods, grouped by about
20 comment headers: recording, library, compile and run, operator surface,
check setup, per-item approval, qualification, cloud sync, auth, settings,
policy, OS permissions, surfaces, egress review, hosted runner, mobile
decisions, shadow mode and others.

Target: one handler class per group, each owning its state, with the
dispatcher keeping the single command table so the `engine.ts` contract stays
one list. Proof: a test that the table's keys are unchanged, the Desktop
engine tests (`test_engine/test_dispatch.py`). Wait for #1486 and the in-app
approver key session, which are changing this file now.

### 5.5 Files covered only by the ratchet

`capture/recorder.py` (4,532), `interactive_recorder.py` (4,524),
`runtime/durable/attended.py` (4,271), `terminal_verification_v2.py` (3,663)
and `backends/playwright_backend.py` (3,636). They get plans after the files
above. The ratchet keeps them from growing meanwhile.

## 6. Size ratchet

`scripts/check_file_size_ratchet.py` (added in PR 1) reads the table between
the markers below and fails if any listed file has more lines than its
budget. The root test `tests/test_file_size_ratchet.py` runs it, so it runs in
the Python CI workflow on every pull request and in the merge queue.

PR 1 sets each budget to the file's size on `main` when PR 1 merges. After
that:

- A pull request that shrinks a file lowers its budget in the same pull
  request; the check prints the new number.
- A pull request that adds net lines to a listed file raises the budget in
  its own diff, with a one-line reason in the pull request description. The
  ratchet makes growth visible in review; it does not block feature work.

<!-- size-ratchet:begin -->
| File | Line budget |
|---|---:|
| `packages/flow/openadapt_flow/runtime/replayer.py` | 18258 |
| `packages/flow/openadapt_flow/__main__.py` | 11970 |
| `packages/flow/openadapt_flow/qualification.py` | 5775 |
| `packages/flow/openadapt_flow/ir.py` | 5765 |
| `packages/desktop/openadapt_desktop/dispatch.py` | 5686 |
| `packages/capture/openadapt_capture/recorder.py` | 4532 |
| `packages/flow/openadapt_flow/interactive_recorder.py` | 4524 |
| `packages/flow/openadapt_flow/runtime/durable/attended.py` | 4271 |
| `packages/flow/openadapt_flow/terminal_verification_v2.py` | 3663 |
| `packages/flow/openadapt_flow/backends/playwright_backend.py` | 3636 |
<!-- size-ratchet:end -->

## 7. Coordination with work in flight

Open pull requests touching the files in this plan, as of 2026-10-24:

| File | Open pull requests | Affects |
|---|---|---|
| `runtime/replayer.py` | #1504 (same-identity row halt), #1449, #1435, #1463 | PR 2 (identity, delivery, acting, interstitials), PR 3 (program interpreter, lifecycle) |
| `runtime/durable/` | #1435, #1463 | PR 3 |
| `__main__.py` | #1486, #1501, #1463, #1447 | PR 4 |
| `dispatch.py`, `flow_bridge` | #1486, #1500 | PR 4 |
| `ir.py` | #1504, #1463, #1449, #1435 | PR 5 |
| `qualification.py` | — | PR 5 can start now |

`runtime/identity.py` and `runtime/resolver.py` are not moved by this plan.

## 8. How the numbers were measured

- **Structure.** An `ast` walk of each file lists classes and functions with
  start and end lines, parameter counts, calls to `self.<method>` and to
  module functions, and `self.<attribute>` reads and writes.
- **Concerns.** Methods were assigned to concerns by name with ordered regular
  expressions (first match wins). For example, names containing `identity`,
  `signal`, `identifier`, `lookalike`, `hover` or `pixel_recheck` count as
  identity checks; `_run_step*` as the step driver. One method was left
  unassigned (`_next_anchored_step`, 6 lines).
- **Coupling.** "Calls out" and "called in" count `self.<method>()` call sites
  between concerns, from the same `ast` walk.
- **Dependencies on the layout.** `grep` over `packages/` for
  `from openadapt_flow.runtime.replayer import`, for patch target strings
  `openadapt_flow.runtime.replayer.<name>`, for `monkeypatch.setattr` on the
  module, and for calls of `_<name>(` on objects named like a replayer.
- **Mixin typing.** A two-file example (a host class and a mixin) checked
  with mypy 1.11.2 using the strict flags from `packages.yml`.

The scripts will be committed with PR 1 so the numbers can be refreshed.

## 9. Review decisions

Decided in review on 2026-10-24:

1. **Five pull requests by module group** (4.3): approved.
2. **Ratchet:** lands in PR 1, with budgets set to `main`'s sizes when PR 1
   merges. Open pull requests that add net lines to a listed file raise the
   budget in their own diff with a one-line reason (section 6).
3. **Coverage floor:** PR 1 reports safety-path coverage as a measured number
   and does not gate on it. After it is measured, the floor is set at the
   measured value and only rises, instead of a fixed 85%.
4. **Resolution rungs:** no second ladder is planned, so `resolver.resolve()`
   stays one ladder. Checking identity against the system of record is a check
   source (S5), not a ladder.
5. **Core boundary:** an import-linter contract keeps `runtime.core` free of
   governance imports (2.4), and the first executors move behind it at the
   start of PR 2. Governance becomes gates around the core.
6. **Landing order:** #1510 (PR 1) lands last among the in-flight pull
   requests, with budgets reset to `main` at merge time and the committed
   golden baselines; the ratchet then becomes blocking.

## 10. Duplicates to collapse

Measured on `origin/main` at `ecc162e12` by reading and `grep`. "Users" counts
non-test files that reference the symbol. Each family names the canonical
implementation and the pull request in section 4.3 that collapses it.

| Family | What exists | Canonical | PR |
|---|---|---|---|
| Check-strength scales | 8 ladders for one idea: `CheckLevel` (types, 0–5, higher is stronger, already maps the others), `VerificationTier` (1–4, lower is stronger, 46 users, on the wire), `EffectVerificationTier`, `EffectStrengthV1`, `OracleTier`, `ReadBackTier` (defined in types and hand-copied in `approval_mode/approver.py:237`), six copies of the `independent_readback \| screen_readback \| unchecked` literal, two identical `ReadGrade` literals | `CheckLevel`; `VerificationTier` stays as a wire alias | 5 |
| Run outcomes | `TransactionOutcome` (10 states), `ExecutionOutcome` and the envelope literal (5), `QualificationOutcome` (the same 5 in lower case), `ProductionExecutionOutcome`, `ExecuteTerminalOutcomeV1`, `PlainResultV1`, a parallel plain vocabulary in `desktop/outcomes.py`, free strings in the agent runner; `RunReport` carries `success`, `execution_outcome`, `outcome_envelope` and `transaction_outcome` at once | `TransactionOutcome` for the terminal state; `PlainResultV1` for people | 3 (report), 5 (schema) |
| Admission formats | Qualification admission v1 (enforced today), v2, v4; local-dev admission; JIT `admission.json`; execute admission; admit-pipeline pending and decision records; product-release admission | The v4 envelope, with dev, JIT and execute as issuer or mode variants | 5, after a cross-repository contract review |
| Certify paths | Three certify commands repeat the same policy-source resolution; Desktop `certify_bundle` evaluates qualification twice; JIT, repair readmission and the admit pipeline each sign their own records | `certify_project` and one `resolve_policy` helper | 4 |
| Signing and canonical JSON | About 45 canonical-JSON helpers. They are not byte-identical: some use `ensure_ascii=True` (`runner_capability.py:177`, `jit/lifecycle.py:192` by default), most `False`. `flow/openadapt_flow/agent/seal.py` copies `types/openadapt_types/seal.py`. Six key stores | `openadapt_types.seal.canonical_json` with an explicit `ensure_ascii`; signed formats keep a versioned helper so existing signatures still verify | 3 (agent seal copy first), 5 |
| Pagination | One pager (`runtime/list_source.py`), used only by `list_guard.py` | Nothing to collapse | — |
| Census | Not on `main`; on its feature branch a positional-id check exists in Python twice and once in JavaScript | One implementation with a generated JavaScript mirror | After that branch merges |
| Runtimes | One step interpreter (`Replayer`). Four callers start it as a subprocess, five through `deployment.build_replayer`, and about ten construct `Replayer(...)` directly with their own arguments, which can skip governance wiring | `build_replayer` for every in-process caller | 3 |

Cheapest first: the agent seal copy, the shared literals, the copied outcome
string sets, `resolve_policy`, routing direct `Replayer(...)` callers through
`build_replayer`. Strength scales, outcomes and admissions change persisted or
signed formats and come last, each with a versioned mapping.
