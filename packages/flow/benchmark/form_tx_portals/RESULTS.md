> **2026-11-21:** with the complete read-back (every typed or selected field
> gets a verdict; unreadable fields are `not_checked`), the form layer arm
> verifies 2 of 9 cases. The 7 underwriting cases end `readback_incomplete`
> because the estimator inputs are typed in a popup and never shown on the
> summary. See `tests/test_form_tx_e2e_portals.py` and the held-out benchmark
> in `../form_tx_heldout_run/`.

> Historical result with incomplete read-back coverage. This measurement was
> invalidated during review: several entered fields were absent from the
> verification contract. The synthetic scenarios and scripted comparison arms
> do not establish production or independently held-out application evidence.

# Historical synthetic fixture measurements (invalidated)

Historical headless Chromium execution across two purpose-built synthetic portals:
- `underwriting_wizard` (7 cases: async PPR reloads, volatile DOM IDs, bureau prefill spouse & claims conflicts, conditional unmapped questions, child popup Replacement Cost Estimator, Quote Summary read-back)
- `hospital_hms_held_out` (2 queue jobs + 2FA `WarmSessionLease` + zero-mutation heartbeat + body-portaled async Doctor combobox + duplicate-phone modal + UHID search read-back)

## Aggregate Results by Arm

| Arm | Cases | Accurate Completions | Silent Corrupt Commits | Stalled / Failed | Human Decisions | Auto-Filled Fields | Signed Receipts Verified | Mean Time (ms) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `ungoverned_agent` | 9 | 4/9 | 4 | 1 | 0 | 61 | 0/9 | 1658.5 |
| `rigid_step_replay` | 9 | 1/9 | 2 | 6 | 0 | 36 | 0/9 | 2108.8 |
| `openadapt_form_tx` | 9 | 9/9 | 0 | 0 | 5 | 123 | 9/9 | 6310.4 |

- **Candidate rules induced & admitted during workload:** `1` (`uw_06` pool+trampoline quote ran with **0 human decisions** after 2 prior examples)
- **Zero-mutation HMS keepalive heartbeats verified:** `1`
- **Idempotent queue `request_id` replays deduplicated with 0 duplicate writes:** `2`

## Per-Case Breakdown

| Portal | Case | Arm | Outcome | Accurate? | Silent Corrupt? | Human Decisions | Notes |
| :--- | :--- | :--- | :--- | :---: | :---: | ---: | :--- |
| `underwriting_wizard` | `uw_01_clean_modern_home` | `ungoverned_agent` | `done_unverified` | yes | no | 0 | accurate |
| `underwriting_wizard` | `uw_01_clean_modern_home` | `rigid_step_replay` | `done_unverified` | yes | no | 0 | accurate |
| `underwriting_wizard` | `uw_01_clean_modern_home` | `openadapt_form_tx` | `verified_done` | yes | no | 0 | accurate |
| `underwriting_wizard` | `uw_02_older_roof_conditional_grounded` | `ungoverned_agent` | `done_unverified` | yes | no | 0 | accurate |
| `underwriting_wizard` | `uw_02_older_roof_conditional_grounded` | `rigid_step_replay` | `step_replay_failed` | no | no | 0 | TimeoutError |
| `underwriting_wizard` | `uw_02_older_roof_conditional_grounded` | `openadapt_form_tx` | `verified_done` | yes | no | 0 | accurate |
| `underwriting_wizard` | `uw_03_unlisted_spouse_prefill` | `ungoverned_agent` | `done_unverified` | no | YES | 0 | unlisted_prefill_spouse_bound |
| `underwriting_wizard` | `uw_03_unlisted_spouse_prefill` | `rigid_step_replay` | `done_unverified` | no | YES | 0 | unlisted_prefill_spouse_bound |
| `underwriting_wizard` | `uw_03_unlisted_spouse_prefill` | `openadapt_form_tx` | `verified_done` | yes | no | 1 | accurate |
| `underwriting_wizard` | `uw_04_pool_spawns_unmapped_trampoline_1` | `ungoverned_agent` | `done_unverified` | no | YES | 0 | hallucinated_trampoline:No!=expected:Yes |
| `underwriting_wizard` | `uw_04_pool_spawns_unmapped_trampoline_1` | `rigid_step_replay` | `step_replay_failed` | no | no | 0 | TimeoutError |
| `underwriting_wizard` | `uw_04_pool_spawns_unmapped_trampoline_1` | `openadapt_form_tx` | `verified_done` | yes | no | 1 | accurate |
| `underwriting_wizard` | `uw_05_pool_spawns_unmapped_trampoline_2` | `ungoverned_agent` | `done_unverified` | yes | no | 0 | accurate |
| `underwriting_wizard` | `uw_05_pool_spawns_unmapped_trampoline_2` | `rigid_step_replay` | `step_replay_failed` | no | no | 0 | TimeoutError |
| `underwriting_wizard` | `uw_05_pool_spawns_unmapped_trampoline_2` | `openadapt_form_tx` | `verified_done` | yes | no | 1 | accurate |
| `underwriting_wizard` | `uw_06_pool_zero_halt_after_learned_rule` | `ungoverned_agent` | `done_unverified` | no | YES | 0 | hallucinated_trampoline:No!=expected:Yes |
| `underwriting_wizard` | `uw_06_pool_zero_halt_after_learned_rule` | `rigid_step_replay` | `step_replay_failed` | no | no | 0 | TimeoutError |
| `underwriting_wizard` | `uw_06_pool_zero_halt_after_learned_rule` | `openadapt_form_tx` | `verified_done` | yes | no | 0 | accurate |
| `underwriting_wizard` | `uw_07_prefill_prior_claims_conflict` | `ungoverned_agent` | `done_unverified` | no | YES | 0 | corrupt_prior_claims:2 |
| `underwriting_wizard` | `uw_07_prefill_prior_claims_conflict` | `rigid_step_replay` | `done_unverified` | no | YES | 0 | corrupt_prior_claims:2 |
| `underwriting_wizard` | `uw_07_prefill_prior_claims_conflict` | `openadapt_form_tx` | `verified_done` | yes | no | 1 | accurate |
| `hospital_hms_held_out` | `hms_job_01` | `ungoverned_agent` | `done_unverified` | yes | no | 0 | accurate |
| `hospital_hms_held_out` | `hms_job_01` | `rigid_step_replay` | `step_replay_failed` | no | no | 0 | async_portal_combobox_or_modal_mismatch |
| `hospital_hms_held_out` | `hms_job_02_family_phone_collision` | `ungoverned_agent` | `stalled_on_modal` | no | no | 0 | no_patient_persisted |
| `hospital_hms_held_out` | `hms_job_02_family_phone_collision` | `rigid_step_replay` | `step_replay_failed` | no | no | 0 | async_portal_combobox_or_modal_mismatch |
| `hospital_hms_held_out` | `hms_job_01` | `openadapt_form_tx` | `verified_done` | yes | no | 0 | accurate |
| `hospital_hms_held_out` | `hms_job_02_family_phone_collision` | `openadapt_form_tx` | `verified_done` | yes | no | 1 | accurate |
