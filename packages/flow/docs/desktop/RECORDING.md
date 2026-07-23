# Desktop recording — `record --backend windows|macos|linux|rdp|citrix`

`openadapt-flow record` records a workflow **once** so it can be compiled into a
deterministic, vision-anchored script and replayed. This page documents the
**desktop** side of that verb — capturing a workflow the operator performs on a
native Windows desktop (`--backend windows`) or a remote display / Citrix
(`--backend rdp|citrix`) — so `record → compile → replay` closes through the product
CLI on the desktop substrate, not just the browser.

```
openadapt-flow record --backend windows --out rec/ --task "triage note"
openadapt-flow compile rec/ --out bundle/ --name triage
openadapt-flow replay bundle/ --backend windows --agent-url http://localhost:5001
```

## What it does (and what it reuses)

Desktop capture is **not reinvented**. The
`record --backend windows|macos|linux|rdp|citrix` path
is a thin orchestration over two components that already exist and are tested:

1. **[openadapt-capture](https://pypi.org/project/openadapt-capture/)** — the
   cross-platform GUI capture component. It records the operator's
   mouse/keyboard input stream time-aligned with an action-gated screen video
   into a *capture session* directory. This is the extensively-tested capture
   stack; the recorder wires it in via `openadapt_capture.Recorder` (a context
   manager: records on enter, stops + flushes on exit).

2. **The capture adapter**
   (`openadapt_flow.adapters.capture.convert_capture`) — converts that capture
   session into the **exact** recording format the compiler consumes
   (`meta.json` + `events.jsonl` + `frames/{i:04d}_before.png` / `_after.png`),
   running openadapt-capture's own event-processing pipeline (raw streams →
   merged clicks / typed text). This adapter is unit-tested end to end against a
   real capture session in `tests/test_capture_adapter.py`, and those tests run
   on default CI: the fast unit `test` job installs the `capture` extra
   (openadapt-capture >=0.5.4 imports clean headless, so no display is needed).

The genuinely new piece (`openadapt_flow/desktop_record.py`,
`record_desktop_capture`) is the **live orchestration**: start a capture
session, let the operator perform the workflow, stop on `Ctrl-C`, then convert
to a compile-ready recording. It is dependency-injectable (the recorder and the
converter are parameters), so the orchestration is unit-tested without a live
display; the capture and conversion are tested in their own suites.

openadapt-capture is the optional `capture` extra:

```
pip install 'openadapt-flow[capture]'
```

## Recording paths

| Capability | Status |
|---|---|
| Capture the operator's desktop demonstration (mouse/keyboard + screen) | **Capture path** (openadapt-capture) |
| Convert to the compile-ready recording format | **CI-backed** (`convert_capture`) |
| Compile and replay through desktop backends | **CI-backed**, with scoped live Windows UIA and RDP qualifications |
| Parameters (typed value → replay-time override, `--param NAME=VALUE`) | **Available** |
| Structural UIA locators on click steps | **Live Windows observer path** |
| Secret handling | **Fail-closed**: masked/password controls or a declared deployment redaction policy |
| RDP coordinate binding | **Same-space capture** at the target resolution; client-window remap is deployment-calibrated |

### Structural UIA evidence

Offline capture records mouse/keyboard/video **only** — there is no live
accessibility tree at conversion time to read an element identity from. So every
`anchor.structural` is `None` and replay resolves on the **visual ladder**
(template → OCR → geometry). The bundle is fully valid and replays; it simply
lacks the deterministic *structural* top rung that a DOM-armed web bundle
(`dom_arm`) or a live UIA-armed desktop recording carries.

The deterministic structural rung is armed by the **live-over-`WindowsBackend`**
path (`openadapt_flow.adapters.desktop_recorder.record_desktop_demo`), which
queries `WindowsBackend.structural_locator_at` at each click — but that path
uses a driver that can query the target under the click. Capture-converted
recordings without that observer remain valid and use the visual ladder. A
deployment that requires UIA identity records through the live observer or
re-arms the fixed workflow against the qualified application before release.

### Secret handling

The browser recorder blacks out a secret field's pixels using the field's DOM
rectangle. A pixel/desktop capture has **no field geometry**, so it cannot
redact the typed value from the captured frames. Rather than persist an
unredacted secret frame, desktop `record` **refuses** `--secret`
on an unqualified pixel capture. Use a masked/password control or a deployment
recorder with reviewed field geometry and fail-closed redaction.

### RDP coordinate space

openadapt-capture records the machine it runs on. For `rdp` / Citrix where the
remote desktop is painted into a **client window** on the operator's host, a
host-screen capture is in host-screen pixel space, while the `rdp` backend
replays in the remote **framebuffer** space — these can differ. The supported
base path records in the **same pixel space the backend replays in**: run
capture inside the remote session, or bind the client window to the target
resolution and calibrate its mapping as part of workflow qualification.
`--backend rdp` records identically to `--backend windows`; the flag selects
intent and replay wiring.

### Window-scoped capture (`--window`)

