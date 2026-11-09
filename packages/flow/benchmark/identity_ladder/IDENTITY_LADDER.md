# Integrated identity ladder — measured on the dense glyph-collapse surface (O/0 + l/1, alphanumeric AND purely-numeric MRNs)

Every number below comes from the **production tier stack**: this harness drives the REAL `Replayer._verify_identity` (**structured text → pixel-compare → optional VLM veto → OCR name+DOB → halt**), never a hand-built tier subset. The OCR tier the replayer ALWAYS appends is therefore in the stack for every config — closing the measurement flaw that hid the 8th wrong-patient reopening.

Surface: dense glyph-collapse same-name/same-DOB homonyms -- O/0 and l/1, alphanumeric AND purely-numeric MRNs (different patients one MRN glyph apart; 9th reopening added numerics) (14 homonym pairs, each measured CORRECT-resolution and WRONG-resolution).

| Config | substrate | false-accept | over-halt |
|---|---|---:|---:|
| `structured` | browser/DOM (structured text) | 0/14 (0%) | 0/14 (0%) |
| `pixel_stable` | pixel-only, stable render, crop (pixel VERIFY gated) | 0/14 (0%) | 14/14 (100%) |
| `pixel_drift_vlm_on` | pixel-only, drifted render, VLM ON (veto-only) | 0/42 (0%) | 42/42 (100%) |
| `pixel_drift_vlm_off` | pixel-only, drifted render, VLM OFF | 0/42 (0%) | 42/42 (100%) |
| `ocr_only_confusable` | pixel-only, NO crop / NO VLM → OCR tier only | 0/42 (0%) | 42/42 (100%) |

**Safety invariant — 0 false-accept across ALL configs, measured on the real replayer stack: HOLDS.**

- **OCR alone cannot verify a collapsible MRN.** On a pure-pixel substrate, a band whose identity rests on a glyph-confusable MRN (ANY identifier-position token carrying an O/0 or l/1/I — numeric OR alphanumeric, the 9th reopening) is NOT safely verifiable by OCR: a same-name/same-DOB homonym whose distinguishing glyph OCR collapsed is indistinguishable. The OCR tier ABSTAINS → HALT (the `ocr_only_confusable` and `pixel_drift_*` over-halt). Safe verification needs the **structured-text tier** (DOM/a11y) — and, once Blocker 2's crop capture + jitter-robust distance land, the **pixel-crop tier** on a stable render. The OCR name+DOB tier alone is NOT a safe identity check on a collapsible MRN; on a pure-pixel substrate without structured text the honest outcome is HALT.
- The VLM tier is **veto-only**: a `"same"` answer never grants a pass (it abstains), so under drift a correct patient falls through to the OCR tier and HALTs; the VLM can only REJECT a wrong patient. This is why `pixel_drift_vlm_on` over-halts on all correct rows.
- The VLM tier is OPTIONAL and OFF by default: the default install runs structured-text + pixel-compare + OCR + halt with no model.
- **Blocker 2**: the pixel-compare VERIFY path is HARD-GATED (cross-render sub-pixel jitter defeats a safe same/different threshold at realistic crop scale, and an absolute whole-crop threshold false-accepts a diluted one-glyph difference). The pixel tier may only MISMATCH (scale-invariant localized spike → safe HALT) or ABSTAIN until a fixed-size crop capture + jitter-robust distance land — so on a pure-pixel substrate the only tier that VERIFIES today is structured text.
- VLM verdicts: ProbeFaithfulVLM reproducing benchmark/vlm_identity (PR #28): 100% detection / 0% false-accept; veto-only (a 'same' answer abstains, never grants a pass).
