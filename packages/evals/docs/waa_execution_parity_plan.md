# WAA Execution-Core Parity Plan

Date: 2026-03-06  
Scope: `openadapt-evals` infrastructure + adapter execution path

## Target Definition

Execution-core parity means our eval loop has the same practical reliability envelope as vanilla WAA for a fixed task set:

- deterministic environment reset
- stable observation/action/evaluate endpoints
- no recurring transport timeout cascades during repeated trials
- reproducible run commands and health gates

## Current State

Not there yet.

What is now in place:

- Multi-sample health gate before runs (`run_dc_eval.py`):
  - requires repeated success across `probe + screenshot + a11y + execute + evaluate`
  - defaults: `3` samples, `2` required successes
- Resume conversion for stale safe-mode checkpoints (`record_waa_demos.py`):
  - non-safe-mode resume no longer gets stuck in manual placeholder flow
  - AI step plan is regenerated from the live screen
- A11y backend adaptation and focus-check correctness were already landed in prior stabilization edits.

## Remaining Gaps vs Upstream-Style Reliability

1. Multi-hop topology risk remains (local tunnel + VM host + container).
2. Screenshot path still relies on Flask `/screenshot` endpoint.
3. UIA remains intermittently slow/unresponsive in this environment.
4. No explicit soak gate yet that blocks promotion unless repeated trials are transport-clean.

## Parity Plan (Phased)

### Phase 1: Gate and Detect (in progress)

- Keep strict pre-run health gate enabled by default.
- Add run-level pass criteria:
  - no infra fail-fast terminations
  - no transport timeout cascades in execution logs
- Artifact: per-run parity summary in `STATUS.md` weekly section.

### Phase 2: Stabilize Substrate

- Add upstream-aligned screenshot fallback path (QMP/`obs_winagent`-based where available).
- Keep win32 as default a11y backend except tasks that explicitly require UIA.
- Ensure reset path is idempotent under LibreOffice recovery edge cases.

### Phase 3: Prove with Repeats

- Core4 soak: 3 repeated trials (ZS+DC) with zero infra aborts.
- Then full hard-12 repeated trials (minimum 3/task/condition).
- Publish north-star delta from only transport-clean runs.

### Phase 4: Decision Gate

- By April 15, 2026:
  - If quantitative lift is clear and infra is stable, continue.
  - If lift is unclear, pivot to narrower retrieval-layer productization.

## Acceptance Criteria ("There Yet")

We consider execution-core parity achieved when all are true for two consecutive evaluation days:

1. Core4 repeated trials complete without infra fail-fast aborts.
2. No repeated transport timeout cascades in task execution logs.
3. Health gate passes consistently without manual tunnel/container intervention.
4. Demo recording/resume flows are deterministic (no stale safe-mode/manual-step traps).

