# openadapt-flow — Design & Module Contracts (v0)

**Product:** record a workflow demonstration once → compile it into a
deterministic, vision-anchored script → replay it locally with a resolution
ladder → when the UI drifts, heal the script (as a reviewable diff) instead of
re-reasoning every run.

The runtime is **vision-first, not vision-only**: it always CAN operate a pure
surface — consuming PNG bytes and emitting clicks/keys at pixel coordinates
through the `Backend` protocol (`openadapt_flow/backend.py`) — but where a
backend owns a structured layer (a browser DOM, a native UIA/AX tree) the
resolution ladder's TOP rung re-finds the recorded target as an ELEMENT and
acts on it deterministically (`StructuralActionBackend`, see Resolution ladder).
Structure is preferred where present; the visual ladder is the fallback floor
for pixel-only substrates (RDP/Citrix/canvas). Backend maturity is uneven and
stated honestly: the **shipped, end-to-end-exercised** backend is
Playwright-driven (headless-capable, CI-friendly, permission-free) and is the
only path proven against a real third-party app. Beyond it, a `WindowsBackend`
(UI Automation over the WindowsAgentArena server) is **proven structurally on a
local Windows-on-ARM VM** (record → compile → replay, DB-judged), and a FreeRDP
`RDPBackend` plus a Citrix/remote-display pixel-only backend exist as
**spikes, not validated integrations** — their live behavior is unmeasured to
the degree disclosed in `docs/backends/RDP.md` and `docs/desktop/CITRIX_PIXEL.md`.
They are adapters onto the one protocol, not rewrites.

## Core contracts (additive-only; do not change without updating this doc)

- `openadapt_flow/ir.py` — the schema. The **v0 core** — `Workflow` (a flat
  `list[Step]`), `Step`, `Anchor`, `Postcondition`, and the runtime result
  models (`RunReport`/`StepResult`/`HealEvent`) — is STABLE and changed only
  additively. It has since grown additively, and the linear
  `Workflow` remains the degenerate case of everything below (so bundles stay
  backward-compatible):
  - **SCROLL** (OpenEMR spike): `ActionKind.SCROLL` plus optional
    `Step.scroll_dx`/`Step.scroll_dy` wheel deltas.
  - **Structural rung**: `StructuralLocator` (DOM selector / role+name, or UIA
    AutomationId / role+name) on the anchor — the top, deterministic ladder rung.
  - **PHI-free identity**: `IdentityTemplate` / `TokenTemplate` / `ConcatTemplate`
    — a salted-hash, shape-preserving stand-in for the plaintext identity band,
    so a bundle can enforce the wrong-patient check with no readable PHI.
  - **Effect verification**: `Step.effects` typed system-of-record contracts
    (checked by an `EffectVerifier` against a REST / FHIR / document-hash record,
    not the pixels) and `ApiBinding` (the API-actuator leaf of the capability
    ladder — perform the write via a declared API call instead of the GUI).
  - **Workflow-program IR** (RFC `docs/design/WORKFLOW_PROGRAM_IR.md`): a
    parameterized state machine layered over the trajectory — `ProgramGraph`,
    `State`, `Transition`, `LoopSpec`, `Relation`, `Guard`, `Predicate`,
    `ParamSpec`. Induced from one or more traces
    (`openadapt_flow/compiler/induction.py`) and quarantined when intent stays
    underdetermined.
- `openadapt_flow/backend.py` — Backend protocol. FROZEN. Additive change
  (OpenEMR spike): `scroll(dx, dy)` — a wheel gesture at the current pointer
  position, so it scrolls whatever container is under the pointer (iframes
  included). SCROLL steps compile with NO postconditions: a scroll shifts the
  whole viewport, so frame diffs would assert mutable page content; the
  scroll's purpose (bringing the next target into view) is verified by the
  next anchored step's resolution ladder — and enforced at replay time by
  the closed-loop scroll semantics (see Runtime).

## Bundle & recording formats

