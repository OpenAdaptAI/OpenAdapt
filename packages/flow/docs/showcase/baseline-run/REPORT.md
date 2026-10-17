# ✅ mockmed-triage — success

- **Started:** 2026-07-06T18:29:51.632497+00:00
- **Steps:** 11/11 ok
- **Heals:** 0
- **Data egress:** none — fully local replay (zero screenshots left the box)

## Parameters

| Param | Value |
| --- | --- |
| `note` | Showcase triage booking three months |

## Identity protection coverage

_No identity-applicable (anchored click/type) steps in this workflow._

## Effect verification (system of record)

_No executed step carried a system-of-record effect contract — every write on this run was verified from screen evidence only. Run `openadapt-flow lint` to see the bundle's consequential-step effect coverage._

## Steps

| # | Step | Intent | Rung | Confidence | Verified | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click at (214, 195) | template | 1.00 | &mdash; | 372 |  | ✅ |
| 2 | `step_001` | type 'nurse.demo' | &mdash; | &mdash; | &mdash; | 553 |  | ✅ |
| 3 | `step_002` | click at (214, 264) | template | 1.00 | &mdash; | 341 |  | ✅ |
| 4 | `step_003` | type 'mockmed-demo-pass' | &mdash; | &mdash; | &mdash; | 324 |  | ✅ |
| 5 | `step_004` | click 'Sign In' | template | 1.00 | &mdash; | 627 |  | ✅ |
| 6 | `step_005` | click 'Open' | template | 1.00 | &mdash; | 565 |  | ✅ |
| 7 | `step_006` | click 'New Encounter' | template | 1.00 | &mdash; | 546 |  | ✅ |
| 8 | `step_007` | click 'Triage' | template | 1.00 | &mdash; | 337 |  | ✅ |
| 9 | `step_008` | click at (344, 290) | template | 1.00 | &mdash; | 334 |  | ✅ |
| 10 | `step_009` | type <note> | &mdash; | &mdash; | &mdash; | 343 |  | ✅ |
| 11 | `step_010` | click 'Save Encounter' | template | 1.00 | &mdash; | 640 |  | ✅ |

## Per-step evidence

Every step below shows the frame **before** and **after** the action next to the resolution rung, the identity-gate and effect-check verdicts, and whether the step healed or halted. The generator links only retained run artifacts and never synthesizes pixels. If image redaction was enabled when a frame was persisted, that redaction is already burned into its pixels; a frame the run did not retain is marked _not retained_.

### 1. `step_000` — click at (214, 195)

**Rung** `template` (conf 1.00, resolved (214, 195)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_000 before](steps/step_000_before.png) | ![step_000 after](steps/step_000_after.png) |

### 2. `step_001` — type 'nurse.demo'

**Rung** &mdash; (keyboard / wait step, no anchor) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_001 before](steps/step_001_before.png) | ![step_001 after](steps/step_001_after.png) |

### 3. `step_002` — click at (214, 264)

**Rung** `template` (conf 1.00, resolved (214, 264)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_002 before](steps/step_002_before.png) | ![step_002 after](steps/step_002_after.png) |

### 4. `step_003` — type 'mockmed-demo-pass'

**Rung** &mdash; (keyboard / wait step, no anchor) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_003 before](steps/step_003_before.png) | ![step_003 after](steps/step_003_after.png) |

### 5. `step_004` — click 'Sign In'

**Rung** `template` (conf 1.00, resolved (119, 324)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_004 before](steps/step_004_before.png) | ![step_004 after](steps/step_004_after.png) |

### 6. `step_005` — click 'Open'

**Rung** `template` (conf 1.00, resolved (775, 186)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_005 before](steps/step_005_before.png) | ![step_005 after](steps/step_005_after.png) |

### 7. `step_006` — click 'New Encounter'

**Rung** `template` (conf 1.00, resolved (114, 159)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_006 before](steps/step_006_before.png) | ![step_006 after](steps/step_006_after.png) |

### 8. `step_007` — click 'Triage'

**Rung** `template` (conf 1.00, resolved (85, 160)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_007 before](steps/step_007_before.png) | ![step_007 after](steps/step_007_after.png) |

### 9. `step_008` — click at (344, 290)

**Rung** `template` (conf 1.00, resolved (344, 290)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_008 before](steps/step_008_before.png) | ![step_008 after](steps/step_008_after.png) |

### 10. `step_009` — type <note>

**Rung** &mdash; (keyboard / wait step, no anchor) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_009 before](steps/step_009_before.png) | ![step_009 after](steps/step_009_after.png) |

### 11. `step_010` — click 'Save Encounter' (final step)

**Rung** `template` (conf 1.00, resolved (134, 391)) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_010 before](steps/step_010_before.png) | ![step_010 after](steps/step_010_after.png) |

## Rung histogram

| Rung | Count | |
| --- | --- | --- |
| `template` | 8 | ████████ |
| `template_global` | 0 |  |
| `ocr` | 0 |  |
| `geometry` | 0 |  |
| `grounder` | 0 |  |

## Totals

| Metric | Value |
| --- | --- |
| Total time | 4981 ms |
| Steps ok | 11/11 |
| Heals | 0 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |
