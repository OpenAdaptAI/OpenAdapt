# OpenAdapt Ecosystem Integration Roadmap

**Status:** decision-grade architecture memo (read-only analysis, no code changes)
**Scope:** how `openadapt-flow` should — and should not — adopt the rest of the
`openadapt-*` ecosystem.
**Author's note:** this memo covers **openadapt-types**, **openadapt-capture**, and
**openadapt-verifier**, plus overall sequencing. Three integrations are already owned by
other workstreams and are only referenced here for sequencing: **openadapt-privacy** (PHI
scrubbing), the **openadapt-grounding** evaluation, and the **`openadapt[flow]` umbrella
extra**.

---

## 0. TL;DR — recommendation per package

| Package | What it is | Overlap with flow | Recommendation | Risk |
|---|---|---|---|---|
| **openadapt-types** | Canonical Pydantic action schema (pydantic-only, zero heavy deps) | `ir.py` `ActionKind` ⊂ `ActionType`; flow's `Anchor`/`Postcondition`/`Resolution`/`IdentityCheck` are net-new | **Shim, don't swap.** Add an optional `to_openadapt_types()` / `from_openadapt_types()` interop layer at the boundary; keep `ir.py` as the internal source of truth | **Low** (additive, no blast radius) — but a full schema *swap* is **High** risk (44 files import `ir`) |
| **openadapt-capture** | Cross-platform desktop recorder (pynput + mss + PyAV), own SQLAlchemy schema | flow already ships `adapters/capture.py` — but it targets a **stale/assumed** capture schema | **Adopt via the public API, fix the adapter.** Rewrite the adapter onto `CaptureSession.load().actions()`; keep it an optional extra | **Medium** — current adapter reads a DB layout that no longer exists; it is dead/untested against real capture output |
| **openadapt-verifier** | Clinical/RWE **data-extraction** statistical validator | **None.** Shares only the word "verify" | **Leave standalone. Do not integrate.** | **N/A** — wrong tool; integrating would be a category error |

**Sequenced order:** (1) types interop shim → (2) capture adapter fix → then defer/skip
verifier. Privacy and grounding (other workstreams) slot around these; see §5.

---

## 1. Principles

flow's value proposition is a **lean, auditable, standalone core**: a demonstration
compiler whose replay loop makes **zero model calls**, runs in CI with no OS permissions,
and whose entire trust surface (`ir.py` + the resolution/identity ladders) can be read in
an afternoon. That smallness is not an accident of youth — it is a **certification-moat
asset**. Every dependency pulled into the replay path is something a regulated buyer's
security review must also vet.

The integration philosophy that follows from this:

1. **Adopt shared *vocabulary*, not shared *machinery*.** Speaking `openadapt-types`
   `Action` at the *boundaries* (import, export, interop with agents/evals) is pure
   upside. Dissolving flow's internal `ir.py` into a monorepo type is not — flow's IR
   carries compiler-specific evidence (`Anchor`, `Postcondition`, `IdentityCheck`) that
   the canonical schema deliberately does not model.
2. **Close *real* gaps with dedicated packages.** Where the ecosystem genuinely does
   something flow can't (cross-platform desktop **capture**; PHI **scrubbing**;
   VLM **grounding**), integrate — behind an optional extra, off the default path.
3. **Never put a heavy dependency on the replay hot path.** The replay loop is the
   moat. Recording, grounding, and privacy are *edges* (compile-time or opt-in); the
   core stays `pydantic + opencv + rapidocr + pillow`.
4. **Prefer additive interop over swaps.** flow moves fast and its schema is load-bearing.
   `ir.py` is imported by **44 files** and marked FROZEN in `DESIGN.md`. A schema swap has blast
   radius across the whole compiler, runtime, and benchmark surface; an interop shim has
   none.

> One-line creed: **adopt the words, keep the core.**

---

## 2. openadapt-types — canonical action schema

### 2.1 What it provides

`openadapt-types` (v0.1.0, PyPI) is a **pydantic-only, zero-heavy-dep** schema library
("No ML libraries, no heavy deps" — its README). Public surface:

- `ActionType` (str-enum, 21 members): `CLICK, DOUBLE_CLICK, RIGHT_CLICK, DRAG, SCROLL,
  HOVER, TYPE, KEY, HOTKEY, GOTO, BACK, FORWARD, REFRESH, OPEN_APP, CLOSE_APP,
  WINDOW_FOCUS, WAIT, SCREENSHOT, DONE, FAIL, ANSWER`.
