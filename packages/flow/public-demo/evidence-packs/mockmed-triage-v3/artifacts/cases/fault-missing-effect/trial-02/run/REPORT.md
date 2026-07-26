# ❌ mockmed-triage — HALTED

- **Started:** 2026-07-26T06:34:26.343771+00:00
- **Execution profile:** `standard` (not production-eligible)
- **Required contracts passed:** authorization 1/1, identity 5/5, postcondition 8/8, effect 0/2
- **Evidence classes:** `authorization`, `identity`, `postcondition`
- **Model calls:** 0
- **External network calls:** `observed`
- **Steps:** 5/6 ok
- **Heals:** 0
- **Screenshot egress:** none observed (zero screenshots left the box)
- **Governed authorization:** `959b009deb7047bbaf84607484e799d7` (public-demo-qualified-campaign)
- **Admitted policy:** clinical-write; runtime inputs bound to `342463e1dfa0e45e5a5dac4c34a3daf2f1499aeeb2fa7eefeb8f0a9a027b3ea1`

## Parameters

| Param | Value |
| --- | --- |
| `note` | Synthetic follow-up in two weeks |

## Identity protection coverage

**5 of 5 click steps identity-armed.** Unarmed clicks proceed with **no identity verification** (see docs/LIMITS.md).

## Effect verification (system of record)

**1 of 6 executed step(s) carried a system-of-record effect contract** — 0 confirmed, 1 halted, 0 approved-unverified. Steps without a contract have only screen evidence for their local step outcome (run `openadapt-flow lint` for the bundle's per-consequential-step effect coverage).

## Steps

| # | Step | Intent | Rung | Confidence | Verified | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click 'Open' | structural | 1.00 | id ✓ | 841 |  | ✅ |
| 2 | `step_001` | click 'New Encounter' | structural | 1.00 | id ✓ | 915 |  | ✅ |
| 3 | `step_002` | click 'Triage' | structural | 1.00 | id ✓ | 509 |  | ✅ |
| 4 | `step_003` | click at (480, 268) | structural | 1.00 | id ✓ | 474 |  | ✅ |
| 5 | `step_004` | type <note> | &mdash; | &mdash; | input ✓ | 550 |  | ✅ |
| 6 | `step_005` | click 'Save Encounter' | structural | 1.00 | id ✓, effect ✗ | 5945 |  | ❌ |

## Per-step evidence

Every step below shows the frame **before** and **after** the action next to the resolution rung, the identity-gate and effect-check verdicts, and whether the step healed or halted. The generator links only retained run artifacts and never synthesizes pixels. If image redaction was enabled when a frame was persisted, that redaction is already burned into its pixels; a frame the run did not retain is marked _not retained_.

### 1. `step_000` — click 'Open'

**Rung** `structural` (conf 1.00, resolved (776, 186)) · **Gates** id ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_000 before](steps/step_000_before.png) | ![step_000 after](steps/step_000_after.png) |

### 2. `step_001` — click 'New Encounter'

**Rung** `structural` (conf 1.00, resolved (114, 159)) · **Gates** id ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_001 before](steps/step_001_before.png) | ![step_001 after](steps/step_001_after.png) |

### 3. `step_002` — click 'Triage'

**Rung** `structural` (conf 1.00, resolved (85, 214)) · **Gates** id ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_002 before](steps/step_002_before.png) | ![step_002 after](steps/step_002_after.png) |

### 4. `step_003` — click at (480, 268)

**Rung** `structural` (conf 1.00, resolved (480, 268)) · **Gates** id ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_003 before](steps/step_003_before.png) | ![step_003 after](steps/step_003_after.png) |

### 5. `step_004` — type <note>

**Rung** &mdash; (keyboard / wait step, no anchor) · **Gates** input ✓ · **Heal** none · **Outcome** ✅ ok

| Before | After |
| --- | --- |
| ![step_004 before](steps/step_004_before.png) | ![step_004 after](steps/step_004_after.png) |

### 6. `step_005` — click 'Save Encounter' (final step, halted)

> ❌ **Error:** System-of-record effect verification HALTED step 'step_005' (click 'Save Encounter'): record_written refuted against the rest system of record and could not be reconciled (escalated) — [rest] record_written: 0 records match the target selector, expected 1 (missing / phantom / rejected write -- the screen may show success but nothing landed) -- no compensator available for an irreversible refuted effect -- durably halt and escalate — run aborted

**Rung** `structural` (conf 1.00, resolved (134, 457)) · **Gates** id ✓, effect ✗ · **Heal** none · **Outcome** ❌ HALTED (governed refusal)

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
| `structural` | 4 | ████ |

## Totals

| Metric | Value |
| --- | --- |
| Total time | 9242 ms |
| Steps ok | 5/6 |
| Heals | 0 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |
