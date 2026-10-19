# Display scaling and ClearType on the pixel path

This sweep measures what happens when a remote desktop draws a recorded
workflow at a different display scale or with ClearType switched, and nothing
else changes. It covers the surfaces that have only the pixel rungs (template,
OCR, geometry): RDP, Citrix, and a canvas with no page structure.

Each kiosk is recorded once at 100% scaling with ClearType on, compiled, and
replayed at 100%, 125%, 150% and 175%, with ClearType on and off. Each point
runs twice:

- **healthy**: the right record stays selected. A correct run saves exactly one
  row for Ada Lovelace with the replay values.
- **wrong record**: after the last value is typed and before Save, the active
  record switches to Grace Hopper. A correct run stops with nothing saved.

A negative control enlarges the fonts by 25% at 100% scaling without moving the
layout. That is a font change, not a display-scale change, and a correct run
stops.

No model is called in any run.

## Result

| | Before | After |
|---|---|---|
| Healthy runs that finished | 3 of 24 | 17 of 24 |
| Wrong-record runs that stopped with nothing saved | 24 of 24 | 24 of 24 |
| Font-only controls that stopped | 6 of 6 | 6 of 6 |
| Wrong writes, false completions, crashes | 0 | 0 |

Before, every scaled run stopped at its first step on the layout check, and
switching ClearType off at 100% stopped every kiosk at the identity check.

After, by kiosk:

| Kiosk | Record IDs | Healthy runs finished, before | After |
|---|---|---|---|
| `canvas_notes` (canvas fixture layout) | `MRN 24682` | 1 of 8 | 8 of 8 |
| `rdp_appointments_unambiguous_ids` (RDP fixture layout) | `MRN A3467` | 1 of 8 | 8 of 8 |
| `rdp_appointments` (RDP fixture layout) | `MRN A1001` | 1 of 8 | 1 of 8 |

Every scaled run fitted its scale from 9 to 11 recorded labels, with the
largest label off its fitted position by 7.8 px, and snapped to the exact
frame-size ratio (1.25, 1.5 or 1.75). In every wrong-record run the identity
check stopped the run before Save, or before the first field.

The appointment kiosk with the fixture's own record IDs (`MRN A1001`,
`MRN B2002`) still stops at every render except the recorded one. Those IDs
rest on glyphs OCR confuses (1 and l, 0 and O). At record time OCR read the
active-record banner as `MRN Alo01`. Only an exact pixel match against the
recording confirmed the record, and a pixel match is only valid on the
recorded render. At any other scale, or with ClearType off, the identity check
cannot confirm the record, so the run stops before the first field. The same
kiosk with IDs free of those glyphs (`MRN A3467`) is the
`rdp_appointments_unambiguous_ids` row.

Per-point results, with the step and check that stopped each run and the rung
each step resolved through, are in [`results_before.json`](results_before.json)
and [`results_after.json`](results_after.json). With the layout check switched
off ([`results_before_no_drift_check.json`](results_before_no_drift_check.json)),
the old code found step 0's target by OCR and then stopped there: on the
step's postcondition, which compares the recorded crop at the recorded size
(canvas kiosk), or on the identity check (both appointment kiosks).

## What changed

1. **One scale estimate per run.** The compiler now stores, for each click
   step, the frame's unique text lines as salted hashes with their centres and
   sizes, and no text. At the first step the frame differs from the recorded
   fingerprint, the runtime reads the live frame once, pairs each recorded label
   with the one live line that reads the same, and fits
   `live = scale x recorded + offset`. The fit needs at least 3 agreeing labels
   spanning 15% of the frame in each axis, both axes scaling alike, and the
   recorded frame inside the live frame. When the fit agrees with the ratio of
   live to recorded frame size within 1%, it snaps to that ratio.
2. **The run continues in recorded geometry.** The backend is wrapped in a view
   that resamples each frame to the recorded size and maps every coordinate
   back to the session. It exposes only the backend methods whose coordinates
   it can map. Guarded clicks still go through the backend's exact-frame check.
   Template, OCR and geometry resolution, the identity check, postconditions
   and effect checks run unchanged on the resampled frame.
3. **OCR reads the session's own pixels.** Resampling merges the gaps between
   words (`Activerecord:AdaLovelace`), and the identity check reads words. OCR
   of a resampled frame reads the live frame and maps each line back.
4. **The layout check compares labels with themselves.** On a scaled run, each
   step checks that its recorded labels are where they were recorded and as
   wide as they were recorded. OCR box heights move in steps of 2 to 3 px
   between renders of the same text, about 10%, so a whole-frame median height
   falsely reported a font change. Widths of the same text stayed within 1%.
5. **OCR labels tolerate font changes.** A different rasterizer can make OCR
   read `MRN A1001` as `MRN Al001`. When the exact label is not found, the OCR
   rung compares labels under OCR's confusable-glyph classes, only inside the
   recorded target region, and only when exactly one line there qualifies. The
   identity check still decides whether it is the right record.
6. **The pixel identity check abstains on another rasterizer.** Its thresholds
   were set on renders from one rasterizer. A ClearType switch changes every
   glyph edge and produced glyph-sized differences it reported as a different
   record. It now abstains when the two crops were rasterized differently, and
   on scaled runs, and the OCR identity check decides. An identifier OCR cannot
   confirm still stops the run.

## What this does and does not show

- The kiosks are in-process re-renderings of the layouts, state and saves of
  [`benchmark/rdp_ladder/fixture/kiosk_app.py`](../rdp_ladder/fixture/kiosk_app.py)
  and [`benchmark/canvas_ladder/fixture/kiosk_app.py`](../canvas_ladder/fixture/kiosk_app.py)
  ([`kiosk_render.py`](kiosk_render.py)). They are drawn with DejaVu Sans
  through FreeType at the scaled pixel size, the way a DPI-aware application
  redraws. The frames do not cross FreeRDP or noVNC, and they are not Tk's own
  rendering. ClearType on is modelled as 3x horizontal subpixel rendering with
  a five-tap filter; off is one-bit aliased text.
- Display scaling is modelled as a whole-session change: the frame grows with
  the scale and the application redraws at the new size. A layout that reflows
  at a new scale, rather than growing, is not a display-scale change and still
  stops.
- A pinned FreeRDP session profile (scale, DPI, font smoothing) still stops on
  a profile change.
- Browser zoom on the web path is unchanged; see
  [`benchmark/cosmetic_drift`](../cosmetic_drift/COSMETIC_DRIFT.md).
- This is not a live Citrix ICA/HDX session.

## Reproduce

```bash
python benchmark/pixel_scale/run_pixel_scale_sweep.py --output results_after.json
python benchmark/pixel_scale/compare.py \
  benchmark/pixel_scale/results_before.json results_after.json
```

`results_before.json` and `results_before_no_drift_check.json` were produced
by the same harness on `origin/main` before this change (commit in each
file). `results_after.json` was produced on this branch before it was
committed. `--no-layout-drift` turns the layout check
off to show the next check that stops a run. A full sweep takes about 15
minutes on an Apple-silicon laptop. Results can differ slightly with the OCR
engine version and the installed DejaVu Sans.
