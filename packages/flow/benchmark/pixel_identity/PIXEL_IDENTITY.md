# Pixel-perceptual identity-comparison probe

**Hypothesis under test.** The rendered pixels retain the O/0 and l/1 distinction that OCR discards, so a pixel / perceptual comparison of the identifier (MRN) crop can distinguish a wrong-patient sibling that OCR collapses. This is a standalone measurement that de-risks the pixel-native fix tier (tier 3) before integration. It does NOT modify `identity.py`, the replayer, or the `dense_surface` harness -- it reuses `render_table_html` unchanged and only adds crop extraction and pixel comparison. Zero Anthropic calls.

## OCR collapse baseline (what the pixels must overcome)

Each pair is rendered and the MRN cell OCR'd with the repo's own RapidOCR. If the target and its one-glyph-apart sibling read as the SAME string, every string-level identifier rule downstream is blind to the difference -- only the pixels still carry it.

| pair | target | sibling | OCR(target) | OCR(sibling) | collapsed? |
| --- | --- | --- | --- | --- | --- |
| `O0_digit_1` | `MG4408` | `MG44O8` | `mg4408` | `mg4408` | YES |
| `O0_digit_2` | `AC50061` | `AC5OO61` | `ac50061` | `ac50061` | YES |
| `O0_digit_3` | `RC90210` | `RC9O210` | `rc90210` | `rc90210` | YES |
| `O0_alpha_1` | `C0X3834` | `COX3834` | `c0x3834` | `cox3834` | no |
| `O0_alpha_2` | `B0X7521` | `BOX7521` | `b0x7521` | `box7521` | no |
| `l1_digit_1` | `MG4118` | `MG41l8` | `mg4118` | `mg4118` | YES |
| `l1_digit_2` | `AC50161` | `AC50l61` | `ac50161` | `ac50161` | YES |
| `l1_alpha_1` | `PL1X904` | `PLlX904` | `pl1x904` | `plix904` | no |
| `l1_alpha_2` | `RX1T552` | `RXlT552` | `rx1t552` | `rxit552` | no |

**5/9 pairs collapse under OCR** -- target and sibling become byte-identical strings, exactly the wrong-patient false-accept mechanism `dense_surface` found. The question is whether the pixels separate what these strings cannot.

## Separation per method (same-value vs different-value on the collapse crops)

Same-value distance = the target (or sibling) MRN crop **recorded vs re-rendered** (must stay LOW so the correct patient verifies). Different-value distance = target crop vs OCR-colliding sibling crop, cross-render (must be HIGH so the wrong patient halts). `AUC` = P(different > same); `clean` = every different-value scored strictly worse than every same-value (a threshold splits them with no overlap).

| method | category | AUC | same median | same max | diff min | diff median | clean split | threshold |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `phash_hamming` | perceptual hash (phash) | 1.0000 | 0.0000 | 0.0000 | 12.0000 | 36.0000 | YES | 6.0000 |
| `dhash_hamming` | perceptual hash (dhash) | 1.0000 | 0.0000 | 0.0000 | 2.0000 | 18.0000 | YES | 1.0000 |
| `edge_iou` | edge-map (Canny IoU) | 1.0000 | 0.0000 | 0.0000 | 0.3425 | 0.5338 | YES | 0.1712 |
| `ncc_global` | normalized cross-correlation (template) | 1.0000 | 0.0000 | 0.0000 | 0.1616 | 0.3529 | YES | 0.0808 |
| `l1_global` | raw pixel L1 | 1.0000 | 0.0000 | 0.0000 | 0.0119 | 0.0253 | YES | 0.0059 |
| `orb_feature` | feature (ORB) | nan | - | - | - | - | no | - |
| `charcell_ssim_max` | character-cell-aligned (SSIM) | 1.0000 | 0.0000 | 0.0000 | 0.2238 | 0.4459 | YES | 0.1119 |
| `local_maxdiff` | localized max abs-diff | 1.0000 | 0.0000 | 0.0000 | 0.0975 | 0.1185 | YES | 0.0487 |
| `l2_global` | raw pixel L2 | 1.0000 | 0.0000 | 0.0000 | 0.0814 | 0.1200 | YES | 0.0407 |
| `ssim_global` | SSIM | 1.0000 | 0.0000 | 0.0000 | 0.0548 | 0.1187 | YES | 0.0274 |

