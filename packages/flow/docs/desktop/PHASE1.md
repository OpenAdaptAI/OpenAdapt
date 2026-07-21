# Desktop Spike — Phase 1: WindowsBackend + Recording-Adapter Contract

Phase 1 de-risks the desktop integration (spec §2.1–§2.2). It does **not**
run the benchmark matrix — that is Phase 2, gated on the identity matcher
settling (PRs #16/#17). No model calls, no agent arm, $0 API spend.

## Infrastructure path taken: contract mock, not the live VM

The spec assumes the Azure WAA pool VM. Reality check at execution time:
`az vm list` returned **zero VMs across all enabled subscriptions** — the
`waa-pool-XX` pool does not currently exist, and bringing one up means
`pool-create` plus a ~20–35 minute Windows install (plus VM spend). Per the
phase plan, the WindowsBackend was therefore built and validated against a
**local stateful mock of the WAA HTTP contract**; the real-VM smoke test is
deferred to Phase 2 as a small wiring step.

The contract itself is taken from the authoritative client in
openadapt-evals (`openadapt_evals/training/standalone/waa_direct.py`,
the WAADirect pattern — plain HTTP, no adapter layer; the WAALiveAdapter is
known to crash the server):

| Endpoint | Contract |
|---|---|
| `GET /screenshot` | raw PNG bytes via Flask `send_file()` — **not** base64 JSON; read `resp.content` |
| `POST /execute_windows` | `{"command": "<bare Python statements>"}`; the server runs `exec(command, ...)` with `pyautogui` importable |

**Correction to project memory:** the note "WAA `/execute` needs a
`python -c "..."` wrapper" applies to a *different* endpoint. For
`/execute_windows` the WAADirect docstring is explicit: send bare Python
statements, do **not** wrap in `python -c`. This backend follows WAADirect
(and its tests `exec()` every emitted command to prove it is valid bare
Python).

## WindowsBackend (`openadapt_flow/backends/windows_backend.py`)

Implements the existing 4-method vision-only `Backend` protocol
(screenshot / click / type_text / press, + scroll) over the WAA HTTP API,
mirroring `PlaywrightBackend`'s shape. ~180 LOC. Design points:

- **Vision-only, honestly.** It does *not* implement the optional
  `StructuralBackend` observations (url / page_title / page_count): native
  Windows has no cheap equivalent, so steps that would rely on structural
  postconditions stay unverified (docs/LIMITS.md), exactly as the protocol
  intends. The replayer probes these with `getattr(..., None)` — absence is
  handled, not an error.
- **Viewport** is derived once from the first screenshot's PNG header
  (IHDR), overridable in the constructor. No resolution assumption.
- **Command safety.** Typed text is embedded via `repr()` — a valid Python
  literal, immune to quote/backslash injection (WAADirect's manual escaping
  is a known sharp edge).
- **Non-ASCII typing.** `pyautogui.write` silently drops characters it
  cannot type — a *silent wrong-write* mode. Non-ASCII text is therefore
  routed through the clipboard: base64 → PowerShell `Set-Clipboard` →
  Ctrl+V. ASCII text uses plain `pyautogui.write`.
- **Keys.** Playwright-style names/chords (what recordings and the replayer
  emit, e.g. `Enter`, `ControlOrMeta+a`, `Meta+d`) are normalized to
  pyautogui names (`enter`, `hotkey('ctrl','a')`, `hotkey('win','d')`);
  `ControlOrMeta` resolves to Ctrl on Windows.
- **Scroll.** The protocol speaks pixels (Playwright wheel, +dy = view
  down); pyautogui speaks wheel notches with the opposite vertical sign.
  Conversion is 100 px/notch (approximate by design — replay's closed-loop
  scroll re-resolves after every gesture, so the ratio is not load-bearing;
  tune on the real VM in Phase 2).
- **Screenshots** retry (server momentarily unready), and payloads are
  validated as PNG before being returned.
- `requests` is the only new dependency, behind the `windows` extra
  (`pip install 'openadapt-flow[windows]'`); the `backends` package
  re-exports lazily so nothing changes for browser-only users.

### Protocol conformance result: zero compiler/replayer changes

`tests/test_windows_backend.py::test_record_compile_replay_over_windows_backend`
runs the **unmodified** Recorder → compiler → Replayer stack over the
WindowsBackend against a stateful mock WAA app (a cv2-drawn 3-step
click → type-note → Enter workflow whose screen state advances only when
the emitted commands are correct, coordinate-checked): recording, bundle
compilation, and a successful replay (`report.success`, postconditions
verified by real OCR, typed-input verification passing) all work with no
changes to any existing module. The 4-method protocol held.

No abstraction leaks found in Phase 1. Two soft spots to watch on the real
VM (not leaks, tuning): screenshot latency vs. the settle-detection
polling budget, and the px/notch scroll ratio.

## Recording-adapter contract (`openadapt_flow/adapters/capture.py`)

The spike's main integration risk (spec §2.2): desktop demonstrations come
from **openadapt-capture**, whose output format was reverse-engineered from
the sibling repo (`openadapt_capture/events.py`, `storage`), and converted
to the flow recording format the compiler consumes.

### Input schema (openadapt-capture session directory)

```
<capture>/
  capture.db   # SQLite
  video.mp4    # screen video; frame wall-clock time =
               #   capture.video_start_time + frame_pts_seconds
```

`capture.db` tables (relevant columns):

- `capture(id, started_at, ended_at, platform, screen_width, screen_height,
  pixel_ratio, video_start_time, task_description, ...)` — one row.
- `events(timestamp REAL, type TEXT, data JSON, parent_id)` — raw streams
  (`mouse.move/down/up`, `key.down/up`, `screen.frame`, `audio.chunk`) plus
  *derived* action events produced by openadapt-capture's post-processing
  (`process_events`): `mouse.singleclick`, `mouse.doubleclick`,
  `mouse.drag`, `key.type`, `mouse.scroll`.

### Output schema (openadapt-flow recording, unchanged)

```
<recording>/
  meta.json      # {"id", "created_at", "viewport": [w,h], "app_url": null,
                 #  "params": {...}, "source": "openadapt-capture",
                 #  "task_description": ...}
  events.jsonl   # {"i":0,"kind":"click","x":..,"y":..,"t":..} etc.
  frames/{i:04d}_before.png / _after.png
```

### Conversion rules

| capture event | flow event | notes |
|---|---|---|
| `mouse.singleclick` (left) | `{"kind":"click"}` | non-left buttons: **rejected** (no flow equivalent) |
| `mouse.doubleclick` (left) | `{"kind":"double_click"}` | |
| `key.type {text}` | `{"kind":"type","text",...}` | marked `"param"` when the text equals a value in the caller's `params` map |
| `key.down` (named, non-modifier) | `{"kind":"key","key"}` | pynput names mapped to Playwright names (`enter`→`Enter`, `esc`→`Escape`, `up`→`ArrowUp`, ...); bare modifier presses skipped |
| `mouse.scroll {dx,dy}` | `{"kind":"scroll"}` | pynput notches (+dy = scroll up) → pixels at 100 px/notch with vertical sign flipped (+dy = view down); signs provisional until verified on hardware |
| `mouse.move`, `mouse.down/up`, `key.up`, `screen.frame`, `audio.chunk` | skipped | already merged into derived events / irrelevant to replay |

**Loud-failure rules** (silent drops are wrong-action seeds):

- Raw-only sessions (no derived events) → `ValueError`: run
  openadapt-capture's `process_events` first; deriving actions is the
  capture library's job, not the adapter's.
- `mouse.drag`, `key.shortcut`, and any *unknown* `mouse.*`/`key.*` type →
  `ValueError` (converting would silently drop a user action).
- Unmapped named keys → `ValueError` naming the key.
- Two params demonstrating the same value → `ValueError` (ambiguous
  marking).

**Coordinate spaces.** Capture mouse coordinates are logical points
(pynput); video frames are physical pixels (Retina/HiDPI differ by
`pixel_ratio`). Flow requires event coordinates in frame-pixel space, so
points are scaled by `video_frame_width / capture.screen_width` — the video
is authoritative, not the stored ratio.

**Window-scoped sessions.** A session recorded with capture's window mode
(`CaptureSession.window_capture` not None, `coordinate_space:
"window_pixels"`) already stores coordinates in the captured frame's pixel
space, so the adapter applies **no** scaling (rescaling would double-scale
every click), screens each mouse action against the recorded bounds-timeline
window events (out-of-window input refuses conversion — it targeted a
different window), refuses sessions where the target window was resized
(capture video and Flow recordings currently use one fixed viewport), verifies
extracted frames have that exact viewport, and stamps the output `meta.json`
with `window_capture` provenance plus `backend_hints` (`rdp_window` /
`rdp_window_title`) naming the recorded target window for
`replay --backend rdp`.

**Frame selection.** For an event at wall-clock `T`: *before* = last video
frame at/before `T`; *after* = frame at `T + settle_s` (default 1.0 s),
clamped to just before the next event. This approximates the live
Recorder's perceptual-hash settle wait offline; if it proves too coarse on
real captures, the fix is a phash-based settle scan over the video segment
— same module, no format change.

### Contract validation

`tests/test_capture_adapter.py` builds a synthetic capture session — a real
SQLite `capture.db` in openadapt-capture's exact schema and a real
`video.mp4` (cv2-encoded, Retina-style 2× pixel ratio) — converts it, and
asserts: event mapping/order, recorder line-format parity, coordinate
scaling, before/after frame selection (classified against the app's state
ladder), param marking, and every loud-failure rule. The converted
recording is then compiled by the **unmodified** `compile_recording`
(click/type/key steps, param preserved). The format bridge is proven.

## Proven vs deferred

Proven in Phase 1 (all offline, mock-contract):

- WindowsBackend implements the Backend protocol; commands match the WAA
  wire format (every emitted command is `exec()`'d in tests the way the
  server does it).
- Zero compiler/replayer changes needed — full record→compile→replay loop
  over the WindowsBackend succeeds.
- capture→flow recording contract defined, implemented, and compiler-
  accepted on a synthetic demonstration.

Deferred to Phase 2 (needs VM / matcher settled):

1. Bring up a WAA pool VM (`oa-vm pool-create` → `pool-wait`; never
   `az vm restart`) and run the real 2–3-step smoke (Notepad-class app)
   through the WindowsBackend: screenshot latency vs settle budget, scroll
   px/notch, pyautogui key-name coverage, clipboard-paste path on real
   PowerShell. (Note: the local `oa-vm` CLI itself is currently broken by a
   NumPy 2 incompatibility in openadapt-evals' import chain — fix or use
   raw `az` + SSH tunnel.)
2. Convert a *real* openadapt-capture recording (incl. `process_events`
   derivation, real video timing) — the synthetic test pins the schema, not
   capture's runtime quirks.
3. OpenDental install + DB ground truth, drift matrix, identity/false-abort
   numbers on desktop rendering, UIA steelman arm, Mode B (RDP stream) —
   the benchmark proper (spec §3–§7), after PRs #16/#17 land.