- `Action` (`type`, `target`, `text`, `key`, `modifiers`, `scroll_*`, `url`, …) with a
  `model_validator` enforcing per-type required fields (TYPE⇒text, KEY⇒key, …).
- `ActionTarget` (`node_id` / `description` / `x,y,is_normalized`) — grounding priority
  node_id > description > coords.
- `ActionResult` (`success`, `error`, `error_type∈{grounding_error, execution_error,
  state_mismatch, timeout, permission_denied, infrastructure_error}`, `duration_ms`,
  `changed_node_ids`, `resolved_coordinates`).
- `ComputerState` / `UINode` / `BoundingBox` / `ProcessInfo` — observation graph.
- `Episode` / `Step` — trajectory container (for evals/RL).
- Parsers: `parse_action` (auto JSON/DSL), `parse_action_dsl`, `parse_action_json`,
  `from_benchmark_action` / `to_benchmark_action_dict` — all **fail-safe** (return
  `Action(type=DONE)` on malformed input, never raise).

It is a **passive data schema**: no executor, no resolver, no verification logic,
no anchors, no template crops, no OCR labels, no postconditions.

### 2.2 What flow reimplements that overlaps

flow's `openadapt_flow/ir.py` defines its own action vocabulary:

- `ir.ActionKind` (`ir.py:33-39`): `CLICK, DOUBLE_CLICK, TYPE, KEY, WAIT, SCROLL`
  (6 members). Used in **27 sites** across the compiler/runtime.
- `ir.Step` (`ir.py:157-...`) is flow's per-step record: `action`, `anchor`, `text`,
  `param`, `key`, `scroll_dx/dy`, `expect`, `risk`, `identity_armed`.
- `ir.Workflow` (`ir.py:...`) is the compiled bundle root (`schema_version`, `name`,
  `params`, `steps`, `save()/load()`).

### 2.3 The DELTA

**Maps 1:1 (flow → types):** every `ActionKind` member is a subset of `ActionType`
with identical string values:

| flow `ActionKind` | value | types `ActionType` |
|---|---|---|
| `CLICK` | `"click"` | `CLICK` ✅ |
| `DOUBLE_CLICK` | `"double_click"` | `DOUBLE_CLICK` ✅ |
| `TYPE` | `"type"` | `TYPE` ✅ |
| `KEY` | `"key"` | `KEY` ✅ |
| `WAIT` | `"wait"` | `WAIT` ✅ |
| `SCROLL` | `"scroll"` | `SCROLL` ✅ |

The string values are byte-identical, so a flow `Step` could emit/ingest an
`openadapt_types.Action` with a trivial field map (click_point → `ActionTarget(x,y)`,
`text` → `text`, `key` → `key`, `scroll_dx/dy` → `scroll_direction`/`scroll_amount`).

**flow has that types lacks (the compiler-specific IR — net-new, keep):**

- `Anchor` — redundant visual evidence: `template` crop path, `region`, `click_point`,
  `ocr_text`, `context_text`, `structured_identity`, `identifier_crop/region`,
  `landmarks`, `search_pad`.
- `Landmark` — geometry-rung stable-text offsets.
- `Postcondition` / `PostconditionKind` — `TEXT_PRESENT/ABSENT`, `REGION_STABLE` (phash),
  `URL/TITLE_CHANGED`, `NEW_TAB_OPENED`.
- `Resolution` — which ladder rung resolved the target (`template/…/grounder`), point,
  confidence, `elapsed_ms`.
- `IdentityCheck` — the pre-click same-entity verdict (`verified/mismatch/abstain/
  unreadable` × `structured/pixel/vlm/context/param`). This is the wrong-patient safety
  core.
- `HealEvent`, `StepResult`, `RunReport`, `UnarmedStep` — audit/telemetry.
- `risk∈{reversible,irreversible}`, `identity_armed` — the halt-on-uncertainty gates.

None of these exist anywhere in `openadapt-types`. The closest analogs are
`UINode.automation_id/xpath/css_selector` (string locators, not visual anchors) and
`ActionResult.error_type="state_mismatch"` (a bare enum literal, not a postcondition
engine).

