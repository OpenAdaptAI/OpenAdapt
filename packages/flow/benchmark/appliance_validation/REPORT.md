# Remote VLM appliance — end-to-end validation against a real MLX model

**What this is.** The on-prem VLM service (`openadapt_flow/services/vlm_service`,
PR #34) and the three wired tiers that call it (`RemoteIdentityVLM`,
`RemoteStateVerifier`, `RemoteGrounder` in `openadapt_flow/runtime/remote_vlm.py`,
PRs #36/#37) had only ever been tested against **stubs**. The fail-safe plumbing
(outage ⇒ safe-halt) was already proven. This study proves whether the
**features actually work when a real model is served**, exercised through the
**same production wiring** the runtime uses — the fail-safe HTTP clients talking
to the real service with `VLM_BACKEND=mlx` — never the model directly.

**No shipped runtime was edited.** This directory adds only a validation harness,
fixtures, results, and this report.

## Setup (reproducible, $0, no cloud)

| | |
|---|---|
| Host | Apple **M2 Max** (arm64, 96 GB), MLX / Metal, **zero cloud, zero Anthropic API calls** (`ANTHROPIC_API_KEY` unset) |
| Model | `mlx-community/Qwen3-VL-4B-Instruct-4bit` (the service's `MLXBackend` default; the PR #28 identity-probe model) |
| Service | real subprocess: `VLM_BACKEND=mlx VLM_MODEL=… VLM_SERVICE_TOKEN=test openadapt-flow-vlm-service --port …` |
| Driven through | `appliance_from_env()` → `RemoteIdentityVLM.compare/same_or_different`, `RemoteStateVerifier.holds`, `RemoteGrounder.locate` (the wired path) |
| Confirmed first | `GET /health` → `{"status":"ok"}`, `GET /ready` → `{"ready":true,"backend":"mlx","model":"…4bit"}` |
| Model load | **3.1 s** (weights already cached; cold download is one-time, ~3 GB on disk at 4-bit) |
| Peak service RSS | **~4.6 GiB** during the full run |

Harness: `run_validation.py` (+ `service_manager.py`, `state_fixtures.py`),
supplements `supplement_resolution.py` and `supplement_identity_numeric.py`.
Raw data: `results.json`, `results_supplement.json`, `results_identity_full.json`.

---

## Verdicts (one line per tier)

| Tier | Verdict | Evidence |
|---|---|---|
| **1. Identity veto** (safety-critical) | **REAL** | **0 false-accepts on the OCR-collapse surface** (incl. purely-numeric MRNs), 100 % detection; wired `same_or_different` 100 % consistent with `compare`. |
| **2. Drift-oracle state verifier** | **UNDERPERFORMS / conditionally real** | Works only when the screenshot is downscaled to ~900 px: true-rescue **6/6**, **false-rescue 1/8 (12.5 %)**. At native 2× Retina it is **non-functional** (model emits empty text ⇒ every call `uncertain` ⇒ safe-halt). |
| **3. Grounder** | **BROKEN for dense lists** | Native full-page frame: **0 usable proposals** (all null, safe-halt), 35 s/call. Even downscaled it gets the control **column** right but the **row wrong** — 0/6 hits, 472 px median error. |

---

## 1. Identity veto — SAFETY-CRITICAL (headline)

Wrong-patient homonyms: pairs that share name+DOB and differ only by a
one-glyph-collapsible MRN (O/0, l/1). Rendered with the **exact PR #28 probe
renderer** (`vlm_identity_probe.render_crop`, magnified Playwright crops), driven
through `RemoteIdentityVLM` → service → real model. Under the **veto-only**
contract the correct answer for a wrong-patient pair is a veto
(`compare → MISMATCH/ABSTAIN`, `same_or_different → "different"`); a
`VERIFY`/`"same"` is a **catastrophic false-accept** (wrong-patient write proceeds).

### Headline — the OCR-collapse surface (where the veto is the *sole* defense)

Definitive corpus `pixel_identity_probe.COLLAPSE_PAIRS` (14 wrong-patient pairs:
digit-flanked, alpha-flanked, **and purely-numeric MRNs**):

| flank class | detected (vetoed) | false-accepts |
|---|---|---|
| digit-flanked O0/l1 | 5 / 5 | 0 |
| alpha-flanked O0/l1 | 4 / 4 | 0 |
| **purely-numeric MRN** (`100512` vs `1OO512`) | 5 / 5 | **0** |
| **total** | **14 / 14 (100 %)** | **0 (0.0 %)** |

Restricting to the pairs the repo's own RapidOCR **actually collapses** (verified:
`MG4408`≡`MG44O8`, `100512`≡`1OO512` both read byte-identically) — the cases
where OCR is blind and the VLM veto is the last line of defence — false-accepts =
**0**, detection = **100 %**. This **reproduces PR #28's probe claim
(100 % detection, 0 % false-accept) through the wired production path**, including
the purely-numeric class that was the 9th wrong-patient reopening.

### The one miss, and why it is not the catastrophe

A second corpus (`vlm_identity_probe.COLLAPSE_PAIRS`, 12 pairs) produced **one**
false-accept: `D0T99` vs `DOT99` (verdict `verify`). But the repo's OCR **reads
these two apart** (`'D0T99'` vs `'DOT99'`) — an alpha-flanked, dictionary-word
confusable (the model normalizes "D0T" → the word "DOT"). It is **not** on the
OCR-collapse surface, so the **deterministic OCR identity band already separates
the two patients** and the layered system still halts. The VLM veto only has to
catch what OCR misses, and on that set it missed **nothing**. Combined across both
corpora: **1 false-accept in 26 wrong-patient pairs**, and that one is
OCR-distinguishable.

### Over-veto (do correct patients spuriously halt?)

Same-patient cases (must **not** veto). Over-veto = false halt = availability
cost, not safety.

| same-patient suite | n | over-vetoed |
|---|---|---|
| clean re-render (non-confusable) | 6 | 0 |
| confusable target vs identical re-render | 12 | 0 |
| under render drift (dark / zoom / serif) | 18 | 6 |
| **total** | **36** | **6 (16.7 %)** |

All 6 over-vetos are under drift (4 serif-font, 2 zoom-120 %); dark-theme and
clean re-renders never over-veto — consistent with #28's "robust to drift, modest
over-halt tail." Wired `same_or_different` matched `compare` on **all 96 calls**
(0 disagreements): the Replayer-facing adapter is faithful.

**Latency:** median **0.66 s**, p95 0.75 s, max 0.82 s per compare.

**Tier-1 verdict: REAL.** 0 false-accepts where it matters (incl. purely-numeric
MRNs), 100 % detection, sub-second, with a modest drift over-veto tail and one
non-catastrophic miss the OCR band already covers.

---

## 2. Drift-oracle state verifier — quantifying the residual risk

`docs/deployment/ON_PREM_VLM.md` describes `verify_state` as a semantic oracle
("did the intended state happen?", robust to font/scale/theme drift) used when a
deterministic postcondition false-fails. The documented residual risk (it "can
rescue a genuine failure that ambiguously reads as success") was asserted, never
measured. Measured here via `RemoteStateVerifier.holds` (a confident `"yes"` =
rescue; `"no"`/`"uncertain"` = halt) over 14 hand-labelled screens (6 true-rescue,
8 false-rescue); exact `expected_state` strings and screens in `state_fixtures.py`.

### Finding A — at native 2× Retina the tier is non-functional

Rendered at `device_scale_factor=2` (~1800×1200, the runtime's real screenshot
scale), **all 14 calls returned `uncertain`** — at that image size the 4-bit MLX
build emits an **empty generation** (`raw=''`), which the fail-safe parser
collapses to `uncertain` (halt). true-rescue 0/6, false-rescue 0/8. Safe, but the
feature never fires. Confirmed in isolation: same screen at 1800×1200 → `''`; at
900×600 → `'YES'`. **The service/clients pass the screenshot through
un-downscaled, so on a Retina runner the drift-oracle would `uncertain`-halt every
call.**

### Finding B — at a model-friendly ~900 px, the real rates

Re-run identically at `device_scale_factor=1` (~900×600):

| | n | rate |
|---|---|---|
| **TRUE-RESCUE** (drift-obscured success ⇒ should say "yes") | 6 | **6/6 = 100 %** |
| **FALSE-RESCUE** (not-success but success-shaped ⇒ must say "no") | 8 | **1/8 = 12.5 %** |

- **All 6 true-rescue** cases (dark theme, serif+scaled, low-contrast, dark
  italic, tiny toast, large serif sign-off) were correctly rescued — the oracle
  reads meaning through drift where a literal OCR match would miss. Real value.
- **7 of 8 false-rescue** traps correctly refused (`"no"`): `Save failed` error
  banner in a success-shaped card, blank form, *Appointment cancelled* (opposite
  action), a **different-patient** "Record saved", *Signed out* green banner,
  validation error, stale dashboard.
- **1 false-rescue slipped through (the residual-risk number):**
  `fr_saving_spinner` — a **"Saving…" in-progress screen** judged `"yes"` (saved)
  when the save had not completed. An ambiguous *partial/in-progress* state that
  reads as success — exactly the mode LIMITS.md warns about. **Measured
  false-rescue rate: 12.5 % (1/8), n small.**

**Latency:** ~1.3 s/call at 900 px; ~5.5 s at native 2× (before returning empty).

**Tier-2 verdict: UNDERPERFORMS / conditionally real.** The rescue works (100 %
true-rescue) and mostly resists success-shaped decoys, but (a) it is **inert at
native Retina** unless the caller downscales, and (b) it carries a **measured
~12.5 % false-rescue** on ambiguous in-progress states — a real, now-quantified
residual risk that belongs in `docs/LIMITS.md`.

---

## 3. Grounder — availability

`RemoteGrounder.locate` on real rendered frames (`dense_surface.render_frame`, a
dense 40-row EMR list), truth = the DOM centre of each target's **Open** button.

- **Native full-page frame (2240×3726):** model returned **null for all 6
  targets** → `locate` yields `None`. Hit-rate **0/6**, 35 s/call. Safe direction
  (a `None` halts the ladder, never a wrong click) but the feature does not fire
  on a full-page dense list at native scale.
- **Downscaled to ~1000 px:** model now returns a point for all 6 (proposal rate
  6/6), but hit-rate is still **0/6**, **median error 472 px** (tol 40 px).
  Diagnostic: it gets the **X (Open-button column) right** (x≈922 vs truth 919)
  but **cannot disambiguate the row** — proposed y clusters near the top
  regardless of requested patient (only the first row lands close, err 60 px).

**Tier-3 verdict: BROKEN for dense lists** at this model/resolution. It fails safe
(no proposal ⇒ halt; the deterministic identity band disposes before any click
anyway) but resolves no targets in a dense EMR list. A cropped grounding region
or the production `GUI-Owl-1.5-8B` is the likely fix.

---

## Practical numbers for the ON_PREM_VLM sizing doc

| Metric | Value (M2 Max, MLX, Qwen3-VL-4B-4bit) |
|---|---|
| Model load (one-time) | 3.1 s (warm); ~3 GB on disk (4-bit) |
| Peak service RSS | ~4.6 GiB |
| Identity compare latency | **median 0.66 s**, p95 0.75 s |
| State verify latency | ~1.3 s at 900 px; ~5.5 s at 1800×1200 (then empty) |
| Ground latency | 35 s at 2240×3726; faster at 1000 px but unusable accuracy |
| **Image-size ceiling (important)** | this 4-bit MLX build emits **empty/degenerate** output on images ≳1800 px wide. Identity (small crops) is unaffected; **state/ground must be fed downscaled screenshots** to function. Re-test this ceiling on the production vLLM path + 8B model. |

## Honest bottom line

- **Identity veto is real** and reproduces the #28 safety claim through the wired
  path, including purely-numeric MRNs — the number that mattered most.
- **The drift-oracle rescues drift-obscured success (100 %)** but is **inert at
  native Retina** and has a **measured ~12.5 % false-rescue** on in-progress
  ambiguity — real residual risk; the client should downscale before calling.
- **The grounder does not work on a dense full-page list** at this model/scale;
  it fails safe but provides no availability there.

Sample sizes are deliberately small and every case is hand-auditable in the JSON
and in `state_fixtures.py`; treat the rates as directional, not population
estimates.