**Cleanly separating methods (AUC 1.0, no overlap): `phash_hamming`, `dhash_hamming`, `edge_iou`, `ncc_global`, `l1_global`, `charcell_ssim_max`, `local_maxdiff`, `l2_global`, `ssim_global`.**

Two honest caveats on this table. (1) The same-value distance is **0.0** for every bounded method: a re-render at the IDENTICAL config (same font/scale/theme, only a few-pixel vertical offset that the cell-crop realigns) is byte-identical, so the stable-render separation is trivially perfect and any method suffices. The realistic same-value noise -- and the real test of a threshold -- is the cosmetic-drift section below. (2) `orb_feature` returns nan: the MRN crop is too small and low-texture for ORB to find stable keypoints, so feature matching is not usable at this crop size (a documented negative result, not a separation).

## Recommended method + threshold

- **`local_maxdiff`** (localized max abs-diff): AUC 1.0000, clean split at threshold **0.0487** (same-value up to 0.0000, different-value from 0.0975 -- a gap of 0.0975).
- **`ssim_global`** (SSIM): AUC 1.0000, clean split at threshold **0.0274** (same-value up to 0.0000, different-value from 0.0548 -- a gap of 0.0548).
- **`charcell_ssim_max`** (character-cell-aligned (SSIM)): AUC 1.0000, clean split at threshold **0.1119** (same-value up to 0.0000, different-value from 0.2238 -- a gap of 0.2238).
- **`ncc_global`** (normalized cross-correlation (template)): AUC 1.0000, clean split at threshold **0.0808** (same-value up to 0.0000, different-value from 0.1616 -- a gap of 0.1616).

## Cosmetic-drift degradation (where same-value starts to false-halt)

The SAME target MRN is re-rendered under cosmetic drift and compared to its stable reference crop. If the same-value distance climbs past the different-value floor (`diff min` above), a naive pixel compare can no longer tell 'same patient, drifted render' from 'different patient' -- it would FALSE-HALT the correct patient. That is the escalation point to a VLM / robust-feature tier.

### `local_maxdiff` (stable different-value floor = 0.0975, clean threshold = 0.0487)

| drift condition | same-value median | same-value max | crosses diff-floor? | verdict |
| --- | --- | --- | --- | --- |
| `dark_theme` | 0.8627 | 0.8627 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_110` | 0.1384 | 0.1548 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_125` | 0.1299 | 0.1407 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_georgia` | 0.1260 | 0.1379 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_verdana` | 0.1307 | 0.1430 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_times` | 0.1239 | 0.1275 | YES | FALSE-HALT RISK (needs VLM tier) |

### `ssim_global` (stable different-value floor = 0.0548, clean threshold = 0.0274)

| drift condition | same-value median | same-value max | crosses diff-floor? | verdict |
| --- | --- | --- | --- | --- |
| `dark_theme` | 0.9674 | 0.9774 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_110` | 0.2182 | 0.2440 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_125` | 0.2523 | 0.2723 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_georgia` | 0.1406 | 0.1796 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_verdana` | 0.1980 | 0.2113 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_times` | 0.1580 | 0.1889 | YES | FALSE-HALT RISK (needs VLM tier) |

### `charcell_ssim_max` (stable different-value floor = 0.2238, clean threshold = 0.1119)