**types has that flow lacks (worth borrowing at the boundary, not the core):**

- `ActionTarget.node_id` / `UINode` graph — a structured-element addressing model flow
  only touches via its optional `IdentityBackend.structured_text_at`.
- Fail-safe **DSL/JSON parsers** — useful when flow emits skills/MCP tools that an agent
  or evals harness must round-trip.
- `Episode`/`Step` trajectory container — the lingua franca of `openadapt-evals`; useful
  when flow's `RunReport` needs to feed the eval/RL flywheel.
- A **wider `ActionType`** (right-click, drag, hotkey, goto) — flow will grow into some of
  these; aligning values now avoids a later rename.

### 2.4 Migration shape — **compatibility shim (adopt the vocabulary, keep the IR)**

Do **not** replace `ir.ActionKind` / `ir.Step` with `openadapt_types.Action`. Reasons:

- **Impedance mismatch.** flow's `Step` is a *compiled artifact* (anchor + postconditions
  + identity gates). `openadapt_types.Action` is an *instantaneous intent*. They are
  different layers; forcing flow's IR to be the canonical `Action` would either bloat the
  canonical schema with compiler internals or strip flow's evidence out of its own IR.
- **Blast radius.** `ir` is imported by 44 files and 27 `ActionKind.` call-sites, and is
  marked **FROZEN** in `DESIGN.md` (additive changes only).
  A swap touches the compiler, every runtime rung, the healer, the benchmark harness, and
  the emit/skill + MCP surfaces at once. That is a major refactor, not an additive dependency.

**Recommended concrete shape** (optional, additive):

```
openadapt_flow/interop/types.py   # new, optional
  def step_to_action(step: ir.Step, resolution: ir.Resolution|None) -> "openadapt_types.Action"
  def action_to_step_stub(a: "openadapt_types.Action") -> ir.Step   # for ingest
  def result_to_action_result(r: ir.StepResult) -> "openadapt_types.ActionResult"
```

- Guard the import (`try: import openadapt_types`) so the core never hard-depends on it.
- Expose it under an optional extra: `openadapt-flow[types]` (or fold into the umbrella
  `openadapt[flow]` extra the other workstream owns).
