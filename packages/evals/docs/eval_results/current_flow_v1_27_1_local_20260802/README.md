# Flow 1.27.1 local evidence (2026-08-02)

Measured against the exact published `openadapt-flow` 1.27.1 wheel, SHA-256
`99d8f3ef014481356f4bcfc65f694ed2fd47a75e2025c5ddfdae4bfab2194094`, bound to
release-tagged tracked-clean source `ee52def4190fc08bc3ecdee8ea28a4aae205f1d7`.
Zero model calls, $0.00 model cost, loopback only.

This is the `current` entry in `docs/eval_results/PUBLISHED_EVIDENCE.json`. It
supersedes `current_flow_v1_24_0_local_20260727`, which remains committed and
reproducible against the 1.24.0 wheel it was measured on.

| File | What it holds |
|---|---|
| `REPORT.md`, `results.json` | Compiled replay versus steelmanned DOM selector controls: 3 conditions x 3 arms x 3 trials, no retries |
| `COMPARISON_TO_v1_24_0.md` | Release-over-release delta, with both 1.24.0 defects and the engine fixes that closed them |
| `REPRODUCE.md` | Exact commands, digests, and environment capture |
| `clean_postcondition_over_halt.json` | Zero observations — evidence that the 1.24.0 `clean` over-halt is fixed |
| `theme_postcondition_over_halt.json` | Zero observations — the 1.16.1 `theme` over-halt remains fixed |
| `replication/` | An independent re-run of the whole comparison |
| `transaction_probe/` | Transaction outcome taxonomy against MockMed's persistence boundary: 5 fault modes x 2 verification configurations x 3 trials |

There is no `transaction_probe/invariant_violations.json` in this set. The
runner writes that file only when an invariant is violated, and none was.

## Headline results

**Held.** Compiled replay completed the task 3/3 in every condition, in both the
primary run and its independent replication — 18 counted compiled trials with
zero silent incorrect successes, zero wrong-target writes, zero model calls, and
$0.00. The DOM selector controls still fail 0/3 under label drift, loudly, at
the first renamed locator and before any mutation. Identity coverage is
unchanged at 5/8 applicable clicks armed.

**Fixed.** The `region_stable` postcondition over-halt that fired 2/3 in the
`clean` baseline on 1.24.0 does not occur: 0 observations in the primary run and
0 in the replication. MockMed is byte-identical between the two pinned wheels
(tree `f0736c7a`), so this is not a fixture effect. openadapt-flow `c068554`,
`fix(compiler): stop a parameter's demonstrated value becoming a pixel
invariant (#285)`, released in 1.25.0, is the named cause.

**Fixed.** The 1.24.0 "Unsound" finding is closed. Uncertain delivery now lands
in `RECONCILIATION_REQUIRED` instead of asserting a proven absence: the `timeout`
fault mode, which commits the row and then hangs past the client timeout,
reported `HALTED_BEFORE_EFFECT` in all 6 runs on 1.24.0 and reports
`RECONCILIATION_REQUIRED` in all 6 runs here. Both failing invariants now hold,
with 0 violations across 30 counted runs. The probe harness is byte-identical to
the one that produced the 1.24.0 result, so the change is entirely the engine's.
openadapt-flow `11c115c`, `fix(transaction): require positive evidence of
absence for HALTED_BEFORE_EFFECT (#280)`, released in 1.25.0.

**Not claimed.** No Flow-versus-zero-shot comparison was run; it needs a paid
model and a paid WAA environment, and the live replay path still lacks a wired
WAA evaluator. One invariant — "an outcome asserting no business effect must not
be reported when the record shows the write landed" — is reported as `vacuous`
rather than as a pass, because no counted run claimed an absence and so no run
could exercise it. A vacuous invariant proves nothing.