| drift condition | same-value median | same-value max | crosses diff-floor? | verdict |
| --- | --- | --- | --- | --- |
| `dark_theme` | 1.2710 | 1.2874 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_110` | 0.5920 | 0.6477 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_125` | 0.5282 | 0.5839 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_georgia` | 0.5188 | 0.6407 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_verdana` | 0.5623 | 0.6496 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_times` | 0.5343 | 0.6051 | YES | FALSE-HALT RISK (needs VLM tier) |

### `ncc_global` (stable different-value floor = 0.1616, clean threshold = 0.0808)

| drift condition | same-value median | same-value max | crosses diff-floor? | verdict |
| --- | --- | --- | --- | --- |
| `dark_theme` | 1.9960 | 1.9962 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_110` | 0.6616 | 0.7295 | YES | FALSE-HALT RISK (needs VLM tier) |
| `scale_125` | 0.6925 | 0.7279 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_georgia` | 0.4596 | 0.5907 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_verdana` | 0.5652 | 0.6400 | YES | FALSE-HALT RISK (needs VLM tier) |
| `font_times` | 0.5222 | 0.6295 | YES | FALSE-HALT RISK (needs VLM tier) |

## Pixel-identical-font floor (the true limit)

O/0 and l/1 rendered as isolated glyphs in common fonts. Where the two glyphs render pixel-identical (`max abs diff == 0`), NO vision method -- pixel, perceptual, or VLM -- can distinguish them; the distinction does not exist in the raster. This is a real, disclosed limit.

| font | O vs 0 max-diff | O/0 identical | l vs 1 max-diff | l/1 identical | l vs I max-diff |
| --- | --- | --- | --- | --- | --- |
| `Arial` | 255 | distinct | 255 | distinct | 255 |
| `Helvetica` | 255 | distinct | 255 | distinct | 255 |
| `Verdana` | 255 | distinct | 255 | distinct | 255 |
| `Tahoma` | 255 | distinct | 255 | distinct | 255 |
| `Trebuchet MS` | 255 | distinct | 255 | distinct | 255 |
| `Georgia` | 255 | distinct | 255 | distinct | 255 |
| `Times New Roman` | 255 | distinct | 255 | distinct | 255 |
| `Courier New` | 255 | distinct | 255 | distinct | 255 |
| `Courier` | 255 | distinct | 255 | distinct | 255 |
| `Menlo` | 255 | distinct | 255 | distinct | 255 |
| `Monaco` | 255 | distinct | 255 | distinct | 255 |
| `Andale Mono` | 255 | distinct | 255 | distinct | 255 |
| `Comic Sans MS` | 255 | distinct | 255 | distinct | 255 |
| `monospace` | 255 | distinct | 255 | distinct | 255 |

- Fonts where **O and 0 are pixel-identical**: none of those tested.
- Fonts where **l and 1 are pixel-identical**: none of those tested.

## Verdict

**Does pixel-perceptual comparison of the identifier crop close the OCR-collapse wrong-patient gap on pure pixels (no DOM / a11y)?**

**YES, on stable renders.** OCR collapsed 5/9 target/sibling pairs to identical strings, yet `local_maxdiff` separates every different-value pair from every same-value pair with AUC 1.0000 and a clean threshold (0.0487). The pixels DO retain what OCR discards. A cheap pixel-compare (tier 3) is sufficient to catch the wrong-patient sibling when the replay render matches the recorded render.

**At what render-drift point does it need a more robust (VLM / feature) tier?**

The clean pixel separation holds only while the replay render tracks the recorded one. It BREAKS under: `dark_theme`, `scale_110`, `scale_125`, `font_georgia`, `font_verdana`, `font_times` -- there the same (correct) patient's drifted crop scores at or beyond the different-patient floor, so a pixel-only compare would false-halt the right patient. Those drifts are exactly where tier 4 (a VLM / drift-robust feature comparison) must take over.

**Bottom line for the tiering:** on a STABLE render the cheap pixel compare (tier 3) alone catches the OCR-collapse wrong-patient case that every string rule misses; the VLM (tier 4) is needed only once the replay render drifts in scale / font / theme past the points above, and can never recover a pixel-identical-font collapse.
