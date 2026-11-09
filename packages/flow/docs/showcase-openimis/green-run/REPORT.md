# ✅ openimis-eligibility-check — success

- **Started:** 2026-07-21T10:11:54.210861+00:00
- **Steps:** 6/6 ok
- **Heals:** 0
- **Data egress:** none — fully local replay (zero screenshots left the box)

## Parameters

| Param | Value |
| --- | --- |
| `insurance_no` | 999000003 |
| `service_code` | A1 |
| `as_of_date` | 2026-07-21 |

## Identity protection coverage

**3 of 3 click steps identity-armed.** Unarmed clicks proceed with **no identity verification** (see docs/LIMITS.md).

## Effect verification (system of record)

**1 of 6 executed step(s) carried a system-of-record effect contract** — 1 confirmed, 0 halted, 0 approved-unverified. Steps without a contract have only screen evidence for their local step outcome (run `openadapt-flow lint` for the bundle's per-consequential-step effect coverage).

## Steps

| # | Step | Intent | Rung | Confidence | Verified | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click 'Q Insuree enquiry ?' | structural | 1.00 | id ✓ | 1152 |  | ✅ |
| 2 | `step_001` | type <insurance_no> | &mdash; | &mdash; | input ✓ | 791 |  | ✅ |
| 3 | `step_002` | press Enter | &mdash; | &mdash; | &mdash; | 1255 |  | ✅ |
| 4 | `step_003` | click at (323, 626) | structural | 1.00 | id ✓ | 1129 |  | ✅ |
| 5 | `step_004` | type 'General' | &mdash; | &mdash; | input ✓ | 1428 |  | ✅ |
| 6 | `step_005` | click at (351, 673) | structural | 1.00 | id ✓, effect ✓ | 1072 |  | ✅ |

## Per-step evidence

Every step below shows the frame **before** and **after** the action next to the resolution rung, the identity-gate and effect-check verdicts, and whether the step healed or halted. The generator links only retained run artifacts and never synthesizes pixels. If image redaction was enabled when a frame was persisted, that redaction is already burned into its pixels; a frame the run did not retain is marked _not retained_.

### 1. `step_000` — click 'Q Insuree enquiry ?'

**Rung** `structural` (conf 1.00, resolved (859, 63)) · **Gates** id ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_000 before](steps/step_000_before.png) | ![step_000 after](steps/step_000_after.png) |

### 2. `step_001` — type <insurance_no>

**Rung** &mdash; (keyboard / wait step, no anchor) · **Gates** input ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_001 before](steps/step_001_before.png) | ![step_001 after](steps/step_001_after.png) |

### 3. `step_002` — press Enter

**Rung** &mdash; (keyboard / wait step, no anchor) · **Gates** none on this step · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_002 before](steps/step_002_before.png) | ![step_002 after](steps/step_002_after.png) |

### 4. `step_003` — click at (323, 626)

**Rung** `structural` (conf 1.00, resolved (323, 627)) · **Gates** id ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_003 before](steps/step_003_before.png) | ![step_003 after](steps/step_003_after.png) |

### 5. `step_004` — type 'General'

**Rung** &mdash; (keyboard / wait step, no anchor) · **Gates** input ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_004 before](steps/step_004_before.png) | ![step_004 after](steps/step_004_after.png) |

### 6. `step_005` — click at (351, 673) (final step)

**Rung** `structural` (conf 1.00, resolved (351, 673)) · **Gates** id ✓, effect ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_005 before](steps/step_005_before.png) | ![step_005 after](steps/step_005_after.png) |

## Rung histogram

| Rung | Count | |
| --- | --- | --- |
| `template` | 0 |  |
| `template_global` | 0 |  |
| `ocr` | 0 |  |
| `geometry` | 0 |  |
| `grounder` | 0 |  |
| `structural` | 3 | ███ |

## Totals

| Metric | Value |
| --- | --- |
| Total time | 6827 ms |
| Steps ok | 6/6 |
| Heals | 0 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |
