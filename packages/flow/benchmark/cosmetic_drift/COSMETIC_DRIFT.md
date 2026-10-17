# Cosmetic-drift operating envelope

**What breaks deterministic replay when only the _rendering_ changes** —
browser zoom, screen DPI (device pixel ratio), font size, and font family —
with the workflow itself untouched: the target is always present and
semantically identical, only its pixels move.

This study turns the scary, unqualified "0% at 125% zoom" into a bounded,
defensible spec. The headline is not the tight envelope — it is _how_ replay
leaves the envelope.

## TL;DR (operating spec a customer can be told)

> Compiled replay is calibrated to the exact render it was recorded on.
> It runs deterministically at **100% browser zoom, 1x display scaling, and
> the recorded font size**; a font-family change to another proportional face
> is absorbed automatically. **Any** deviation in render _scale_ — a
> different zoom level, a HiDPI/Retina display, or an OS font-size bump —
> stops the run at the first step with an accurate "expected screen not
> reached" report and saves nothing. It never acts on the wrong target: across
> the full zoom / DPI / font sweep there were **zero wrong-actions**. The
> failure mode is _availability_ (it halts and asks for a re-record or a heal
> pass), never _safety_ (a wrong write).

**Fails safe across the entire sweep: 0 wrong-actions / 0 crashes in 21
points.** Data: [`results.md`](results.md), [`results.json`](results.json).
Reproduce: `python benchmark/cosmetic_drift/sweep.py`.

## Method

- **One bundle, recorded once** against MockMed at 1280x800, dsf=1, 16px
  Arial (the canonical triage demo: sign in → open the first referral →
  New Encounter → Triage → note → Save). 11 steps; the correct outcome is
  always a save landing on `#patient/p1` (Jane Sample).
