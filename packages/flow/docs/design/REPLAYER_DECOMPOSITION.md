# Replayer decomposition plan

Status: PLAN ONLY — no code moves in this document's PR.

`openadapt_flow/runtime/replayer.py` is 544 KB / 12,296 lines. This document
maps its real internal structure, explains why a large "mechanical, verbatim,
zero-behavior-change" extraction is **not** safely available today, and lays
out an ordered sequence of small, individually verifiable extraction steps —
starting with the one step that *is* purely verbatim.

All numbers below were measured with `ast` against
commit `b646276` (`chore: release 1.28.0`).

## 1. What the file actually is

Contrary to the usual "big module with many sections" shape, the file is
**one class plus a small preamble**:

| Region | Lines | Size | Contents |
|---|---|---|---|
| Module preamble | 1–331 | ~20 KB | Docstring, imports, tuned constants (`PC_TEMPLATE_*`, `FIELD_REGION_*`, `MASKED_*`, `SCROLL_BUDGET_FACTOR`, `SECRET_ENV_PREFIX`, `PROGRAM_MAX_STEPS/DEPTH`), `secret_env_var()`, `_GraphStepContext`, `_ProgramHalt`, `_all_workflow_steps()`, `EgressNotPermitted`, `_DURABLE_RESUME_AUTHORITY` |
| `class Replayer` | 332–12,296 | 524 KB | Everything else. ~230 methods on a single class. |

There are **no** module-level definitions after line 332. The class is the
file.

### Coupling inside the class (measured)

- `__init__` assigns **67 instance attributes**; methods reach them freely.
- The three largest methods are monolithic drivers, not sections:
  - `_run_step` (lines 5067–6120, **52 KB**): 12 self-attrs, calls **30**
    sibling methods.
  - `_act` (8536–9442, **43 KB**): calls 20 sibling methods.
  - `run` (938–1699, **37 KB**): 38 self-attrs, 23 sibling calls.
- 38 `@staticmethod`s total only **39 KB**, are scattered across the file,
  and several recurse via the class name (e.g.
  `Replayer._canonical_resume_value` calls itself through `Replayer.`), so
  even they are not verbatim-movable.

### Functional regions (for orientation; NOT extraction boundaries)

| Lines | Region |
|---|---|
| 332–937 | `__init__`, durable-resume admission |
| 938–2212 | `run()` linear driver; report finalization, control overlay, idempotency |
| 2213–4817 | Deterministic program (graph) interpreter; checkpoints; resume; attended revalidation |
| 4819–5066 | Qualification-fault drivers |
| 5067–6124 | `_run_step` (single method) |
| 6125–6830 | API actuation tier |
| 6832–7409 | Effect resolution/verification; evidence retention |
| 7411–7958 | Consequential-actuation gates and revalidation |
| 7960–8534 | Anchor/step resolution; backend delivery; fresh actuation; snapshots |
| 8536–9599 | `_act`; interstitial snapshots; settling |
| 9600–10128 | Qualification environment; interstitial handling |
| 10129–10310 | Step gates; predicates |
| 10314–11162 | Identity-signal verification (quorum, OCR, crops) |
| 11164–11648 | Typed-input and selection verification |
| 11649–11888 | Closed-loop scroll |
| 11889–12190 | Structural state; postconditions; asset access |
| 12191–12296 | Healing acceptance; evidence persistence |

## 2. Why a 50–120 KB verbatim extraction is not safe today

Three gates are pinned to this file **by exact filename** and must stay in
lock-step (the repo says so itself in `.github/workflows/ci.yml` and
`.github/CODEOWNERS`):

1. `mypy-strict-safety` — explicit file list includes
   `openadapt_flow/runtime/replayer.py`.
2. The 85% safety coverage floor — `--include` glob names
   `openadapt_flow/runtime/replayer.py` exactly.
3. `CODEOWNERS` — the safety path list mirrors both.

Any extraction therefore *must* also edit those three lists, or the moved
code silently drops out of strict typing and the coverage floor — a gate
weakening disguised as a refactor.

And because the file is one class, every candidate mechanism for moving
instance methods requires **non-verbatim** edits:

- **Mixin classes**: moved methods reference `self.vision`, `self.backend`,
  etc. `--check-untyped-defs` (on in the strict pass) resolves attribute
  access, so a bare mixin fails with `attr-defined`. Fixes require either
  `if TYPE_CHECKING:` attribute/method stubs (duplicated declarations that
  can drift silently, in the safety core) or explicit `self: "Replayer"`
  parameter annotations (a signature edit on every moved method).
- **Static-method hoisting**: only 39 KB exists, it is scattered rather than
  cohesive, and bodies self-reference via `Replayer.`, so hoisting requires
  body edits plus class-dict rebinding (`_name = staticmethod(fn)`).