Instead of full-screen capture, scope the recording to **ONE window, recorded in
that window's own pixel space** — closing the coordinate-space gap above at the
source. Select the target by owner-app substring (and optionally a title
substring to disambiguate):

```
openadapt-flow record --backend rdp --window Parallels --out rec/
openadapt-flow record --backend citrix \
    --window 'Citrix Viewer' \
    --rdp-window 'Citrix Viewer' \
    --rdp-window-title 'Ward A' \
    --rdp-readiness-text 'Appointments' \
    --out rec/
```

Selectors are case-insensitive substrings matching openadapt-capture's
`WindowTarget` (`--window` → owner app, `--window-title` → window title); the
largest matching visible window wins. Every frame is that window's own pixels
and input coordinates are translated into the same space at capture time, so a
demonstration recorded here is already in the pixel space the `rdp` backend
replays in (`CaptureSession.window_capture`, `coordinate_space:
window_pixels`). The capture adapter stamps the window identity into
`meta.json` under `window_capture` (target + resolved owner/title, plus the
resolved `resolved_pid` / `resolved_window_id` OS handle where available) and
emits closed `backend_hints` (`backend`, `rdp_window`, `rdp_window_title`, and
optional `rdp_readiness_text`) so compile preserves the target and an unflagged
replay resolves the same client window. Capture selectors are intentionally
substring-based; replay selectors are exact. Use `--window` to find the window
during recording and `--rdp-window` / `--rdp-window-title` to pin the exact
replay identity. The resolved exact owner/title is used when the explicit replay
selector is omitted.

The Citrix defaults are `Citrix Viewer` on macOS and the exact process basename
`wfica32` on Windows (`.exe` is optional). `CDViewer` remains an explicit
alternate on Windows; the driver never cycles through candidates or accepts the
first partial match. Duplicate exact matches require a title or other explicit
disambiguation and otherwise refuse.

```bash
openadapt-flow compile rec/ --out bundle/ --name ward-a
# Uses the recorded Citrix target and readiness marker.
openadapt-flow replay bundle/
# Explicit config/CLI remains authoritative.
openadapt-flow replay bundle/ --backend citrix \
    --rdp-window wfica32 --rdp-window-title 'Ward A'
```

Governed `run` requires a current-frame readiness marker for Citrix. Record it
with `--rdp-readiness-text`, set `backend.rdp_readiness_text` in deployment
config, or pass the flag directly. If it is absent, `run` refuses before any
action; ordinary record/replay remains available.

Window-scoped capture is implemented on **macOS and Windows** hosts
(`CGWindowListCreateImage` / Win32 region grab); on any other host `--window`
is refused up front rather than silently falling back to full-screen (which
would record coordinates in the wrong pixel space). `--window` applies only to
the desktop backends — `--backend web` records the Playwright page and refuses
it.

**PHI note:** a window title or readiness marker can contain a patient name.
These values remain local execution metadata: plaintext in `meta.json` and in an
explicitly unencrypted local bundle, encrypted inside `workflow.json.enc` when
the bundle is sealed, and subject to the existing sanitized-derivative review
before egress. They are never copied into `manifest.json`, hosted run summaries,
or console logs. Command-line values are still visible in process listings and
shell history: use `--rdp-window-title` / `--rdp-readiness-text` only for stable,
non-sensitive application chrome. Put sensitive deployment values in a
permission-protected YAML config, or record them once and seal the compiled
bundle rather than repeating them on the command line.

## Parameters

Desktop has no field identity (no DOM `name`/`id`), so a parameter is keyed by
its **demonstrated value**: `--param NAME=VALUE`. A typed value equal to `VALUE`
is marked as parameter `NAME`; its demonstrated value becomes the default,
overridable at replay with `--param NAME=<new value>`. (This mirrors
`convert_capture`'s `params` contract and the replay `--param` contract.)

## Marking the record-identifying region (`--identifier`)

The compiler automatically emits a **pixel identifier crop**
(`anchor.identifier_crop`, stored under `templates/identifiers/` so it is
sealed with the other image crops) for every identity-armed click that
captured no structured identity — exactly the pixel-recording case — from the
OCR identity band. That crop arms the pixel-compare identity tier
(MISMATCH-or-ABSTAIN: it can add a safe halt on a wrong MRN, never authorize
a match) on remote-display replays.

To scope the crop to the operator-designated identifying region instead (the
patient banner / MRN cell), mark it once per recording:
`record --backend rdp --identifier X,Y,W,H` (recording pixels; a pixel
capture has no field identity, so the region is given literally — on
`--backend web` the same flag takes a field `name`/`id`). Steps that compile
without a crop record why in `Step.identifier_crop_missing_reason`, and
`lint` reports per-bundle pixel-identity coverage
(`missing_identifier_crop`).

## Where recording happens

`record --backend windows` captures the desktop **the recorder process runs
on**. In the product deployment the operator runs `openadapt-flow record` on the
target Windows desktop (or inside the remote session) and performs the workflow;
replay then drives the same substrate via the in-guest agent
(`replay --backend windows --agent-url …`). Record on the box, replay through the
agent.
