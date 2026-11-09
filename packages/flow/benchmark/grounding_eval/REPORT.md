# openadapt-grounding vs the bespoke dense-list grounder

**Question.** End-to-end validation of the on-prem VLM appliance (PR #38,
`benchmark/appliance_validation/REPORT.md`) exposed a grounder gap: the bespoke
remote-VLM grounder (`RemoteGrounder.locate` -> served
`mlx-community/Qwen3-VL-4B-Instruct-4bit`) resolves the correct control
**column** but not the correct **row** on a dense EMR list -- **0/6 hits, ~472 px
median error** (tol 40 px), the proposed *y* clustering near the top regardless
of the requested patient. Can the ecosystem packages (**openadapt-grounding**,
**openadapt-ml**) close that gap, measured on the same surface, at $0?

**Answer (one line): ADOPT openadapt-grounding's OCR text-anchoring for
dense-list row resolution.** On the identical surface it goes from the VLM's
**0/6 @ 472 px** to **88 % hit@40 over 50 targets at ~3 px median** -- and
**100 % @ ~2 px on every OCR-distinct row**. The only misses are the deliberately
adversarial O0/l1 *wrong-patient* collision pairs, which are the **identity
band's** job to separate, not the grounder's. $0, CPU-only, no served model, no
paid API.

---

## What the two ecosystem packages actually provide

| Package | Grounding API | Backend | Runnable here at $0? |
|---|---|---|---|
| **openadapt-grounding** (v0.1.0) | `ElementLocator(registry).find(text, screenshot) -> LocatorResult(x, y)`; `RegistryBuilder().add_frame(...).build()`; the OCR primitive `ElementLocator._run_ocr(img) -> [Element(bounds, text)]` | **`pytesseract` (local Tesseract OCR)** for text-anchoring; optional `OmniParserClient` / `UITarsClient` (remote FastAPI/vLLM); optional Anthropic/OpenAI/Google VLM providers | **YES** -- core deps are `pillow` + `pytesseract` + `requests` only. Tesseract 5.5.1 already on this Mac. No GPU, no server, no key. |
| **openadapt-ml** (v0.5.0) `grounding/` | `GeminiGrounder().ground(image, target) -> RegionCandidates` | **Google Gemini vision API** (`gemini-2.5-flash`, needs `GOOGLE_API_KEY`) or remote **OmniParser**; plus oracle-bbox / Set-of-Marks helpers that need pre-labelled element IDs | **NO** -- the only real detector is a **paid** API (Gemini) or a remote served model. Oracle/SoM need ground-truth element IDs, so they cannot ground a raw screenshot. Out of scope under the $0 / zero-paid-API constraint. |

So the only genuinely local, $0 ecosystem grounder is **openadapt-grounding's OCR
text-anchoring**. That is what this study measures. openadapt-ml's grounder is a
serving/API shell (Gemini or remote OmniParser) -- no local capability to test.

The bespoke grounder and openadapt-grounding attack the problem from opposite
sides:

- **Bespoke VLM** regresses a single `(x, y)` from the whole page. One forward
  pass; nothing ties the point to a specific row. Empirically it locks onto the
  column and smears the row.
- **openadapt-grounding** OCRs *every* text box with pixel bounds, then anchors
  on **text**. A dense list's rows are individuated by unique text (name, and
  especially the **MRN**, the unique key), so anchoring on that text lands on the
  right row by construction -- the button is then the "Open" box on that row.

---

## Method -- identical surface, identical truth, identical target selection

Reused verbatim from the baseline (`openadapt_flow/validation/dense_surface.py`,
`benchmark/appliance_validation/run_validation.py::run_grounder`):

