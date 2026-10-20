# Hover response before an icon-only click

An icon-only click on a pixel surface carries no text that says which control
it is, so the right-record check had nothing to compare. This benchmark
measures what the hover look
([`runtime/hover_evidence.py`](../../openadapt_flow/runtime/hover_evidence.py))
adds on a kiosk where that matters.

## Setup

The appointment kiosk of [`benchmark/pixel_scale`](../pixel_scale/README.md),
rendered in process, with its Save button turned into a toolbar icon. A second
icon, `Clear form`, draws the same glyph; the two differ only by their tooltip.
The kiosk draws a button under the pointer in its active colour, as Tk does,
and shows the icon's tooltip. Record IDs are free of look-alike glyphs, so the
identity check of the record is not what stops a run here.

The kiosk is recorded once at 100% scaling with ClearType on, with the
production Recorder, compiled, and replayed:

- **recorded layout**: a correct run saves one row for Ada Lovelace;
- **swapped layout**: the two icons trade places, as a toolbar reorder between
  versions would. At the recorded Save position there is now the identical
  Clear icon. A correct run does not click it.

Each at 100% ClearType on (same render), 100% ClearType off (another
rasterizer) and 125% display scaling. No model is called.

Before is origin/main at `426d08f2d`, run with this script and the extended
`kiosk_render.py` copied into that checkout; its Recorder cannot capture hover
frames, so it records without them. After is `283336358`, the hover change
with the extra identity looks merged in.

## Result

| | Before | After |
| --- | --- | --- |
| Click steps with identity evidence | 1 of 5 | 2 of 5 |
| Swapped layout: runs that clicked the look-alike Clear icon | 2 of 3 | 0 of 3 |
| Swapped layout: runs that stopped before clicking it | 1 of 3 | 3 of 3 |
| Recorded layout: runs that finished | 2 of 3 | 2 of 3 |
| Wrong writes, model calls | 0, 0 | 0, 0 |

After, the Save icon's hover response (its highlight and the
`Save appointment` tooltip) is bound to the step. On the swapped layout the
icon at the recorded spot answered with `Clear form`, a box of another size,
and the run stopped before the click. With ClearType off the pixels differ but
the tooltip text is the same, and the run finished.

Before, both swapped runs at 100% clicked Clear, which emptied the form; each
run then stopped after the click, when the expected screen did not appear.
Nothing was saved, but the wrong control had been pressed.

At 125% scaling both versions stop at the Save step before any hover look: the
icon resolves only through the geometry rung, and an irreversible step does
not act on a geometry-only resolution. That swapped run is the one "stopped
before clicking" run before, and one of the three after; that stop does not
come from recognising the wrong icon.

The other click steps get no hover evidence: the roster row is drawn white, so
its active colour shows no change, and text fields do not answer the pointer.
Only the click into the first field has identity evidence from text on its
row. Per-step
coverage is in each results file under `coverage.steps`.

Raw results: [`results_before.json`](results_before.json),
[`results_after.json`](results_after.json).

## Limits

- An in-process rendering, not a live RDP or Citrix session. Tk-style hover
  colour and a tooltip are modelled; real applications differ.
- Hover works on the browser, remote-display and FreeRDP backends. The Aardwolf
  RDP transport and the native Windows, macOS and Linux backends take no hover
  look yet.
- A tooltip that opens later than the 0.8 s wait looks like a different
  response and stops the run.

## Reproduce

```bash
python benchmark/hover_identity/run_hover_sweep.py --output results_after.json
```
