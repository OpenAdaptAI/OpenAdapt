# Desktop Phase 2 — Limits and caveats

Read the numbers in `benchmark/desktop/BENCHMARK.md` against these. Every one
was pre-committed in the spike spec; publishing regardless is the point.

## 1. ARM + x64-emulation rendering is not native x86

The guest is Windows 11 **ARM**; the host is Apple Silicon. Text/glyph
rendering under this stack is **not byte-identical** to native x86 Windows.
The compiled-replay arm is pixel-template + OCR based, so its absolute numbers
are specific to this rendering substrate. The **mechanism** (record → compile →
replay with identity verification, DB-judged) is what transfers; a native-x86
confirmation run (cloud spot VM with a persisted WAA image) is future work.

## 2. Render-scale is a proxy for DPI, not a real DPI change

The spec asks for DPI 100/125/150 %. A real per-monitor DPI change on Windows
requires a **session sign-out/in**, which is not no-touch automatable in this
harness. Instead the app scales its **base font** (`font_scale` 1.0/1.25/1.5),
reproducing the same *class* of rendering shift — larger glyphs, moved targets,
different anti-aliasing — that defeats pixel-template matching, which is the
effect under test. It is labelled `render_125` / `render_150`, not `dpi_*`.
Note also that the WinForms app runs **DPI-unaware** (bitmap-scaled by the
compositor), so a true system-DPI change would partly bitmap-rescale uniformly;
the font-scale proxy is arguably a *harder* test of layout robustness. A real
DPI-aware, sign-out-driven DPI sweep is future work.

## 3. WinForms substitute for OpenDental

The benchmarked app is `patient_notes.ps1` (WinForms + SQLite), **not**
OpenDental — the OpenDental trial is not no-touch installable (SmartScreen +
UAC secure-desktop + interactive wizard; see `PHASE2.md`). The substitute
preserves the properties that matter: a real WinForms UIA tree (with the
same partial/broken row a11y), a list-select → edit → save workflow, and exact
SQL ground truth. It does **not** reproduce OpenDental's specific visual
density or its MariaDB schema. The clinical-app confirmation is future work.

## 4. Window-resize drift is deferred

The app runs **maximized** so the captured frame is entirely app content (no
background-window bleed into identity bands / postconditions). A non-maximized
window re-introduces desktop bleed that destabilises band OCR. A clean
window-resize condition (solid-colour desktop, closed shell windows) is future
work; the render-scale and theme conditions already exercise layout/appearance
shift.

## 5. Modest N — this is an existence result

N is small per cell (a first desktop existence result, not a big-N study). The
outcome *categories* (success / wrong-action / safe-halt / false-abort) and the
identity-vs-positional contrast are the signal; the rates carry wide error
bars. Scaling N and adding native-x86 + Mode-B (RDP/VNC pixel-stream) runs is
future work.

## 6. Identity band coverage is app-specific

Armed coverage (which click steps carry an identity band) depends on how much
stable text sits around each target in *this* app. The headline number is a
per-app measurement, not a universal constant; it is reported, not asserted.