- Use it in exactly two places where the *boundary* benefits:
  1. **emit/** (skill + MCP tool generation) — so a flow bundle can describe its steps in
     the ecosystem's canonical action language.
  2. **benchmark/** — so flow's runs can serialize as `openadapt_types.Episode`/`Step`
     for the evals/RL flywheel instead of a bespoke JSON.

**Risk: Low.** Purely additive, no change to `ir.py`, no change to the replay hot path,
trivially revertible. The only ongoing cost is keeping the field map in sync if
`ActionType` gains members flow starts using — a one-function maintenance surface.

> If a future release wants tighter alignment, the safe next step is to make
> `ir.ActionKind`'s **values** authoritative-compatible (they already are) and add a CI
> test asserting `set(ActionKind) ⊆ set(ActionType)`, so drift is caught without a swap.

### 2.5 Priority

**First.** It's the cheapest, lowest-risk, highest-leverage integration and it's the
"shared vocabulary" principle in action. It also unblocks clean interop with evals and
with whatever the umbrella-extra workstream assembles. Ship the shim before the capture
rewrite.

---

## 3. openadapt-capture — cross-platform desktop recording

### 3.1 What it provides

`openadapt-capture` (v0.5.1) is the ecosystem's **desktop recorder**:

- `Recorder(capture_dir, task_description, …)` context manager — spawns reader threads
  for mouse/keyboard/screen/window/browser (pynput + mss + PyAV), writes a per-capture
  **`recording.db`** (SQLAlchemy) + action-gated `oa_recording-*.mp4` + optional audio.
- `CaptureSession.load(dir)` → `.actions(include_moves=False)` yields public `Action`
  dataclasses (`.timestamp`, `.type` e.g. `mouse.singleclick`/`key.type`, `.x/.y/.dx/.dy`,
  `.button/.text/.keys`, lazy `.screenshot` PIL frame via `get_frame_at`). Also
  `.raw_events()`, `.browser_events()`, and metadata (`platform`, `screen_size`,
  `pixel_ratio`, `duration`, `task_description`, `video_path`).
- Cross-platform input via **pynput** (macOS Quartz / Windows hooks / X11); screenshots
  via **mss**; per-click element state and window geometry available but **off by
  default** (`RECORD_WINDOW_DATA=False`, `RECORD_READ_ACTIVE_ELEMENT_STATE=False`).
- Import stays headless-safe (guarded so `Recorder=None` without a display); **actual
  recording** pulls native deps and needs OS input-monitoring/screen-recording
  permission. No OCR. Its own schema — **no dependency on openadapt-types.**

### 3.2 What flow reimplements / already has

flow's **native/desktop recording gap** is real: `recorder.py` records only through a
`Backend` (the Playwright reference backend); there is no cross-platform OS-level capture.
flow already anticipated this with `openadapt_flow/adapters/capture.py` +
`convert_capture()`, documented in `docs/desktop/PHASE1.md` — an adapter that converts a
capture session into flow's recording format (`meta.json` + `events.jsonl` + `frames/`).

### 3.3 The DELTA — and a concrete defect

**Maps well conceptually:** capture's derived action types line up with flow's event
kinds (`mouse.singleclick`→click, `mouse.doubleclick`→double_click, `key.type`→type,
`key.down`→key, `mouse.scroll`→scroll). The adapter already handles the hard parts:
logical-point→physical-pixel scaling via `pixel_ratio`, wheel-notch→pixel conversion,
param-value tagging, and loud rejection of untranslatable events (`mouse.drag`,
`key.shortcut`) so a demonstrated action is never silently dropped.

**capture has that flow lacks:** genuine cross-platform desktop recording (flow can't
record outside a browser backend); per-event window geometry and element/a11y state
(opt-in); audio narration + Whisper word timestamps; browser semantic-element refs
(role/name/bbox/xpath/css).

**flow has that capture lacks:** the perceptual-hash **settle wait** (capture is
action-gated video, so the adapter approximates settle by sampling a frame at
`t+settle_s`), OCR labels, template crops, and everything downstream in the compiler.

**⚠️ Concrete defect (load-bearing finding):** `adapters/capture.py` reads the **wrong
schema**. It opens a raw `capture.db` with a flat `events(timestamp, type, data JSON)`
table and a `capture(screen_width, video_start_time, …)` metadata row. The **actual**
openadapt-capture 0.5.1 writes `recording.db` (SQLAlchemy `Recording`/`ActionEvent`
models) and exposes the public `CaptureSession.load(dir).actions()` API. The filenames
(`capture.db` vs `recording.db`, `video.mp4` vs `oa_recording-*.mp4`) and the access
pattern (raw SQL vs public API) do **not** match. The adapter was written against an
assumed/older/hypothetical capture layout and is effectively **dead code against real
capture output** — it will `FileNotFoundError` on `capture.db` the moment it meets a real
session.

### 3.4 Migration shape — **adopt the dependency, rewrite the adapter onto the public API**

- **Rewrite `convert_capture()` to consume `CaptureSession.load(dir).actions()`** instead
  of hand-rolled SQL against a non-existent table. This deletes the fragile raw-schema
  coupling, gets the `pixel_ratio` and frame extraction from capture's own tested code
  (`Action.screenshot` / `get_frame_at`), and survives capture's future schema changes
  (they'll be absorbed behind capture's public API).
- **Keep it an optional extra.** `openadapt-flow[capture]` pulling `openadapt-capture`.
  The core replay path never imports it; only the `demo-record`-from-desktop compile edge
  does. This preserves the permission-free, CI-friendly core.
- **Do not** replace flow's Playwright `Recorder` — that is the reference recorder that
  keeps the whole loop runnable in CI with zero OS permissions. capture is the **desktop**
  on-ramp, not a replacement.
- **Preserve the loud-rejection contract.** The current adapter's refusal to silently drop
  `mouse.drag`/`key.shortcut` is exactly the right wrong-action-safety posture; keep it
  when moving to the public API.

**Risk: Medium.** The direction is clearly right (real gap, dedicated package, adapter
already scoped), but the existing adapter is **untested against real capture output** and
targets a schema that isn't there — so "integration" here is really "finish + correct a
half-built bridge," and it needs an integration test against an actual recorded session
(which needs OS permissions, i.e. not pure-CI). Until that test exists, treat desktop
capture as **experimental**.

### 3.5 Priority

**Second.** After the types shim, before anything speculative. It closes flow's single
biggest capability gap (desktop, not just browser) and the scaffolding already exists —
the work is correcting it, not greenfield. Gate it behind an extra so it never threatens
the core's leanness. Sequence it *after* privacy lands if capture is to feed PHI-bearing
desktop recordings (see §5), since desktop capture is exactly where PHI scrubbing matters.

---

## 4. openadapt-verifier — **do not integrate**

### 4.1 What it actually is

Despite the name, `openadapt-verifier` (v0.1.0) is a **clinical / real-world-evidence
(RWE) data-extraction validator**. It scores already-extracted structured field
predictions against a human gold standard and emits an FDA-credibility-framed statistical
report (Kahn conformance/completeness/plausibility checks, Wilson/Clopper-Pearson
confidence intervals, Cohen/Fleiss kappa, `validate()`/`verify()`/`compare()` over a
`Dataset` of `FieldSpec`+gold+pred `Record`s).

A grep of the package for `screenshot|ocr|template|ssim|pixel|dom|vlm|opencv|cv2` returns
**nothing**. It has **no dependency on openadapt-types**, no `ActionResult` coupling, no
images, no actions. It shares exactly one thing with flow's verification: the word
"verify."

### 4.2 The DELTA — disjoint

flow's "verification" is `runtime/identity.py` + `Postcondition` + the resolution ladder:
per-click same-entity checks, screen-state postconditions (phash/text/URL), halt-on-
uncertainty, wrong-action detection. openadapt-verifier's "verification" is offline
statistical scoring of extracted clinical fields. **Zero API overlap. Zero conceptual
overlap** beyond both caring about "correctness."

### 4.3 Migration shape — **leave standalone (category mismatch)**

Integrating openadapt-verifier into flow's replay/postcondition path would be a category
error — it does not verify GUI state and cannot. flow's postcondition/identity machinery
is *correctly* home-grown because **nothing in the ecosystem does GUI-state verification**;
this is genuinely flow-specific IP, and keeping it in `ir.py`/`runtime/` is the right call.

**The one legitimate (future, out-of-scope) touchpoint:** if flow ever ships a
*benchmark-accuracy* report — "the compiled workflow wrote the correct field values into
the EMR across N runs, with confidence intervals" — then openadapt-verifier is the right
tool for **that** report (it's literally a clinical-extraction accuracy scorer with CIs and
regression `compare()`). That is a *benchmark/reporting* use, entirely separate from the
runtime, and not part of this roadmap. Do not couple the runtime to it.

**Risk: N/A.** The recommendation is non-integration.

---

## 5. Sequenced roadmap

Ordering across **all** in-flight integrations (types/capture/verifier owned here;
privacy/grounding/umbrella owned by other workstreams):

| # | Integration | Buys | Risks | Path-of-dependency | Verdict |
|---|---|---|---|---|---|
| **1** | **types interop shim** | Shared action vocabulary; clean evals/emit round-trip; future-proofs `ActionType` alignment | Low; field-map maintenance only | Boundary only (emit/, benchmark/); optional extra | **Do now** |
| **2** | **privacy** *(other workstream)* | PHI scrubbing on recordings/frames — prerequisite for any healthcare desktop capture | Medium; must scrub at capture/compile edge, never leak into bundles | Compile-time edge; optional extra | **Do (owned elsewhere); land before desktop capture ships to clinics** |
| **3** | **capture adapter fix** | Real cross-platform **desktop** recording (flow's biggest gap) | Medium; current adapter targets a dead schema; needs a permissioned integration test | Compile-time on-ramp; optional `[capture]` extra | **Do after 1–2** |
| **4** | **grounding eval** *(other workstream)* | Evidence for/against the optional grounder rung; keeps the ladder honest | Low if kept opt-in; High if grounder creeps onto the default path | Opt-in last ladder rung only | **Evaluate (owned elsewhere); keep OFF by default** |
| **5** | **umbrella `openadapt[flow]` extra** *(other workstream)* | One-line install of the composed stack | Low; packaging only — must not make heavy deps mandatory | Packaging metadata | **Do last; assemble the optional extras above** |
| **—** | **verifier** | Nothing for the runtime | — | — | **Do NOT integrate** (see §4) |

### Why this order

- **types first** because it's the cheapest and it's a *precondition for clean interop* on
  everything downstream (emit, benchmark, evals). It also has zero effect on the hot path,
  so it can't destabilize the current release.
- **privacy before desktop capture** because desktop capture is precisely where
  PHI-bearing pixels and keystrokes enter the system. Recording a clinic desktop without a
  scrub step is the one integration ordering that is *unsafe* to get wrong.
- **capture third** because the scaffolding exists but is broken against the real schema —
  it's correction work with a real test dependency (OS permissions), so it shouldn't block
  the cheap wins.
- **grounding evaluated, not adopted-by-default** — the grounder is the *last, optional*
  ladder rung by design. The moment it becomes mandatory, flow loses its "$0, no model
  calls, runs in CI" headline. Keep it opt-in behind `[grounder]`.
- **umbrella last** because it's the bow on top: it should compose the optional extras
  above, and must never promote any of them to a mandatory core dependency.

---

## 6. Where staying standalone is the right call

flow's standalone lean surface is a deliberate strength — a certification-moat asset, not
tech debt. Explicit "keep it home-grown" calls:

1. **`ir.py` stays the internal source of truth.** Interop with `openadapt-types` at the
   boundary; never dissolve the compiler IR (Anchor/Postcondition/IdentityCheck) into the
   canonical schema. Those are flow's differentiators and don't belong in a passive shared
   type.
2. **The replay hot path stays dependency-minimal.** `pydantic + opencv + rapidocr +
   pillow + imagehash`. Grounding, capture, privacy, and types-interop are all **edges**
   (compile-time or opt-in), never core. The property that "the whole loop runs in CI with
   no OS permissions and zero model calls" is the product; protect it.
3. **Postcondition + identity verification stays home-grown.** Nothing in the ecosystem
   does GUI-state verification (openadapt-verifier is a clinical scorer). This is
   flow-specific IP; keep it in `runtime/`.
4. **The Playwright reference recorder stays.** capture is the desktop on-ramp, not a
   replacement — the permission-free CI recorder is what keeps the whole test story cheap.
5. **The `Backend` protocol stays small.** New substrates (Windows/Parallels/RDP)
   implement the tiny vision-only protocol; they do not pull the ecosystem into the
   runtime.

The governing test for any future integration proposal:

> **Does it add shared vocabulary or close a real capability gap — at an edge — without
> putting a new dependency on the replay hot path or dissolving the auditable core?**
> If yes, integrate behind an optional extra. If no, stay standalone.

---

## Appendix A — flow `ir` ↔ `openadapt-types` field map (for the shim)

| flow `ir` | openadapt-types | Note |
|---|---|---|
| `ActionKind.{CLICK,DOUBLE_CLICK,TYPE,KEY,WAIT,SCROLL}` | `ActionType.{same}` | identical string values; `set(ActionKind) ⊆ set(ActionType)` |
| `Step.action` | `Action.type` | 1:1 |
| `Anchor.click_point (x,y)` | `Action.target = ActionTarget(x,y,is_normalized=False)` | pixel coords |
| `Anchor.ocr_text` / `context_text` | `ActionTarget.description` (lossy) | flow's is richer; export only |
| `Step.text` / `Step.key` | `Action.text` / `Action.key` | 1:1; validators agree |
| `Step.scroll_dx/dy` | `Action.scroll_direction` + `scroll_amount` | sign/px→direction conversion |
| `StepResult{ok,error,elapsed_ms,resolution.point}` | `ActionResult{success,error,duration_ms,resolved_coordinates}` | export flow→types |
| `Anchor/Postcondition/Landmark/IdentityCheck/HealEvent` | *(none)* | net-new; **do not** try to map |
| `RunReport` | `Episode`(+`Step`) | for evals/RL flywheel only |

## Appendix B — dependency-weight ledger

| Package | Import weight | On flow's hot path? |
|---|---|---|
| openadapt-types | pydantic only (light) | No — boundary/optional |
| openadapt-capture | pynput + mss + PyAV + sounddevice (heavy, native, needs perms to record; import stays headless-safe) | No — compile-time on-ramp, optional extra |
| openadapt-verifier | stdlib-only (light) | N/A — not integrated |
| flow core | pydantic, numpy, opencv-headless, pillow, imagehash, rapidocr, playwright, httpx | This IS the hot path; keep it here |