**Workflow bundle** (compiler output, replayer input):
```
<bundle>/
  workflow.json      # ir.Workflow
  templates/*.png    # anchor crops (paths referenced from workflow.json)
```

**Recording** (recorder output, compiler input):
```
<recording>/
  meta.json          # {"id", "created_at", "viewport": [w,h], "app_url",
                     #  "params": {"<param_name>": "<value typed during demo>"}}
  events.jsonl       # one JSON object per line, in order:
                     # {"i":0,"kind":"click","x":123,"y":45,"t":1.20}
                     # {"i":1,"kind":"type","text":"...","param":"note","t":2.03}
                     # {"i":2,"kind":"key","key":"Enter","t":3.10}
                     # {"i":3,"kind":"scroll","dx":0,"dy":400,"t":4.02}
                     # "param" present iff the typed value is a parameter
  frames/{i:04d}_before.png
  frames/{i:04d}_after.png   # after the action settled
```

**Run directory** (replayer output):
```
<run>/
  report.json        # ir.RunReport
  steps/{step_id}_before.png / _after.png
  heals/{step_id}/…  # heal crops + heal.json per heal event
```

## Module map

| Area | Files |
|---|---|
| Mock app + backend + recorder | `openadapt_flow/mockmed/**`, `openadapt_flow/backends/playwright_backend.py`, `openadapt_flow/recorder.py`, `openadapt_flow/demo_driver.py`, `tests/test_mockmed.py`, `tests/test_recorder.py` |
| Vision + compiler | `openadapt_flow/vision/**`, `openadapt_flow/compiler/**`, `tests/test_vision.py`, `tests/test_compiler.py` |
| Runtime (ladder/heal/verify) | `openadapt_flow/runtime/**`, `tests/test_resolver.py`, `tests/test_replayer.py`, `tests/test_heal.py` |
| Report, emit, CLI, bench, CI | `openadapt_flow/report.py`, `openadapt_flow/bench.py`, `openadapt_flow/emit/**`, `openadapt_flow/__main__.py`, `.github/workflows/ci.yml`, `tests/test_report.py`, `tests/test_emit.py` |
| E2E integration | `tests/e2e/**` |

## Vision API

`openadapt_flow/vision/__init__.py` re-exports:

```python
class Match(BaseModel):
    point: Point          # click/center point, screen coords
    region: Region        # matched region, screen coords
    confidence: float

# match.py
def find_template(screen_png: bytes, template_png: bytes, *,
                  search_region: Region | None = None,
                  scales: tuple[float, ...] = (0.85, 1.0, 1.18),
                  threshold: float = 0.82) -> Match | None

# ocr.py  (rapidocr_onnxruntime; instantiate the engine once, module-level lazy)
class OcrLine(BaseModel):
    text: str; region: Region; confidence: float
def ocr(screen_png: bytes, *, region: Region | None = None) -> list[OcrLine]
def find_text(screen_png: bytes, text: str, *,
              region: Region | None = None, min_ratio: float = 0.8) -> Match | None
    # normalized fuzzy match (difflib ratio on lowercased/stripped text)

# hashing.py
def phash_png(png: bytes, region: Region | None = None) -> str
def phash_distance(a: str, b: str) -> int

# settle.py
def wait_settled(backend, *, interval_s: float = 0.1,
                 stable_frames: int = 2, timeout_s: float = 3.0) -> bytes
    # poll screenshots until N consecutive identical (phash dist 0) or timeout;
    # return the last PNG
```

## Compiler

`openadapt_flow.compiler.compile_recording(recording_dir, out_bundle_dir, *, name) -> Workflow`

