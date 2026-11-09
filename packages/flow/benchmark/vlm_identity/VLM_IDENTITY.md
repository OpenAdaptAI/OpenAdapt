# Local VLM same/different identity comparator -- experiment

Does a small **local open VLM** work as the top rung of the pixel-only identity ladder -- a **veto-only** same/different comparator that catches the `O`/`0` wrong-patient collapse OCR misses, and is robust to the render drift where a cheap pixel/SSIM compare false-halts? This is the validation the identity memo demanded (novel use; VLMs are documented weak at fine-grained perception + visual comparison), run on a REAL local MLX model with ZERO Anthropic/API calls.

## Model + footprint + latency

- **Model:** `mlx-community/Qwen3-VL-4B-Instruct-4bit` (open weights, MLX, local).
- **On-disk footprint:** 3.11 GB (4-bit).
- **Load time:** 1.14 s (one-time).
- **Per-call latency:** mean 0.802s, p50 0.797s, p95 0.936s over 36 calls (the comparator fires only as a rare escalation, so this is invisible in practice).

## 1. Collapse pairs -- FALSE-ACCEPT (the safety number)

Different patients whose identifiers are glyph-confusable. Under veto-only, the correct answer is DIFFERENT (veto the wrong patient); any **SAME is a false-accept -- the veto failed** and a wrong-patient write proceeds.

- All 12 confusable pairs: false-accept **8.3%**, detection 91.7%.
- The 7 pairs the repo's own OCR actually COLLAPSES (where the VLM is the *last* line of defence): false-accept **0.0%**, detection 100.0%.

| class | A | B | OCR(A) | OCR(B) | OCR collapsed | pixel-diff | VLM verdict | correct |
|---|---|---|---|---|---|---|---|---|
| digit_flanked_O0 | `MG4408` | `MG44O8` | `MG4408` | `MG4408` | YES | 0.043 | DIFFERENT | ok |
| digit_flanked_O0 | `AC50061` | `AC5OO61` | `AC50061` | `AC50061` | YES | 0.074 | DIFFERENT | ok |
| digit_flanked_O0 | `RT8005` | `RT8OO5` | `RT8005` | `RT8005` | YES | 0.070 | DIFFERENT | ok |
| digit_flanked_O0 | `MG7008` | `MG7O08` | `MG7008` | `MG7008` | YES | 0.063 | DIFFERENT | ok |
| digit_flanked_O0 | `BX3040` | `BX3O40` | `BX3040` | `BX3040` | YES | 0.066 | DIFFERENT | ok |
| digit_flanked_O0 | `LN6001` | `LN6OO1` | `LN6001` | `LN6001` | YES | 0.054 | DIFFERENT | ok |
| digit_flanked_O0 | `PT9012` | `PT9O12` | `PT9012` | `PT9012` | YES | 0.064 | DIFFERENT | ok |
| alpha_flanked_O0 | `C0X3834` | `COX3834` | `C0X3834` | `COX3834` | no | 0.126 | DIFFERENT | ok |
| alpha_flanked_O0 | `D0T99` | `DOT99` | `D0T99` | `DOT99` | no | 0.103 | SAME | FALSE-ACCEPT |
| letter_l_one | `PL13421` | `PLl3421` | `PL13421` | `PLI3421` | no | 0.088 | DIFFERENT | ok |
| letter_l_one | `RC1105` | `RCll05` | `RC1105` | `RCII05` | no | 0.063 | DIFFERENT | ok |
| letter_l_one | `BK7011` | `BK70ll` | `BK7011` | `BK70ll` | no | 0.023 | DIFFERENT | ok |

> `pixel-diff` is the mean per-pixel intensity difference between the two crops (0 = pixel-identical, unverifiable by *any* vision method incl. SSIM; larger = the glyphs differ in pixels, so a comparator *could* catch them). A SAME verdict where pixel-diff > 0 is a genuine VLM miss, not the font floor.

## 2. Same-value CLEAN -- over-halt

Identical value, clean re-render (n=6). Must say SAME; a DIFFERENT is a false-veto (over-halt). Over-halt rate: **0.0%**.

## 3. Same-value under RENDER DRIFT -- the value test

The SAME value re-rendered under dark theme / ~120% zoom / a serif font -- exactly where a pixel/SSIM compare false-halts (the pixels change while the value does not). A semantic VLM comparator earns its cost only if it still says SAME here. Over-halt under drift: **33.3%**.

| drift condition | n | over-halt | mean pixel-diff vs record |
|---|---|---|---|
| drift_dark_theme | 6 | 0.0% | 0.9018 |
| drift_zoom_120 | 6 | 33.3% | 0.1368 |
| drift_serif_font | 6 | 66.7% | 0.1106 |

> The mean pixel-diff column shows how far each drift moves the pixels: a cheap pixel/SSIM compare thresholding on this would false-halt every one of these SAME-value pairs. The VLM's over-halt rate is what it buys over that.

## VERDICT

**Qualified YES -- it works as a SAFETY VETO, and it demonstrably beats cheap pixel-compare on the drift that matters most, but it is not a drop-in drift-robust verifier.** Three things are simultaneously true on this fixture:

1. **Safe.** Zero false-accepts (0.0%) on the 7 pairs the repo's own OCR actually COLLAPSES -- the exact slice where the VLM is the last line of defence. It catches the `O`/`0` wrong-patient collapse OCR misses (incl. the flagship `MG4408`/`MG44O8`), so under veto-only it never silently passes the wrong patient in the regime that reaches it. The one false-accept in the corpus (`D0T99`/`DOT99`) is on an alpha-flanked pair the OCR ALREADY distinguishes, so it never escalates to the VLM in production.
2. **Robust exactly where cheap pixel-compare is hopeless.** Under a dark-theme re-render the two crops differ in 90.2% of their pixels (inverted colours) -- a pixel/SSIM compare false-halts every one -- yet the VLM over-halts 0.0% of them. That is the headline value proof: semantic 'different rendering, same value' where pixels are useless.
3. **But weak under font/zoom drift:** over-halt climbs to 33.3% across all drifts (serif font worst). Over-halt is the CHEAP, fail-safe direction (escalate to hybrid/structured-text/human, ~$0.10), not a wrong-patient write -- so this is an AVAILABILITY cost, not a safety hole. Net: deploy it as the veto rung ABOVE cheap-pixel-compare (pixel-compare handles same-render look-alikes for free; the VLM rescues the theme-drift case and vetoes the O/0 collapse), but do NOT trust its SAME as a substitute for structured text under heavy font drift.

_Selection-bias disclosure: measured on THIS renderer + RapidOCR + this MLX model. A different font stack, OCR engine, or VLM would shift these numbers. The point estimates are a floor-test, not a universal claim._