- **Function extraction with parameter passing**: signature changes by
  definition.

Conclusion: the "cohesive leaf section" premise does not hold for this file.
The correct first PR is this plan; the code moves follow as small steps, each
with its own full gate run.

## 3. Ordered extraction steps

Every step below carries the same **gate-sync checklist**:

- [ ] Add the new module to the `mypy-strict-safety` file list in
      `.github/workflows/ci.yml`.
- [ ] Add it to the coverage `--include` glob in the same file (or switch the
      replayer entry to `openadapt_flow/runtime/replayer*.py` once and keep
      new modules on that prefix).
- [ ] Add it to the safety path in `.github/CODEOWNERS`.
- [ ] Run locally: the strict mypy command from ci.yml, the fast test suite,
      and `coverage report --fail-under=85` with the CI `--include` list.
- [ ] `git diff` on moved blocks shows pure relocation (verify with
      `git diff --color-moved=dimmed-zebra`).

### Step 1 — module preamble → `openadapt_flow/runtime/replayer_defs.py` (~12 KB, purely verbatim)

The only extraction with **zero** class coupling. Move lines 178–331
verbatim: the tuned constants, `secret_env_var()`, `_GraphStepContext`,
`_ProgramHalt`, `_all_workflow_steps()`, `EgressNotPermitted`,
`_DURABLE_RESUME_AUTHORITY`, `_MAX_FRESH_ACTUATION_REACQUISITIONS`,
`_DeliveryResultT`. `replayer.py` re-imports every name (`# noqa: F401`
style, as `runtime/__init__.py` already does) so all existing import paths —
including `runtime.durable.resume`'s deferred
`from openadapt_flow.runtime.replayer import _DURABLE_RESUME_AUTHORITY` —
keep working unchanged. Known importers to smoke-test: `runtime/__init__.py`,
`runtime/durable/resume.py`, `validation/identity_ladder.py`,
`validation/governed_run.py`, tests importing `secret_env_var` /
`EgressNotPermitted` / the `PC_*` constants.

Small, but it establishes the pattern: new module, gate-list sync,
re-export shim, moved-block diff proof.

### Step 2 — identity-signal verification mixin (~32 KB)

`_verify_identity`, `_verify_identity_ocr`, `_identifier_crops`,
`_ocr_identity_crop`, `_verify_dedicated_identity_signal`,
`_compare_direct_signal_text`, `_compare_qualified_signal_text`,
`_qualified_signal_evidence`, `_verify_signal_quorum`,
`_captured_context_observations` → `openadapt_flow/runtime/replayer_identity.py`.

Measured coupling: self-attrs {`backend`, `identity_vlm`,
`pixel_verify_enabled`, `vision`}; external sibling calls: only
`_asset_bytes`. Mechanism: mixin class, moved bodies verbatim, explicit
`self: "Replayer"` annotations (the one sanctioned deviation — annotation
only, zero runtime effect), `class Replayer(_IdentityVerificationMixin, ...)`.

### Step 3 — typed-input + selection verification mixin (~24 KB)

`_verify_typed_input`, `_typed_input_landed`, `_field_region`,
`_ocr_squashed`, `_readable_chars`, `_text_value_at` (self-attrs only
{`backend`, `vision`}) plus `_verify_selected_option`,
`_selection_region_compatible`, `_selection_target_continuous`,
`_mapped_selection_readback_region` (self-attrs {`vision`}) →
`openadapt_flow/runtime/replayer_readback.py`. External sibling calls to
audit at review time: `_deliver_backend_call`,
`_delivery_authorization_refusal`, `_revalidate_consequential_actuation`,
`_step_needs_consequential_revalidation`, `_cancel_guarded_keyboard`,
`_require_qualification_environment_current`, `_resolve_step`.

### Step 4 — postcondition polling mixin (~9 KB)

`_check_postconditions`, `_poll_postconditions`, `_postcondition_passes`,
`_postcondition_template`, `_expected_state_text`,
`_describe_postcondition`, `_drift_oracle_rescue` (self-attrs {`backend`,
`poll_interval_s`, `state_verifier`, `vision`}; external calls
`_asset_bytes`, `_structural_changed`).

### Later (needs design, not just relocation)

- The program interpreter (lines 2213–4817, ~130 KB) is the largest coherent
  region but is entangled with durable checkpoints, attended revalidation,
  and `_run_step`. Splitting it means first giving it an explicit context
  object instead of 67 shared attributes.
- `_run_step` / `_act` / `run` should shrink by delegation to the mixins
  above before any attempt to move them.

## 4. Non-goals

- No renames, no cleanup, no signature changes beyond the explicit
  `self: "Replayer"` annotations named in steps 2–4.
- No behavior change of any kind; every step must show a pure-relocation
  diff and green local gates before push.