Per click event (and `double_click`, compiled identically with action
DOUBLE_CLICK): crop a template around the click point (target-sized crop,
e.g. 160x64 clamped to frame, centered on click), OCR the crop for `ocr_text`,
extract up to 2 landmarks (nearest OCR lines outside the crop, carrying both
relation/distance and exact `dx_px`/`dy_px` offsets to the click point), set
`click_point`, `region`. Per type event: TYPE step with `text` or `param` (from
events.jsonl). Per scroll event: SCROLL step carrying the wheel deltas, no
anchor, no postconditions. Postconditions from the after-frame: pick the
largest changed region between before/after (cv2.absdiff + threshold +
bounding rect), REGION_STABLE with its phash plus a template crop of the
expected content (`templates/<step_id>_expect.png`) — the replayer first
searches for that content near the recorded region (real apps re-layout by a
few pixels between runs) and only then falls back to the exact-position
phash; plus TEXT_PRESENT for the most distinctive new
OCR text (text in after, not in before — compared whitespace-insensitively
so OCR jitter cannot make permanently visible chrome look "new"; prefer
longest). Parameterized typed values vary per run and are NEVER asserted in
any step's postconditions — including downstream steps whose after-frames
embed the typed value (e.g. a save-confirmation banner), and including the
pixel form: parameterized TYPE steps get no REGION_STABLE at all, since the
changed region is the typed value's own rendering. Click target labels
(any anchor's `ocr_text`) are likewise never asserted: they are mutable
evidence the resolution ladder heals through under rename drift, not
invariants. Intent: rule-based `"click '<ocr_text>'"` /
`"type <param or text preview>"` (VLM annotation is a later enhancement —
design for it, don't call any API).

Also emit a readable Python *rendering* of the workflow (`workflow.py` in the
bundle, generated, not parsed back) so humans can code-review the automation.

## Runtime

```python
class Replayer:
    def __init__(self, backend, *, vision=None, grounder=None): ...
    def run(self, workflow: Workflow, *, params: dict[str, str] | None = None,
            bundle_dir: Path, run_dir: Path,
            save_healed_to: Path | None = None) -> RunReport
```

Parameters not supplied in `params` fall back to the recorded example/default
values in `workflow.params`, so a bundle replays without any explicit params.

Resolution ladder per step with an anchor (record rung + confidence + ms).
The full capability hierarchy is API → tool/MCP → DOM/UIA → geometry → OCR →
template → VLM → human; API and tool/MCP are future placeholders, so the
implemented rungs are:

0. `structural` — DETERMINISTIC (additive change, structural-rung spike). When
   the live backend implements `StructuralActionBackend` (a browser DOM, a
   native UIA/AX tree) AND the anchor carries a `StructuralLocator` the compiler
   captured (a stable DOM selector / role+name, or a UIA AutomationId /
   role+name), the runtime re-finds the recorded target as an ELEMENT via
   `backend.locate_structural(locator)` and acts on its center — no pixel
   matching. Tried FIRST; a successful locate short-circuits the visual rungs.
   Pixel-only substrates (RDP/Citrix/canvas) and failed/ambiguous locates fall
   through to the visual rungs below UNCHANGED. This is the thesis refinement
   from "vision-only" to "deterministic compiled automation with visual
   FALLBACK" (desktop benchmark: UIA 21/21 vs compiled visual replay 6/21 under
   drift; reproduced by `benchmark/structural_action/`). The resolved point
   flows through the SAME click path, so the pre-click identity gate and the
   irreversible risk gate still fire on it — structure makes identity STRONGER
   (an exact element), it never bypasses it. Rungs 1–5 remain the visual
   FALLBACK floor and are unchanged:
1. `template` — find_template within anchor.region padded by search_pad
2. `template_global` — find_template full frame; for UNLABELED anchors
   (no ocr_text) the match is rejected when every locatable landmark places
   the target more than 40px away (repeated-icon UIs: an identical glyph on
   another card can outscore the true target when mutable content near the
   target changed) — the ladder then continues to ocr/geometry
3. `ocr` — find_text(anchor.ocr_text) full frame
4. `geometry` — landmarks: locate landmark text, offset by the exact
   `dx_px`/`dy_px` offsets when recorded, else by relation/distance
5. `grounder` — optional injected `Grounder.locate(png, intent) -> Match|None`
   (protocol in `runtime/grounder.py`; ship a `NullGrounder`; an Anthropic
   implementation goes behind the `grounder` extra and is NOT used in tests)

Ladder failures retry with fresh settled frames until `step.timeout_s`
(remote apps present settled-looking but still-loading frames; the target
often appears moments later). Structural errors (missing anchor) and the
risk gate do not retry.

**Closed-loop SCROLL:** a SCROLL step's execution semantics are "scroll
until the NEXT anchored step's anchor resolves, starting from the recorded
delta" — not "replay the recorded delta blindly". Open-loop fixed-count
scrolling breaks whenever content above the target grows between record and
replay (everything below lands displaced; see the OpenEMR findings). At
replay time a SCROLL step:

1. finds the next step (in workflow order) that carries an anchor; when
   none exists, or the recorded delta is zero, it falls back to the fixed
   recorded delta (one open-loop gesture);
2. probes that anchor against the current settled frame with a single
   ladder pass (no timeout retries, grounder never consulted — the loop
   stays model-free): if it already resolves, the step is a no-op (an
   earlier SCROLL step may have brought the target into view);
3. otherwise repeats scroll-by-recorded-delta → settle → probe until the
   anchor resolves, bounded by ~2.5x the step's own recorded scroll
   distance (`SCROLL_BUDGET_FACTOR`);
4. on budget exhaustion: if the immediately following step is another
   SCROLL step, it inherits the loop (a recorded run of N scrolls shares a
   combined ~2.5x budget, and trailing steps no-op via the probe-first
   rule once the target appears); otherwise the step FAILS loudly, naming
   the anchor that never came into view.

The anchored step that follows still re-resolves on its own settled frame
and remains the authority on where to click; the scroll loop only decides
when to stop scrolling.

Click point = matched region origin + (anchor.click_point - anchor.region
origin), scaled by match scale. After acting: `wait_settled`, then check
postconditions (poll until each passes or times out). Postcondition failure →
re-settle and re-check once → fail the step and abort the run (semantic drift
halts; the report must name the step and embed before/after screenshots).

**Heal:** when a step succeeds via any rung other than `template`, emit a
HealEvent: new template crop at the resolved region from the live frame,
updated region/click_point/ocr_text (re-OCR). Apply to the in-memory workflow;
if `save_healed_to` is set, write the healed bundle (updated workflow.json +
new crops). Record heal crops + heal.json under `run_dir/heals/<step_id>/`.
Irreversible steps: if resolution needed a rung below `ocr`, do NOT act — fail
with a clear "needs human confirmation" error (v0 policy).

## MockMed

Static single-page app (`openadapt_flow/mockmed/static/index.html` + app.js +
styles.css, no external resources, no CSS transitions/animations, font-size
≥ 14px). Served by `openadapt_flow.mockmed.server.serve(port=0) -> (url, stop)`
(threaded http.server). Hash-routed screens:

- `#login` — Username, Password fields; "Sign In" button
- `#tasks` — "Referral Tasks" table, rows with fake patient names
  (obviously fake: "Jane Sample", "Alex Testcase"), reason, priority, an
  "Open" button per row
- `#patient/<id>` — patient banner, "New Encounter" button, Encounters list
- `#encounter` — Type chooser as segmented BUTTONS ("Triage" / "Consult" —
  no native <select>), Note textarea, "Save Encounter" button
- after save → back to patient screen with a banner `Encounter saved — <first
  40 chars of note>` and the encounter listed

Drift modes via query string `?drift=a,b` (applied before render):
- `theme` — dark palette (breaks template matching for every anchor; OCR
  still works for labeled targets, while unlabeled input-field anchors fall
  through to the geometry rung)
- `move` — "New Encounter" and "Save Encounter" buttons relocated to the
  opposite side of their container (breaks local template search)
- `rename` — "Save Encounter"→"Submit Encounter", "Open"→"View" (breaks
  template + OCR; geometry rung must resolve — keep the button in the SAME
  position as default so landmarks/geometry succeed)
- `modal` — after clicking Save, a blocking "Survey" modal appears instead of
  the banner (semantic drift: replay must FAIL gracefully)

`demo_driver.py`: `record_triage_demo(url, out_dir, *, note_text, param_name
= "note") -> Path` — drives the canonical demo via Playwright locators (record
time may cheat with selectors; replay never does): login → tasks → Open first
referral → New Encounter → click "Triage" → click Note field → type note
(param) → click Save Encounter → done. It performs every action through
`Recorder` so frames/events are captured (before frame, act, wait settle,
after frame).

`PlaywrightBackend(page)` implements `Backend` (chromium, fixed viewport
1280x800, deviceScaleFactor=1). `Recorder(backend, out_dir)` wraps a backend
with the same action methods plus `type_text(text, param=None)` and
`finish() -> recording dir`.

## Report / emit / CLI / bench / CI

- `report.py`: `render_run_report(run_dir) -> Path` — REPORT.md in run_dir:
  outcome, per-step table (intent, rung, confidence, verified, ms, heal?), a
  per-step evidence section with embedded relative-path before/after images for
  EVERY step (each next to its resolution rung, identity-gate + effect-check
  verdicts, and heal/halt status) plus each heal's healed frame, rung
  histogram, totals (ms, model calls, est cost). The generator links only
  retained regular-file artifacts inside the run directory (never paths or
  symlinks that escape it) and never synthesizes pixels; a frame not retained
  on disk is marked absent. `render_bench_report(bench.json, out) -> Path`.
- `bench.py`: `run_bench(workflow_bundle, backend_factory, n) -> dict` —
  replay N times; success rate, p50/p95 total ms, rung histogram, model calls
  (0 in v0), cost 0; serialize bench.json.
- `emit/skill.py`: workflow bundle → `SKILL.md` folder (Agent Skills format:
  name, description, when-to-use, `openadapt-flow replay bundle --param k=v`
  invocation). The bundle is copied into the skill folder (`bundle/`) so the
  artifact is self-contained and portable. `emit/mcp_tool.py`: generate a
  standalone `server.py` exposing the workflow as an MCP tool (string
  template; must `ast.parse`; do not import mcp at generation time); the
  bundle is copied next to `server.py` and referenced relative to
  `__file__`, never by an emitting-machine absolute path.
- `__main__.py` CLI: `demo-record`, `compile`, `replay`, `bench`, `emit-skill`,
  `emit-mcp` (thin wrappers over the module APIs above). `replay --run-dir`
  is optional and defaults to `runs/replay-<UTC timestamp>`.
- CI (`.github/workflows/ci.yml`): ubuntu-latest, py3.12,
  `pip install -e .[dev]`, `playwright install --with-deps chromium`,
  `pytest -q --basetemp=runs/ci` (temp dirs pinned inside the workspace so
  run artifacts survive), upload `runs/**/REPORT.md` + PNGs as artifacts.

## Test policy

- Unit tests per module, cross-module deps faked/injected (the runtime must
  not import the vision module in unit tests — inject fakes; real parts are
  wired in `tests/e2e/`).
- Deterministic: no sleeps besides settle polling; server on ephemeral port.
- No network beyond localhost. No API keys required anywhere in tests.
- E2E matrix: baseline ×3 all-template zero-heal; theme / move /
  rename each succeed WITH heals then replay-healed all-template; modal fails
  gracefully naming the step; params substitution verified via banner OCR
  with a note value DIFFERENT from the recorded one (the identity case
  cannot distinguish substitution from replaying the baked-in literal); the
  irreversible-step risk gate exercised end-to-end (step marked irreversible
  + drift forcing a below-ocr rung must refuse to act).

## Repo policies

- Fake data only; no real patient/clinic/person names anywhere.
- No references to customers or design partners in this repo.
- Conventional commits; feature branches; never push to main.
