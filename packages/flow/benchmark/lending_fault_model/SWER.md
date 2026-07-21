# Lending (MockLoan) Silent Wrong-Effect Rate - screen-only vs effect-verified

The non-healthcare replication of the SWER headline on a second system of
record. Judged by an independent `RestRecordVerifier` reading the MockLoan ledger
at `GET /api/db` (a path the SPA never calls), never the screen or the agent's
self-report. Every trial binds a trial-unique memo + idempotency key. Zero model
calls.

**The gate (AGENTS.md safety asymmetry): the only dangerous error is a SILENT
WRONG-EFFECT - reporting/rendering success while the ledger disagrees. It must be
~0 under effect verification.** Over-halt (halting when the write was actually
fine) is the safe error; it is reported as the availability cost.


Generated: 2026-07-21 01:26:41  
Platform: Darwin arm64 py3.12.7  
Tasks: 11 (all seven divergence categories + clean / idempotent controls).  
Trials per task per arm: 3.  

## Headline

| arm | episodes | SWER | over-halt | task success | screen success | success-effect gap |
|---|---|---|---|---|---|---|
| `screen_only` | 33 | **0.6363636363636364** (21/33) | 0.0 | 0.18181818181818182 | 0.8181818181818182 | 0.636 |
| `effect_verify` | 33 | **0.0** (0/33) | 0.18181818181818182 | 0.2727272727272727 | 0.2727272727272727 | 0.0 |

- **Screen-only SWER = 0.6363636363636364** (21/33): the injected transactional faults render a clean 'Disbursement authorized' banner while the ledger is wrong (a partial/phantom/duplicate/lost-update/wrong-loan write).
- **Effect-verified SWER = 0.0** (0/33): reading the true effect from the ledger collapses the silent-wrong-effect rate; the residual cost is over-halt = 0.18181818181818182 (safe: a human finishes a recoverable case).
- **Success-effect gap** shrinks from 0.636 (screen-only) to 0.0 (effect-verified).

## Per-outcome counts

| arm | false_abort | over_halt | safe_halt | silent_wrong_effect | success | wrong_action |
|---|---|---|---|---|---|---|
| `screen_only` | 3 | 0 | 3 | 21 | 6 | 0 |
| `effect_verify` | 0 | 6 | 6 | 0 | 9 | 12 |

## Method / oracle independence

Both arms drive the SAME writes against the SAME fault server; only `reported_success` differs. The independent oracle handed to `score_episode` is a `RestRecordVerifier` reading `/api/db` pre-action and post-action, and it is a DISTINCT instance from the `effect_verify` arm's own verifier - the arm cannot influence the judge. The C6 task seeds a same-name decoy loan and funds it; the intended loan stays empty, so a blind (identity-less) write is a silent wrong-effect under `screen_only` and an over-halt (caught, safe) under `effect_verify` - the identity gate on the consequential step.

## Reproduce

```
python -m benchmark.lending_fault_model.swer
```

