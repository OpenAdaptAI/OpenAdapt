# Dense sibling-surface false-abort / false-accept study

The identity band matcher's bounded aggregate (`docs/validation/IDENTITY_EVIDENCE.md`: **0 measured false accepts, 48.31% false aborts**) comes from synthetic string pairs and review probes. This study measures the same matcher on a dense, sibling-heavy rendered record list read by the repository's OCR path. Every band string in this study came from OCR reading a rendered image.

## Method (faithful to record + replay)

- **Fixture**: a dense clinical record list (40 rows: MRN / Patient Name / DOB / Sex / Status / Last Seen / Open) with seeded collision siblings placed one row from their target. Rendered over 5 seeds (1, 2, 3, 4, 5).
- **Record** (crisp, `device_scale_factor=2`, Arial 15px): OCR the full frame and store the identity band exactly as `compiler.compile` does — `context_from_lines(...)` with the clicked cell's template crop EXCLUDED and volatile lines dropped.
- **Replay**: at the resolved click point, extract the band exactly as `Replayer._verify_identity` does — `band_region`, translate the exclude crop to the resolved point, drop volatile lines, `lines_near_point` row refinement, then `verify_target_identity` with the same 2x-upscale retry. No Anthropic calls; identity + OCR only.
- **Two click configs** per target: `click_name` (open the chart by clicking the name cell — the NAME is then excluded from the band, so DOB/MRN/Sex/Status carry identity) and `click_action` (click the row's Open button — the NAME stays in the band).
- **Replay conditions** (the record frame is always crisp; the RISK variable is the replay surface): `hi_res_arial`, `native_arial`, `small_dense`, `serif_drift` (`hi_res_arial` = same crisp dsf2 control; `native_arial` = dsf1; `small_dense` = dsf1, 12px, tighter rows; `serif_drift` = dsf1, Georgia — an app font change between record and replay).
- **False abort** = resolver on the CORRECT row, identity fails to verify. **False accept** = resolver on the adjacent SIBLING row, identity verifies it as the target (catastrophic; must stay 0). Siblings are realistic different patients (distinct MRN); the confusable/transposed classes put the sole difference in the MRN.

Operating point (pinned, from the ROC): {'coverage_threshold': 0.8, 'uncovered_run_cap': 4, 'contradicted_chars_cap': 0, 'suspect_chars_cap': 0, 'unexplained_name_tokens_cap': 0, 'absent_name_token_cap': 3}.

## Headline (dense surface, armed clicks)

- **per-click false abort: 78.33%** (376/480) — of which 36 readable-but-mismatch and 0 unreadable.
- **per-click false accept: 0.00%** (0/480).
- unarmed clicks (no band recorded, identity gate never runs): 0.

**Versus the synthetic baseline** (false abort 48.31%, false accept 0.00%): the real dense-surface false abort is **78.33%**, i.e. **HIGHER** than the synthetic 48.31% by 30.02%. False accept STAYED 0 on real dense OCR.

## Structured-text (DOM) identity path (the headline)

Identity here is verified against STRUCTURED text -- the DOM row text under the click point (`backend.structured_text_at`, the same signal a native desktop backend gets from the UIA/AX tree) -- NOT OCR. The recorded target's DOM identity string is compared to the live DOM string at the resolved row by exact/normalized match, in which `0` and `O`, `1` and `l` are DISTINCT characters. This runs on the browser backend, where the dense table's DOM is available; on a pure-pixel substrate it is unavailable and identity falls back to the OCR path measured below.

- **structured-path false accept: 0.00%** (0/480).
- **structured-path false abort (over-halt): 0.00%** (0/480).

The digit-flanked glyph-collapse (`MG4408` vs `MG44O8`, `AC50061` vs `AC5OO61`) that produces false accepts on the OCR path in `click_action` -- and over-halts on the OCR path in `click_name` (identity resting solely on the collapsible MRN) -- does NOT occur here: the two MRNs are different strings in the DOM, so the sibling MISMATCHES and the true row VERIFIES. Because the DOM text is invariant across replay font/resolution, the structured path carries NO OCR-availability cost: it closes the class without #27's over-halt.

### Structured path -- by collision class

| group | n | false-abort (over-halt) | false-accept |
| --- | --- | --- | --- |
| `generational_suffix` | 40 | 0.00% (0) | 0.00% (0) |
| `id_confusion_O0` | 40 | 0.00% (0) | 0.00% (0) |
| `id_confusion_l1` | 40 | 0.00% (0) | 0.00% (0) |
| `id_numeric_O0` | 40 | 0.00% (0) | 0.00% (0) |
| `id_numeric_l1` | 40 | 0.00% (0) | 0.00% (0) |
| `id_split_numeric_O0` | 40 | 0.00% (0) | 0.00% (0) |
| `letterletter_name` | 40 | 0.00% (0) | 0.00% (0) |
| `mrn_transposition` | 40 | 0.00% (0) | 0.00% (0) |
| `near_surname` | 40 | 0.00% (0) | 0.00% (0) |
| `nguyen_variant` | 40 | 0.00% (0) | 0.00% (0) |
| `same_name_diff_dob` | 40 | 0.00% (0) | 0.00% (0) |
| `same_surname_diff_first` | 40 | 0.00% (0) | 0.00% (0) |

### Structured path -- by click config

| group | n | false-abort (over-halt) | false-accept |
| --- | --- | --- | --- |
| `click_action` | 240 | 0.00% (0) | 0.00% (0) |
| `click_name` | 240 | 0.00% (0) | 0.00% (0) |

The OCR band path (the pixel-substrate FALLBACK) is measured below. UPDATED for the 8th wrong-patient reopening: #27's "disclosed digit-flanked residual" -- a same-name/same-DOB homonym whose collapsible MRN OCR-collapses to the target's, name shown -- was a LIVE wrong-patient VERIFY (proved on the real replayer in PR #31). The OCR tier now ABSTAINS on ANY band resting on a glyph-confusable identifier, REGARDLESS of a matched name+DOB, so that residual is closed (0 false accept) at the cost of a higher halt rate on the OCR path; a different-NAME sibling still MISMATCHES and a clean name+DOB with a NON-confusable identifier still VERIFIES. The structured tier never lets the OCR fallback override a structured mismatch.

### By replay condition (OCR resolution)

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `hi_res_arial` | 120 | 78.33% (94) | 10 / 0 | 0.00% (0) |
| `native_arial` | 120 | 78.33% (94) | 13 / 0 | 0.00% (0) |
| `serif_drift` | 120 | 78.33% (94) | 1 / 0 | 0.00% (0) |
| `small_dense` | 120 | 78.33% (94) | 12 / 0 | 0.00% (0) |

### By click config

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `click_action` | 240 | 78.33% (188) | 21 / 0 | 0.00% (0) |
| `click_name` | 240 | 78.33% (188) | 15 / 0 | 0.00% (0) |

### By collision class

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `generational_suffix` | 40 | 20.00% (8) | 0 / 0 | 0.00% (0) |
| `id_confusion_O0` | 40 | 100.00% (40) | 30 / 0 | 0.00% (0) |
| `id_confusion_l1` | 40 | 100.00% (40) | 2 / 0 | 0.00% (0) |
| `id_numeric_O0` | 40 | 100.00% (40) | 0 / 0 | 0.00% (0) |
| `id_numeric_l1` | 40 | 100.00% (40) | 3 / 0 | 0.00% (0) |
| `id_split_numeric_O0` | 40 | 100.00% (40) | 0 / 0 | 0.00% (0) |
| `letterletter_name` | 40 | 80.00% (32) | 0 / 0 | 0.00% (0) |
| `mrn_transposition` | 40 | 40.00% (16) | 1 / 0 | 0.00% (0) |
| `near_surname` | 40 | 60.00% (24) | 0 / 0 | 0.00% (0) |
| `nguyen_variant` | 40 | 80.00% (32) | 0 / 0 | 0.00% (0) |
| `same_name_diff_dob` | 40 | 80.00% (32) | 0 / 0 | 0.00% (0) |
| `same_surname_diff_first` | 40 | 80.00% (32) | 0 / 0 | 0.00% (0) |

### By collision class x click config

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `generational_suffix::click_action` | 20 | 20.00% (4) | 0 / 0 | 0.00% (0) |
| `generational_suffix::click_name` | 20 | 20.00% (4) | 0 / 0 | 0.00% (0) |
| `id_confusion_O0::click_action` | 20 | 100.00% (20) | 15 / 0 | 0.00% (0) |
| `id_confusion_O0::click_name` | 20 | 100.00% (20) | 15 / 0 | 0.00% (0) |
| `id_confusion_l1::click_action` | 20 | 100.00% (20) | 2 / 0 | 0.00% (0) |
| `id_confusion_l1::click_name` | 20 | 100.00% (20) | 0 / 0 | 0.00% (0) |
| `id_numeric_O0::click_action` | 20 | 100.00% (20) | 0 / 0 | 0.00% (0) |
| `id_numeric_O0::click_name` | 20 | 100.00% (20) | 0 / 0 | 0.00% (0) |
| `id_numeric_l1::click_action` | 20 | 100.00% (20) | 3 / 0 | 0.00% (0) |
| `id_numeric_l1::click_name` | 20 | 100.00% (20) | 0 / 0 | 0.00% (0) |
| `id_split_numeric_O0::click_action` | 20 | 100.00% (20) | 0 / 0 | 0.00% (0) |
| `id_split_numeric_O0::click_name` | 20 | 100.00% (20) | 0 / 0 | 0.00% (0) |
| `letterletter_name::click_action` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |
| `letterletter_name::click_name` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |
| `mrn_transposition::click_action` | 20 | 40.00% (8) | 1 / 0 | 0.00% (0) |
| `mrn_transposition::click_name` | 20 | 40.00% (8) | 0 / 0 | 0.00% (0) |
| `near_surname::click_action` | 20 | 60.00% (12) | 0 / 0 | 0.00% (0) |
| `near_surname::click_name` | 20 | 60.00% (12) | 0 / 0 | 0.00% (0) |
| `nguyen_variant::click_action` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |
| `nguyen_variant::click_name` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |
| `same_name_diff_dob::click_action` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |
| `same_name_diff_dob::click_name` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |
| `same_surname_diff_first::click_action` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |
| `same_surname_diff_first::click_name` | 20 | 80.00% (16) | 0 / 0 | 0.00% (0) |

## Worst collision class

Highest false-abort collision class: **`id_confusion_O0`** at 100.00% (40/40).

## Adjacent-row bleed

- Bands whose raw OCR lines included a token from a NEIGHBOUR row (above/below the resolved row): 199/480 (41.46%).
- Neighbour tokens that SURVIVED the `lines_near_point` row filter into the identity band: 0.
- Trials where the row filter CHANGED the false-abort verdict (i.e. bleed would have changed the decision without it): 99.

**Finding:** the `lines_near_point` row refinement absorbs adjacent-row bleed — neighbour tokens are picked up in the coarse 64px band but filtered out before the verdict, and removing the filter would change decisions in the count above.

## False accepts (headline safety finding)

**Zero.** No seeded sibling was verified as its target on the real dense-OCR'd surface, across every collision class, click config, and replay condition. The catastrophic direction held.

## Honest verdict (does the product clear the flagship bar?)

On the dense sibling surface the TRUE per-click false abort is **78.33%**, above the synthetic 48.31%. False accept stayed at 0 — the catastrophic wrong-patient direction held on real dense OCR, which is the number that gates the regulated-clinic buyer.

The availability cost (false abort) is a per-click hybrid-fallback escalation (~$0.10) or a human retry; it is the cheap direction and is the price paid for the zero-false-accept posture. Selection-bias disclosure: this is measured on THIS rendered fixture + RapidOCR, not 'in the world' — a different renderer, font stack, or OCR engine would shift the false-abort rate (and could, in principle, surface a false accept the frozen confusion table does not model).
