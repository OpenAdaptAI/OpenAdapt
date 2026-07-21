# No-DOM canvas vision-ladder qualification

This directory holds a **no-DOM HTML5-canvas** end-to-end qualification of the
vision-only resolution ladder — the live proof for the pixel path on the
**class of surface Citrix Workspace-*web* presents**: a remote session painted
into an HTML5 `<canvas>` over a WebSocket, with **no accessible content inside
the canvas**.

## Honest label (read this first)

**This is the no-DOM-HTML5-canvas class — it is NOT Citrix ICA/HDX.** It
reproduces the canvas surface class (server-side rendering, compression,
scaling, latency, and — the load-bearing part — *no accessible DOM inside the
canvas*), but it uses **VNC/noVNC**, not Citrix's proprietary ICA/HDX protocol.
It has **no HDX codecs, no ICA compression, and no Citrix-Workspace-client input
path**. Genuine ICA/HDX evidence requires a Citrix entitlement (a CVAD/DaaS
trial or a design partner); the procurement + runbook are in
`~/oa/src/.private/rdp_citrix_validation_2026_07_20.md`. We do not fake Citrix
evidence.

| Proof | What it exercises | Substrate |
| --- | --- | --- |
| `benchmark/rdp` (PR #142) | RDP **transport + input** (aardwolf) | real Windows RDP (Parallels) |
| `benchmark/rdp_ladder` (PR #177) | vision ladder over real **RDP** pixels | FreeRDP3 round-trip |
| `benchmark/windows_uia`, `benchmark/macos_native` | **structural** rungs (UIA / AX) | native a11y |
| **`benchmark/canvas_ladder` (here)** | **vision-only ladder + contract** over a **no-DOM HTML5 canvas** (the Citrix Workspace-web class), effect verification, drift behavior | noVNC/TigerVNC canvas, no a11y |

## What it proves

`run_canvas_ladder_qualification.py` drives the **unmodified** `Recorder` →
`compile_recording` → `Replayer` over a genuine no-DOM `<canvas>`, with **no
structural backend**, and asserts the validation contract across three regimes
(`results.json`, `schema_version: openadapt.canvas-ladder-qualification.v1`,
`accepted` gate):

1. **healthy** — record → compile → replay a patient-note write **succeeds**,
   with **zero model calls**, resolution through the **visual rungs only** (the
   structural rung is never used — the canvas has no a11y tree by construction),
   and the write **independently confirmed** by a document oracle (the note the
   kiosk persisted equals the intended value).
2. **moderate drift** — a laggy-but-**legible** degraded frame (DPI downscale +
   theme-inversion + JPEG). The invariant proven here is **never a silent WRONG
   write**: the ladder resolves to the **correct** target (via OCR + geometry)
   and writes the **correct** value. This is deliberately *not* an "always
   halt" assertion — over-halting on a still-readable frame would be a useless
   false refusal.
3. **severe drift** — a genuinely **illegible** frame (heavy downscale +
   Gaussian blur + inversion + hard JPEG; the roster/MRN are unreadable). The
   ladder finds **no confident target and HALTS** — no write, no model call. It
   does **not** blind-fire the recorded pixel coordinates the way a naive
   record/replay tool would. This is the safe-halt / anti-silent-wrong-action
   guarantee the product is sold on.

Together (2)+(3) prove the real property: **under remote-display drift the
system either resolves correctly or halts — it never silently writes the wrong
thing.** (1) proves it is not merely always-halting.

## Why a canvas (and why it is pixel-only by construction)

Over Citrix Workspace-web the user reads an HTML5 `<canvas>` the remote session
paints; UIA/MSAA/DOM do not cross into it. This fixture reproduces that: the
kiosk runs on a TigerVNC display, noVNC renders that framebuffer into a
`<canvas>`, and the harness drives the `<canvas>` with a browser backend
(`CanvasBrowserBackend`, Playwright). That backend implements **only** the base
`Backend` protocol — not `StructuralBackend` / `IdentityBackend` /
`StructuralActionBackend` — so the resolver's structural rung is unavailable and
resolution runs on the visual floor (template / template_global / ocr /
geometry); identity would fall back to the OCR name+DOB tier. Screenshots are
the canvas pixels; clicks/keys are injected at canvas-relative pixel
coordinates that noVNC forwards over the VNC wire to the remote session.

## Honest scope

Real no-DOM canvas pixels + a real VNC-transported remote session, the real
resolver ladder and the $0 / identity / effect gates. **NOT** Citrix ICA/HDX,
**NOT** an aardwolf/RDP transport proof (that is `benchmark/rdp` /
`benchmark/rdp_ladder`), and the drift is **simulated-on-a-real-session** (not
WAN/HDX capture).

## Runbook

```bash
# 1. Build the fixture image (multi-arch: amd64 CI + arm64 Apple Silicon)
docker build -t oaflow-canvas-fixture:latest benchmark/canvas_ladder/fixture

# 2. Start the no-DOM canvas surface (self-contained; publishes noVNC on :6080)
docker run -d --name oaflow-canvas-ladder --shm-size=1g -p 6080:6080 \
    oaflow-canvas-fixture:latest
sleep 15   # let TigerVNC + kiosk + noVNC come up

# 3. Run the qualification (needs the flow stack + playwright + cv2 + rapidocr)
python3 benchmark/canvas_ladder/run_canvas_ladder_qualification.py \
    --container oaflow-canvas-ladder \
    --output benchmark/canvas_ladder/results.json \
    --candidate-commit "$(git rev-parse HEAD)"

# 4. Tear down
docker rm -f oaflow-canvas-ladder
```

Exit code `0` iff `accepted` is true. The env-gated pytest wrapper
`tests/e2e/test_canvas_nodom_ladder_e2e.py` (`OAFLOW_CANVAS_LADDER_E2E=1`)
builds the fixture, runs the qualification, and asserts `accepted`. A nightly CI
job is drafted in `.github/workflows/canvas-nodom-ladder.yml`.

## License posture

The fixture image apt-installs noVNC (MPL-2.0), websockify (LGPL-3) and TigerVNC
(GPL-2.0) as **external applications run at test time** inside a repository-only
image; nothing here is vendored into or shipped by the `openadapt-flow`
wheel/sdist (`benchmark/` is excluded from the package). This is consistent with
`AGENTS.md` "License Hygiene and Package Boundaries" (running/automating an
external app is not redistributing its source).
