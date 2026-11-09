# Citrix Workspace-window pixel backend

Part 2 of the no-DOM/Citrix validation. A backend that drives a **Citrix
Workspace/Viewer session WINDOW as a no-DOM
pixel surface**: window-scoped screen capture + OS-level input injection into the
Workspace window, **pixel-only** (no structural/a11y), conforming to the base
`Backend` contract so the resolver ladder + effect verification run over it
UNCHANGED.

## What this is (and what already existed)

The window-scoped-capture + OS-input mechanism is
`openadapt_flow.backends.remote_display.RemoteDisplayBackend` (merged, PR #106):
`CGWindowListCreateImage` / `PrintWindow` capture by window id + `CGEvent` /
`SendInput` injection, per-monitor-v2 DPI, and fail-loud safety gates
(frame-freshness lease, occlusion guard, DPI-consistency, input-trust). It is
target-window-agnostic. This deliverable adds the thin **Citrix preset** over it:

- `openadapt_flow/backends/citrix_workspace.py`
  - `CITRIX_WINDOW_OWNERS` — per-platform Workspace/Viewer window owner names.
  - `default_citrix_owner()` — pick the host's Citrix owner.
  - `CitrixWorkspaceBackend` — a `RemoteDisplayBackend` that defaults its target
    owner to the host's Citrix Workspace window and carries the ICA scope note.
    Pixel-only by construction (base `Backend` only; NOT `StructuralBackend` /
    `IdentityBackend` / `StructuralActionBackend`), so the structural rung is
    unavailable — the ICA floor.
- Factory: `--backend citrix` now builds `CitrixWorkspaceBackend` with the
  default exact owner (`Citrix Viewer` on macOS; process basename `wfica32` on
  Windows). `--rdp-window` overrides it and `--rdp-window-title` disambiguates
  multiple exact matches. `CDViewer` is an explicit Windows alternate, never a
  silent fallback.

The product CLI closes the binding from demonstration to execution:

```bash
openadapt-flow record --backend citrix \
  --window 'Citrix Viewer' \
  --rdp-window 'Citrix Viewer' \
  --rdp-window-title 'Ward A' \
  --rdp-readiness-text 'Appointments' \
  --out rec/
openadapt-flow compile rec/ --out bundle/ --name ward-a
openadapt-flow replay bundle/
openadapt-flow run bundle/ --approve-unverified-effects
```

`--window` / `--window-title` are substring selectors used only to find the
capture window. `--rdp-window` / `--rdp-window-title` are exact replay
selectors. Compile carries the recorded Citrix kind, target, and readiness
marker in closed local workflow hints; config and CLI overrides remain
authoritative. Governed `run` refuses before action if a Citrix readiness marker
is absent, while record and replay remain usable. Owner/title/readiness values
can be sensitive: they are not copied to the plaintext manifest, hosted
summary, or console output, and they are encrypted when the workflow bundle is
sealed. An explicitly unencrypted local bundle remains plaintext by design.

## Evidence scope

**Synthetic code-readiness evidence:**
- The backend records → compiles → replays through the vision-only ladder and
  **safe-halts under drift**, exercising the `RemoteDisplayBackend` code
  contract (window resolution, per-frame capture + scale, pixel→window-point
  map, frame-freshness lease, occlusion guard, input-trust gate) over a genuine
  no-DOM surface. Evidence: `results.json`
  (`schema_version: openadapt.citrix-workspace-code-readiness.v2`):
  - 3 healthy record→compile→replay trials succeed with **0 model calls**,
    **visual rungs only**, and writes independently confirmed by the document
    oracle;
  - 3 severe-drift trials **halt** with no write and no model call;
  - silent incorrect success, false completion, and healthy over-halt are
    reported explicitly.
- Unit contract: `tests/test_citrix_workspace_backend.py` (owner presets,
  pixel-only protocol surface, factory wiring).

This supports `code_readiness_accepted:true` for the synthetic stand-in. It does
not support Citrix ICA/HDX acceptance: `ica_hdx_accepted:false`.

**Real ICA/HDX acceptance gate:**
- Validation against a **REAL Citrix Workspace window** on a **live ICA/HDX
  session**. The ICA-specific delta the canvas stand-in cannot cover: HDX codec
  artifacts, ICA compression, and the real Workspace-client input path.
- Confirming the exact **owner/title** string on the target Workspace build and
  a **session-lock readiness marker**.

## How it was validated now: the no-DOM canvas STAND-IN

`run_citrix_workspace_qualification.py` drives the **unmodified**
`CitrixWorkspaceBackend` over the Part-1 no-DOM canvas fixture
(`benchmark/canvas_ladder/fixture`) by swapping only the backend's `WindowClient`
seam for a `CanvasWindowClient` that captures the noVNC `<canvas>` and injects
into it (Playwright). This exercises the backend contract over the **no-DOM
HTML5-canvas class** — **NOT** the host-native capture/input implementation or
Citrix ICA/HDX.

```bash
# Reuses the Part-1 fixture container (benchmark/canvas_ladder):
docker build -t oaflow-canvas-fixture:latest benchmark/canvas_ladder/fixture
docker run -d --name oaflow-canvas-ladder --shm-size=1g -p 6080:6080 \
    oaflow-canvas-fixture:latest
sleep 15
python3 benchmark/citrix_workspace/run_citrix_workspace_qualification.py \
    --container oaflow-canvas-ladder \
    --output benchmark/citrix_workspace/results.json \
    --candidate-commit "$(git rev-parse HEAD)" \
    --base-commit "$(git merge-base HEAD origin/main)"
docker rm -f oaflow-canvas-ladder
```

Env-gated e2e: `tests/e2e/test_citrix_workspace_standin_e2e.py`
(`OAFLOW_CITRIX_STANDIN_E2E=1`). Path-filtered pull-request and manual CI:
`.github/workflows/citrix-workspace-standin.yml`.

## Real ICA/HDX release gate

Once a synthetic lab application is reachable via Citrix Workspace on a
controlled GUI host:

1. **Find the exact window owner/title.** On the host, enumerate on-screen
   windows and confirm the Workspace session window's owner (macOS: usually
   `Citrix Viewer`; Windows: process basename `wfica32`, with `CDViewer` as an
   explicit alternate). Set
   `--rdp-window <owner>` if it differs from the preset, `--rdp-window-title` to
   disambiguate.