- **Cosmetic-only perturbation.** Zoom / font-size / font-family are applied
  as a `<head>` stylesheet injected after navigation (selector rules, so they
  survive MockMed's hash-router re-renders); DPI is `device_scale_factor` on
  the page. CSS `zoom` is the same model MockMed's own `drift=zoom` uses.
  Nothing changes the DOM's text or structure.
- **Ground truth from the live app.** After each run we read `location.hash`
  and the saved-encounter banner directly from the page — that is what
  distinguishes a _safe halt_ from a _wrong action the report never noticed_.
- **No model calls.** The grounder rung is never installed: resolution is
  template + OCR + geometry only (`ANTHROPIC_API_KEY` unset).
- Platform: macOS arm64, headless chromium, py3.12. Font-family results depend
  on installed faces and so are the only platform-sensitive rows.

Outcome vocabulary: **pass** (ran to completion, saved to the target),
**safe-halt** (stopped, saved nothing), **wrong-action** (saved to the wrong
patient — the dangerous class), **crash**.

## The envelope, per axis

| axis | passes at | first break tested | outcome outside envelope | where / why it halts |
|---|---|---|---|---|
| **Browser zoom** | 100% only | ±10% (`zoom 90%`, `110%`) | safe-halt | `step_000` `region_stable` postcondition |
| **DPI / device scale** | 1x only | 1.5x | safe-halt | `step_000` `region_stable` postcondition |
| **Font size** | recorded (16px) only | +10% | safe-halt | `step_000` `region_stable` postcondition |
| **Font family** | any proportional face | monospace | **pass** (serif) / safe-halt (monospace) | absorbed by OCR+heal; monospace trips `step_005` identity gate |

### Heatmap (scale × outcome × rung that carried step_000)

| perturbation | outcome | SAFE | step_000 rung | heals | rungs used |
|---|---|:--:|---|--:|---|
| baseline 100% / 1x / 16px | **pass** | ✅ | template | 0 | template:8 |
| zoom 80% | safe-halt | ✅ | geometry | 0 | — |
| zoom 90% | safe-halt | ✅ | geometry | 0 | — |
| zoom 110% | safe-halt | ✅ | geometry | 0 | — |
| zoom 125% | safe-halt | ✅ | geometry | 0 | — |
| zoom 133% | safe-halt | ✅ | geometry | 0 | — |
| zoom 150% | safe-halt | ✅ | geometry | 0 | — |
| zoom 175% | safe-halt | ✅ | geometry | 0 | — |
| zoom 200% | safe-halt | ✅ | geometry | 0 | — |
| DPI 1.5x | safe-halt | ✅ | geometry | 0 | — |
| DPI 2.0x | safe-halt | ✅ | geometry | 0 | — |
| DPI 3.0x | safe-halt | ✅ | geometry | 0 | — |
| font-size +10% | safe-halt | ✅ | geometry | 0 | — |
| font-size +19% (19px) | safe-halt | ✅ | geometry | 0 | — |
| font-size +37% | safe-halt | ✅ | geometry | 0 | — |
| font Georgia (serif) | **pass** | ✅ | geometry | 8 | geometry:3, ocr:5 |
| font Times New Roman (serif) | **pass** | ✅ | template | 5 | template:3, ocr:5 |
| font Courier New (monospace) | safe-halt | ✅ | template | 1 | template:2, ocr:1 |
| zoom 125% + DPI 2x | safe-halt | ✅ | geometry | 0 | — |
| zoom 133% + DPI 1.5x | safe-halt | ✅ | geometry | 0 | — |
| zoom 110% + font +19% + Georgia | safe-halt | ✅ | geometry | 0 | — |

## Precise break points

**Zoom, DPI, and font-size share a single binding constraint: the
`region_stable` postcondition on `step_000`.** step_000 is an _unlabeled_
click (a bare input field, no `ocr_text`). What happens under scale drift:

1. **Resolution ladder survives further than you'd expect.** The template
   rung's threshold (0.985) tolerates almost no resizing, so a scaled crop
   drops below it and — with no `ocr_text` to try the OCR rung — the ladder
   falls through to the **geometry** rung, which locates the field from its
   still-readable landmarks and produces a plausible click point. (The
   template scale ladder is `[0.85, 1.0, 1.18]`, i.e. it _could_ match a
   +18%/−15% resize, but 0.985 is strict enough that even 10% zoom misses.)
2. **The postcondition gate is what refuses.** After the click, `step_000`'s
   `region_stable` postcondition compares the live region to the recorded
   crop (a structural phash within tolerance 16, plus a template crop at 0.9).
   A scaled or reflowed render clears neither, so the run aborts with
   `postconditions_ok = False` — **before** proceeding to any later step.

So the operative envelope for scale drift is **exactly 100% / 1x / recorded
metrics**. The break point is not a gradual degradation with a survivable
rung above it — it is a hair-trigger at the _first_ deviation, enforced by the
postcondition, not the resolver. That is deliberately conservative: the same
gate is what stops a genuinely wrong screen.

**DPI has a second, independent failure reason** worth noting for engineers:
at `device_scale_factor > 1` the screenshot comes back in _device_ pixels
(e.g. 2560×1600) while clicks and the reported viewport are in _CSS_ pixels
(1280×800). Resolved coordinates and click coordinates desync, so even if the
postcondition were loosened, DPI drift would still not replay correctly
without a DPI-aware coordinate transform. (It still fails safe today.)

**Font family is the exception the heal ladder absorbs.** A family swap
changes glyph shapes and widths but not the text, so OCR still reads every
label:

- **Proportional faces (Georgia, Times New Roman): full pass.** Times stays
  close enough that 3 steps still template-match; the rest resolve via OCR.
  Georgia drifts more (all anchored steps land on geometry/OCR) but every step
  is located, healed, and the encounter saves to the correct patient. 5–8
  heals — this is the ladder doing exactly its job.
- **Monospace (Courier New): safe-halt at `step_005`.** The monospace face
  widens the referral table enough that the resolved row's context band no
  longer matches the recorded identity band, and the **pre-click identity
  gate** (`runtime.identity`) refuses to click — `identity.status =
  "mismatch"`, nothing saved. A stricter cosmetic change than a proportional
  swap, caught by a different, later guard, still fail-safe.

## What the heal ladder absorbs vs. what defeats it

| cosmetic change | heal ladder verdict |
|---|---|
| Font-family swap to a proportional face | **Absorbed** — OCR reads labels, templates heal, run completes |
| Font-family swap to monospace | **Defeated safely** — identity gate halts at the row click |
| Browser zoom (any level) | **Defeated safely** — step_000 postcondition halts before healing helps |
| DPI / device scale > 1x | **Defeated safely** — same postcondition halt (plus a coordinate-space desync) |
| Font-size bump (any) | **Defeated safely** — step_000 postcondition halt |

The ladder heals _label/appearance_ drift on elements it can re-identify by
text. It cannot heal _scale_ drift, because the postcondition that certifies
"we reached the right screen" is itself scale-sensitive and (correctly)
refuses to lower its bar for a globally rescaled frame.

## Is it safe? Yes — across the whole range

**0 wrong-actions and 0 crashes in 21 perturbation points.** Every failure is
a halt with an accurate report and no side effect. No cosmetic render change —
not 200% zoom, not 3x DPI, not a +37% font bump, not the realistic
125%-zoom-on-a-Retina-display pairing — ever wrote to the wrong patient. Under
cosmetic-only drift the target is unique and unchanged, so the resolver has no
look-alike to be fooled by; where a coordinate _could_ have gone wrong (DPI,
monospace reflow) the postcondition and identity gates caught it first.

## Deployment implication

The varied-hardware back-office risk is **real but bounded, and it is an
availability risk, not a safety risk.** A bundle recorded on one machine will
_halt_ (not misfire) on a colleague's 125%-zoom or Retina setup. Mitigations,
in order of leverage:

1. **Record at the deployment render** (100% zoom, note the display scaling),
   or record once per distinct render profile.
2. **A DPI-aware coordinate transform** would recover the entire DPI axis (the
   resolver already has the device/CSS pixel ratio available from the page).
3. **A scale-tolerant `region_stable`** (match the recorded crop across the
   template scale ladder before the phash check) would widen the zoom/font
   envelope from "exactly 100%" toward the resolver's own ±15–18% tolerance —
   without weakening the wrong-screen guard, since the crop content still has
   to match.

Until (2)/(3) land, the honest customer statement is the TL;DR above: it holds
at the recorded render, absorbs font-family drift, and outside that it halts
safely rather than acting wrongly.