- Surface: `render_frame(build_dense_table(seed=1, n_rows=18), RECORD_CONDITION,
  top_offset_px=12)` -> **51 rows, 2240x3726 px** full-page screenshot (the
  baseline's native frame).
- Targets: the same deterministic spread -- `indices[::step][:6]` = rows
  `[0, 8, 16, 24, 32, 40]`.
- Ground truth: the **DOM centre of each row's Open button**
  (`frame.points[i][1]`), in screen px.
- Tolerance: **40 px** (REPORT.md headline) and 60 px (`run_grounder` default).

Three methods, all built only on openadapt-grounding primitives (harness:
`harness.py`; raw data: `results.json`, `results_sweep.json`):

| id | method | what it is |
|---|---|---|
| **A** | `find("Open")` | The naive shipped API asked for the button by its label. There are 51 identical "Open" tokens; `find` returns the first. Baseline-for-the-baseline. |
| **B** | `find(name)` + row-join | Library `find()` on the patient name to get the row, then the Open box on that row. |
| **C** | OCR row-anchor | Cluster the library's OCR boxes into rows, score each row's text against the target's **name + MRN** (with O/0, l/1 glyph normalisation), take the Open box in the winning row. The intended text-anchoring pattern. |

---

## Results

### Headline -- same 6 targets as the baseline (seed 1)

| grounder | hit@40 | hit@60 | median err | proposal rate | latency |
|---|---|---|---|---|---|
| **bespoke VLM** (Qwen3-VL-4B-4bit, served) | **0/6 (0 %)** | -- | **472 px** | 6/6 downscaled | **35 s / target** (native) |
| A - `find("Open")` | 1/6 (16.7 %) | 16.7 % | 1364 px | 100 % | -- |
| B - `find(name)`+row | 0/6 (0 %) | 0 % | 1636 px | 83 % | -- |
| **C - OCR row-anchor** | **4/6 (66.7 %)** | **66.7 %** | **3.1 px** | 100 % | **~1.9 s/frame, ~0 ms/target** |

C on the **4 OCR-distinct rows: 4/4 = 100 %, median 2.0 px.** The 2 misses are
rows 32 and 40 -- both **adversarial O0/l1 collision targets** -- landing exactly
**one row off (68 px)**, still 7x tighter than the VLM's 472 px.

### Wider sample -- method C across 5 seeds, 50 targets

| slice | n | hit@40 | median err |
|---|---|---|---|
| **overall** | 50 | **88.0 %** | **3.0 px** |
| **clean rows** (OCR-distinct MRN) | 40 | **100.0 %** | **2.2 px** |
| OCR-collision rows (O0/l1 sibling pairs) | 10 | 40.0 % | 67 px (one row off) |

### Why the collision rows miss -- and why that is not the grounder's failure

The misses are the *exact* wrong-patient surface the identity work exists for.
OCR of the two colliding rows (from the harness probe):

```
row 39  MRN 200633 (digits)  -> OCR "200633"
row 40  MRN 2OO633 (letters) -> OCR "200633"     # byte-identical
row 31  MRN PL19181 (digit-1)-> OCR "PL19181"
row 32  MRN PLl9181 (letter-l)-> OCR "PLI9181"    # collapses under O0/l1 norm
```

`2OO633` and `200633` render to **byte-identical OCR** -- no text signal can
separate those two patients. That is the same OCR-collapse class the appliance
REPORT's **Tier-1 identity veto** was built to catch (and does: 14/14, 0
false-accepts). Grounding's job is to get to the right *row band*; disambiguating
the wrong-patient sibling one row away is the **identity band's** job, and
openadapt-flow already ships a proven mechanism for it. Landing one row off (68 px)
is a bounded, adjacent error the downstream identity check vetoes -- categorically
different from the VLM's 472 px random smear, which has no row signal at all.

Methods A and B confirm the shipped `find()` **alone** does not solve dense lists
(a single generic-label lookup has no more row information than the VLM). The
capability is real but must be used as **row-anchor on the unique key**, not
`find(button_label)`.

---

## Cost, latency, setup

| | bespoke VLM (baseline) | **openadapt-grounding OCR (C)** |
|---|---|---|
| Hardware | served MLX 4-bit model, ~4.6 GiB RSS | CPU only, Tesseract |
| Setup | serve the VLM appliance | `pip install openadapt-grounding pytesseract` (+ system `tesseract`, already present) |
| Cost | model host | **$0** -- 0 GPU, 0 paid-API calls |
| Latency | **~35 s per target** at native res (empty/`None` above ~1800 px) | **~1.9 s OCR per screenshot, ~0 ms per target** (OCR once, resolve any number of targets) |
| Image-size ceiling | emits empty/degenerate output >~1800 px wide | works directly on the native 2240x3726 frame -- **no downscale needed** |

