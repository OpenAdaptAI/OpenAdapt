# Structural action rung — availability under render drift

Reproduces, on a real rendered DOM with the real resolution ladder, the desktop
benchmark that reframed the thesis from **"vision-only"** to **"deterministic
compiled automation with visual FALLBACK"**: structural (DOM/UIA) execution
scored **21/21** while compiled *visual* replay scored **6/21** under render
drift.

## What it measures

`structural_action_probe.py` (logic in
`openadapt_flow/validation/structural_action.py`) renders a dense surface of
`n` actionable targets, each with a stable DOM id (`#open-pK`) — what a browser
recorder captures for the structural rung — plus a recorded visual anchor
(template crop + OCR label). It then RE-RENDERS with drift: a deterministic
subset is left untouched (steps whose surface did not change), the rest are
MOVED, RELABELLED (`Open`→`View`) and RE-THEMED (dark, serif). The DOM id never
changes. For each target it asks, on the drifted surface:

- **structural** — `backend.locate_structural(locator)`: does it land inside the
  CORRECT element's box? (Deterministic, pixel-independent.)
- **visual** — `resolve(anchor, drift_png, vision, structural=None)`: does the
  template/OCR/geometry ladder resolve to a point inside the CORRECT box? A
  drifted target's recorded crop no longer matches at its old position and
  either fails or matches a look-alike sibling — both are failures.

Numbers are MEASURED, never hardcoded.

## Result (`structural_action.json`, n=21)

```
structural (DOM) rung : 21/21 acted correctly
visual ladder only    :  6/21 acted correctly
under drift (15 targets): structural 15/15 vs visual 0/15
```

Structural resolves every target whose id is still in the DOM; the visual ladder
resolves only the non-drifted minority. The structural point flows through the
SAME click path, so the pre-click identity gate and the irreversible risk gate
still fire on it — structure makes identity STRONGER (an exact element), it
never bypasses it.

## Run it

```
python benchmark/structural_action/structural_action_probe.py 21
```

The in-suite assertion of the same win (structural resolves all, beats visual
under drift, identity gate intact) lives in `tests/test_structural_rung.py`; the
full record→compile→replay default-on path is in
`tests/e2e/test_structural_action.py`.
