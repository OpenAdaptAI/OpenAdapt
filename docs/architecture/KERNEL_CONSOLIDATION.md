# Kernel Consolidation and Legacy Flow Freeze Policy (STALE / SUPERSEDED)

> [!WARNING]
> **STALE DOCUMENT (Updated 2026-11-27): Wave 2 deletions and fixed LOC budgets are cancelled.**
>
> Do **not** cite this file to propose deleting `hooks/`, `txplan/`, `shadow*.py`, `agent_guard/`, `wrap/`, `flight/`, or `readiness/`, and do **not** enforce an arbitrary line-count cap on `packages/kernel/`.
>
> 1. **Wave 2 deletion list revoked:** Code inspection confirmed that `hooks/` (Claude Code and Codex `PreToolUse` / `PostToolUse` / `Stop` hooks), `txplan/` (SQL, REST, and outbound mail dry-run staging and undo), and `shadow*.py` (HTTP/FHIR proxy shadow mode and thick-client SQL shadow mode) provide non-DOM capabilities that `openadapt-kernel` does not cover. Only `form_tx/` was deleted (PR `#2715`, after failing its pre-registered benchmark threshold). No subsystem in `packages/flow/` may be removed unless (a) it failed a pre-registered benchmark threshold, or (b) every capability it provides has been implemented and verified at or above parity in `openadapt-evals`.
> 2. **Fixed LOC budget revoked:** Functionality, completeness, and reliability take priority over line-count caps. `packages/kernel/tests/test_kernel_boundaries.py` enforces architectural isolation (`zero openadapt_flow / openadapt_desktop / openadapt_capture imports` and `zero app-specific strings` in engine code), without an artificial line ceiling.

## Current Ownership Boundaries

| Tier | Location | Status | Active Rules |
| --- | --- | --- | --- |
| Core verification & simplex runtime | `packages/kernel/` (`openadapt-kernel`) | Active | Zero `openadapt-flow` imports and zero app-specific strings in engine code (`packages/kernel/tests/test_kernel_boundaries.py`). No artificial LOC ceiling. |
| Active non-DOM & wrapper subsystems | `packages/flow/openadapt_flow/{wrap,flight,readiness,hooks,txplan}/` and `shadow*.py` | Active (Keep) | Distinct capabilities (`wrap` CLI, `flight` local LLM proxy/scrubbing, `readiness` shadow scorecard, `hooks` Claude Code/Codex hooks, `txplan` SQL/REST/mail staging and undo, `shadow*.py` HTTP/SQL shadow mode). Do not delete without verified parity in `openadapt-evals`. |
| Production compatibility engine | `packages/flow/` core (`compiler/`, `runtime/`, `backends/`, `verifier.py`, `verify_run.py`, `receipt.py`, `ir.py`, `terminal_verification_v2.py`, `run_gate.py`, `rollback.py`) | Maintained | Required by `openadapt-flow`, `openadapt-cloud`, `packages/desktop`, `packages/connectors`, and `packages/verify` (including Citrix, FreeRDP, macOS, Windows UIA, and `<canvas>` backends). |
| Frozen experimental stacks | `packages/flow/openadapt_flow/{tx,agent_guard}/` | Frozen | Bug and security fixes only; no new modules or launcher imports (`scripts/check_legacy_flow_freeze.py`). `tx/` hit its pre-registered benchmark stop rule (`0/96` writes) and retains `tx/receipt.py` for legacy receipt verification. |
| Deleted Wave 1 stack | `packages/flow/openadapt_flow/form_tx/` | Deleted | Removed in PR `#2715`. Must not be re-created. |

## Mechanical CI Checks (`scripts/check_legacy_flow_freeze.py`)

`scripts/check_legacy_flow_freeze.py` runs in the `smoke` CI job and `tests/test_legacy_flow_freeze.py` against `scripts/legacy_flow_freeze_manifest.json`. It fails if a change:

1. Re-creates `packages/flow/openadapt_flow/form_tx/`.
2. Adds a new `.py` module under the remaining frozen directories (`tx/`, `agent_guard/`).
3. Adds a new top-level command to `openadapt-flow` in `packages/flow/tests/fixtures/cli_surface.json` (new top-level commands belong in `openadapt` / `openadapt-kernel`).
4. Adds a new launcher import of `tx/` or `agent_guard/` outside `openadapt/receipt_verify.py`.
5. Imports `openadapt_flow` inside `packages/kernel/src/openadapt_kernel/`.

## Historical Subsystem Audit (Why Wave 2 Deletions Were Cancelled)

| Subsystem | Source LOC | Unique Capability | Covered by `openadapt-kernel` today? | Policy |
| --- | ---: | --- | --- | --- |
| `form_tx/` | 10,623 | Earlier DOM transaction experiment (`86%` false-stop rate) | Yes | Deleted in PR `#2715` |
| `wrap/` | 9,390 | Top-level `openadapt wrap` multi-surface agent wrapper | Partial | Keep active |
| `flight/` | 5,958 | Local loopback LLM proxy with reversible PHI pseudonymization and spend/egress caps | Complementary to `egress_guard.py` | Keep active |
| `readiness/` | 4,505 | Shadow replay-to-scorecard evaluation over human runs | No | Keep active |
| `hooks/` | 2,309 | `PreToolUse` / `PostToolUse` / `Stop` hooks for Claude Code and Codex (`openadapt.systems.yaml`) | No | Keep active (Wave 2 deletion cancelled) |
| `txplan/` | 7,468 | External transaction staging (`plan`, `apply`, `undo`, `release`) for SQL, REST, and outbound mail | No | Keep active (Wave 2 deletion cancelled) |
| `shadow*.py` | 2,244 | Pass-through HTTP/FHIR proxy shadow mode and thick-client SQL shadow mode (`shadow_db.py`) | No (`ShadowObserver` is browser-extension only) | Keep active (Wave 2 deletion cancelled) |
| `agent_guard/` | 4,726 | CDP/screen approval-field session guard | Partial | Keep intact pending parity audit |
| `tx/` | 10,913 | Experimental HAR lens induction (`0/96` writes); `tx/receipt.py` verifies legacy receipts | Yes (except `tx/receipt.py`) | Frozen; keep `tx/receipt.py` compatibility |