The VLM's per-target latency and its ~1800 px image ceiling (documented in the
appliance REPORT) are both dissolved: OCR runs once on the native frame and every
target resolves from the same boxes in microseconds.

---

## Verdict & recommendation

**ADOPT** openadapt-grounding's OCR text-anchoring as the dense-list row
resolver. Concretely:

1. **Grounding tier -> OCR row-anchor.** For "click the control in the row for
   patient (name, MRN)", OCR the frame once (openadapt-grounding
   `ElementLocator._run_ocr`), cluster boxes into rows, anchor on the **MRN /
   name** (unique key) -- not the button label -- then take the target control's
   box on that row. 100 % @ ~2 px on OCR-distinct rows, at $0 and ~0 ms/target.
2. **The remote-VLM appliance becomes a *fallback / serving shell*, not the
   primary dense-list grounder.** Keep it for surfaces with no anchoring text
   (icon-only toolbars, canvases) -- that is where openadapt-ml's Gemini/OmniParser
   grounders or `UI-TARS`/`GUI-Owl` would earn their cost -- but do not pay 35 s
   and a downscale to regress a point a 1.9 s local OCR pass nails.
3. **Keep the identity band in front of any click.** OCR row-anchor is bounded
   by OCR: on true glyph-collapse wrong-patient pairs it can land on the adjacent
   sibling. That is precisely what the (already-proven) identity veto catches;
   grounding should feed it, never replace it.

### Honesty / limits

- Small, hand-auditable samples: 6 targets (headline, baseline-comparable) and 50
  targets (5-seed sweep). Rates are directional, every case is in the JSON.
- Measured on the **synthetic** `dense_surface` renderer, not a live EMR. It is
  the same surface the baseline used, so the comparison is fair, but real OCR on
  a real EMR (anti-aliasing, theming, scroll) will be noisier -- re-measure there.
- openadapt-ml's grounder (Gemini / remote OmniParser) was **not** benchmarked:
  no $0/local path exists for it, per the task constraints. If a paid run is ever
  authorised, Gemini-2.5-flash grounding on this same surface is the natural
  next comparison.
- Method C's row scorer (name+MRN, glyph-normalised) is eval code, ~40 lines on
  top of the library OCR -- not a shipped component. Productionising it means
  moving that thin row-anchor into the grounding tier; the OCR itself is the
  library's.
- **The ~3 px median is a renderer artifact; "88 % @ 40 px" is an upper bound.**
  Method C's proposal is the OCR **text-token centre**; the ground truth is the
  **DOM button centre**. On this synthetic surface the button is a bare "Open"
  token, so text-centre and click-centre coincide and the residual collapses to
  ~2-3 px. A real EMR button (padding, an icon, a wider hit-box) puts the text
  centre tens of px off the click centre, so on a live surface the hit rate at a
  fixed 40 px tolerance can only be **lower** than measured here. Read 88 % @
  40 px as an upper bound conditional on text-centre approximate click-centre,
  not a portable field number.
- **The VLM baseline (0/6, 472 px) is a hardcoded imported constant, not a
  head-to-head re-run in this harness.** Those figures are lifted verbatim from
  the separate appliance_validation run (PR #38); this harness did not re-serve
  the model and re-measure it side-by-side with method C. The surfaces and
  target selection are identical by construction, but the VLM number carries
  that provenance caveat -- it is a cited prior result, not a fresh arm.
- **Method C is handed the ground-truth patient identity as its query.** The
  row scorer receives the target's own name + MRN and finds the matching row.
  That is fair for the task framing ("click the control in the row for *this*
  patient"), but it measures "given the correct identity, can OCR find the
  row," **not** end-to-end grounding from a raw intent -- identity resolution
  is assumed upstream, not evaluated here.
