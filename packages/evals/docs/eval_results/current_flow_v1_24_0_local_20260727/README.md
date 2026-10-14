# Flow 1.24.0 local evidence (2026-07-27)

Measured against the exact published `openadapt-flow` 1.24.0 wheel, SHA-256
`170fdac154794292c99dc6eea6486e7a2c3fdf321bcd87976d924bccd3db4aef`, bound to
release-tagged tracked-clean source `4ca7566b73154769398d3135507060fa020aad0a`.
Zero model calls, $0.00 model cost, loopback only.

This is the `current` entry in `docs/eval_results/PUBLISHED_EVIDENCE.json`. It
supersedes `current_flow_v1_16_1_local_20260718`, which remains committed and
reproducible against the 1.16.1 wheel it was measured on.

| File | What it holds |
|---|---|
| `REPORT.md`, `results.json` | Compiled replay versus steelmanned DOM selector controls: 3 conditions x 3 arms x 3 trials, no retries |
| `COMPARISON_TO_v1_16_1.md` | Release-over-release delta, with the `clean` over-halt and its attribution called out |
| `REPRODUCE.md` | Exact commands, digests, and environment capture |
| `clean_postcondition_over_halt.json` | The counted `clean`-baseline over-halt trials |
| `theme_postcondition_over_halt.json` | Zero observations — evidence that the 1.16.1 `theme` over-halt is fixed |
| `replication/` | An independent re-run of the whole comparison |
| `transaction_probe/` | Transaction outcome taxonomy against MockMed's persistence boundary: 5 fault modes x 2 verification configurations x 3 trials |

## Headline results

**Held.** Compiled replay completed the task 3/3 in every condition, with zero
silent incorrect successes, zero wrong-target writes, zero model calls, and
$0.00 across both the comparison and its replication. `COMPLETED_UNVERIFIED` was
never reported as a production success and never billable (11 applicable runs).
No blind retry of a consequential write occurred in any of 24 applicable runs,
including commit-then-timeout. Identity coverage improved from 1/8 to 5/8 armed
clicks.

**Moved.** The `region_stable` postcondition over-halt on the `Save Encounter`
step moved out of `theme` drift and into the **`clean` no-drift baseline**,
where it fired 2/3 in both the primary run and the replication. The effect was
independently confirmed to have saved correctly every time. This is **not** an
engine regression: MockMed ships inside the Flow wheel, so the target
application also changed between the two pinned releases, and that change
(`c416b7d`, a patient banner added to the New Encounter form) is the cause. See
"Attribution" in `COMPARISON_TO_v1_16_1.md`.

**Unsound.** Uncertain delivery does **not** land in `RECONCILIATION_REQUIRED`.
When the backend commits a write and then hangs past the client timeout, the run
is labelled `HALTED_BEFORE_EFFECT` — an assertion that no business effect
occurred — while the system of record holds the write. 7 of 16 applicable runs
made a false absence claim; 14 of 23 runs claimed a proven absence for a
consequential step that reached actuation with no verification performed at all.
`RECONCILIATION_REQUIRED` was reached only where a configured verifier returned
a conflicting reading. See `transaction_probe/REPORT.md` and
`transaction_probe/invariant_violations.json`.
