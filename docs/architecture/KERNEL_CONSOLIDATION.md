# Kernel Consolidation and Legacy Flow Freeze Policy

## Decision

`openadapt-kernel` (`packages/kernel/`) is the canonical core runtime for new commit-boundary verification, full-DOM macro staging, stepwise live-DOM healing, cross-rail read-back oracles, reversible pre-click probing, online ACI calibration, and shadow-to-autopilot execution.

To prevent `openadapt-kernel` from becoming an eighth parallel stack alongside `packages/flow`, unused or superseded guard and transaction stacks in `packages/flow` are frozen or deleted (`form_tx/` deleted in Wave 1). `openadapt wrap`, `openadapt flight`, and `readiness/` (shadow scorecard) remain active subsystems.

## Ownership Boundaries

| Tier | Location | Status | Rules |
| --- | --- | --- | --- |
| Canonical runtime | `packages/kernel/` (`openadapt-kernel`) | Active | `< 3,200` LOC budget, zero `openadapt-flow` imports, zero app-specific strings (enforced by `packages/kernel/tests/test_kernel_boundaries.py`). Exposed via `openadapt kernel ...` and lazy imports on `openadapt`. |
| Active flow subsystems | `packages/flow/openadapt_flow/{wrap,flight,readiness}/` | Active | Supported subsystems (`openadapt wrap` whose internals are migrating onto `openadapt-kernel`, `openadapt flight` metering/scrubbing/egress, and `readiness/` shadow scorecard). Not frozen. |
| Production compatibility engine | `packages/flow/` core (`compiler/`, `runtime/`, `backends/`, `verifier.py`, `verify_run.py`, `receipt.py`, `ir.py`, `terminal_verification_v2.py`, `run_gate.py`, `rollback.py`) | Maintained | Required by `openadapt-flow 4.0.0`, `openadapt-cloud` Modal runner, `packages/desktop`, `packages/connectors`, and `packages/verify`. Keep existing contracts working; do not add new top-level CLI subcommands. |
| Frozen overlapping stacks | `packages/flow/openadapt_flow/{tx,txplan,agent_guard,hooks}/` and `shadow*.py` | Frozen | Bug and security fixes only. No new Python modules, no new top-level `openadapt-flow` CLI commands, and no new imports from `openadapt/`. Deleting files or whole stacks is allowed. |
| Deleted Wave 1 stack | `packages/flow/openadapt_flow/form_tx/` | Deleted | Removed in Wave 1. Must not be re-created or imported from `openadapt/`. |

## Enforcement

`scripts/check_legacy_flow_freeze.py` runs in the required `smoke` CI job (`.github/workflows/ci.yml`) and in `tests/test_legacy_flow_freeze.py` against `scripts/legacy_flow_freeze_manifest.json`. It fails if a change:

1. Re-creates any deleted legacy stack (`form_tx/`) under `packages/flow/openadapt_flow/`.
2. Adds a new `.py` module under any frozen legacy directory (`tx/`, `txplan/`, `agent_guard/`, `hooks/`) or adds a new `shadow*.py` file at `packages/flow/openadapt_flow/`.
3. Adds a new top-level command to `openadapt-flow` in `packages/flow/tests/fixtures/cli_surface.json` (frozen at 95 commands).
4. Adds a new import of a frozen or deleted legacy stack from `openadapt/` outside the existing allowlist (`openadapt/cli.py` for `hooks`/`txplan`, `openadapt/receipt_verify.py` for `tx`).
5. Imports `openadapt_flow` from `packages/kernel/src/openadapt_kernel/`.

## Deletion Tracker

None of the stacks below are imported by `openadapt-cloud`. Wave 1 deleted `form_tx/`; `wrap/`, `flight/`, and `readiness/` remain active subsystems; remaining frozen stacks are scheduled for later waves after review or decoupling.

| Stack | Source LOC | Test LOC | Total LOC | `openadapt-cloud` imports | Callers outside `packages/flow` | Status / Next action |
| --- | ---: | ---: | ---: | --- | --- | --- |
| `form_tx/` | 10,623 | 11,509 | 22,132 | None | None | **Deleted (Wave 1)** |
| `agent_guard/` | 4,726 | 5,960 | 10,686 | None | None | Frozen; keep intact pending review with `flight/` and `readiness/` |
| `txplan/` | 7,468 | 4,872 | 12,340 | None | `openadapt/cli.py` (`plan`/`proxy`), `tests/test_proxy_plan_commands.py` | Frozen; delete in next major launcher release (`openadapt` 2.0 / `openadapt-flow` 5.0) |
| `hooks/` | 2,309 | 1,060 | 3,369 | None | `openadapt/cli.py` (`hooks`) | Frozen; delete in next major release or fold into `openadapt-kernel` |
| `shadow*.py` | 2,244 | 4,309 | 6,553 | None | `packages/verify/src/openadapt_verify/shadow.py` | Frozen; replace `openadapt_verify.shadow` facade with `openadapt_kernel.ShadowObserver`, then delete |
| `tx/` | 10,913 | 12,111 | 23,024 | None | `openadapt/receipt_verify.py` (`tx.receipt`), `packages/verify/src/openadapt_verify/tx.py` | Frozen; extract `tx/receipt.py` verifier if legacy receipt verification is still required, then delete remainder |
| `wrap/` | 9,390 | 12,750 | 22,140 | None | `openadapt/cli.py` (`wrap`) and launcher wrap tests | **Active (un-frozen)** — top-level `openadapt wrap` verb; internals migrating onto `openadapt-kernel` |
| `flight/` | 5,958 | 6,302 | 12,260 | None | `openadapt/cli.py` (`flight`), `tests/test_cli_flight.py` | **Active (un-frozen)** — `openadapt flight` metering, scrubbing, and egress guard |
| `readiness/` | 4,505 | 4,062 | 8,567 | None | None | **Active (un-frozen)** — shadow scorecard and readiness harness |