2. **Grant input/capture trust** to the driver process (macOS: Screen Recording
   + Accessibility; Windows: run the driver at the target window's integrity
   level). The backend **fails loud** if trust is missing — a dropped click can
   never look like success.
3. **Set a session readiness marker** (`readiness_text` / `--rdp-readiness-text`)
   — a stable in-app word — so a lock/login/disconnect frame is refused.
4. **Swap the client:** construct `CitrixWorkspaceBackend()` with **no**
   `client` (the host's native Mac/Win `WindowClient` is used automatically), or
   `--backend citrix` from the CLI. Everything else — the ladder, identity,
   effect, drift-halt — is identical to the stand-in harness.
5. **Run the release contract at least 3+3 times:** three healthy
   record→compile→replay trials and three drift/refusal trials on one exact
   client/server/application version matrix, with an independent synthetic-lab
   effect oracle. Acceptance requires zero silent incorrect success, explicit
   refusal under unresolved drift, and recorded failure taxonomy. Keep raw
   screenshots, window fingerprints, detailed configuration, and per-system
   recipes inside the private evidence boundary; publish only a reviewed,
   bounded aggregate.

## Honest scope / label

This proves the **Citrix backend contract + ladder + effect + safe-halt** over a
**no-DOM HTML5-canvas STAND-IN** (the class Citrix Workspace-web presents). It is
**NOT Citrix ICA/HDX**. The real-ICA gate remains the 3+3 release contract above.
