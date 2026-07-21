# End-to-end silent-wrong-effect rate (through the real replayer)

Date: 2026-07-21. This is the genuinely independent, end-to-end measurement of the
silent-wrong-effect (SWER) result. Unlike the definitional
`openadapt_flow.benchmark.silent_wrong_action` (raw `requests.post`; effect
verifier and ground truth read the SAME in-process object; the effect contract
restates the ground-truth definition -- so its `0/90` is circular by
construction), every write below goes through the ACTUAL governed replay path
(`Replayer` -> `ApiActuator` -> a real HTTP write to an on-disk SQLite system of
record), and the three judgments come from three genuinely distinct paths.
9 trials per fault per arm. Zero model calls, localhost only.

## The three independent paths (why this is not circular)

1. **WRITE** -- POST /api/encounter via the replayer's ApiActuator (governed actuation) -> on-disk SQLite mutation.
2. **EFFECT VERIFIER read-back** -- GET /api/records (screen arm: GET /api/ui/last-save) -- a different HTTP verb/endpoint/connection than the write; the replayer's effect_verifier CONFIRMS/REFUTES/HALTs.
3. **GROUND TRUTH** -- direct read-only SQLite file connection over every persisted system-of-record table (discovered dynamically from sqlite_master, not a hardcoded pair; the UI-echo banner excluded), with an independent classifier and its OWN per-table delta audit (NOT the effect kit's audit_table_deltas) -- bypasses the service; the write's success flag never reaches it.

The write and the verifier both traverse the record service but via different
HTTP methods, handlers, and connections; the ground truth bypasses the service
entirely and reads storage directly, so a bug or lie in the service's read
handler cannot fool it, and the write's HTTP success flag never reaches either
the verifier or the ground truth. The verifier evaluates typed `Effect`
contracts via `judge_records`; the ground truth uses its own before/after row
classifier plus its OWN per-table delta audit (NOT the effect kit's
`audit_table_deltas`) -- a different code path that does not restate the
verifier's contract. The ground truth audits every persisted system-of-record
table it discovers in the database (from `sqlite_master`), not a hardcoded pair,
excluding only the app's own `banner` echo (the surface the screen oracle reads).

## Headline (measured end-to-end)

These are counts over a small, hand-authored, DETERMINISTIC fault taxonomy
(9 identical replays per fault per arm, variance approx. 0), not a sampled
population rate -- read the table as a coverage matrix, not an incidence
estimate (see "How to read these numbers" below).

| arm | silent-wrong-effect rate | undetected-wrong rate | wrong effects caught | false-abort rate |
|---|---|---|---|---|
| screen-verify (banner) | **60.0%** (54/90) | 75.0% | 18 | 50.0% (9) |
| effect-verify (REST record oracle) | **10.0%** (9/90) | 12.5% | 63 | 50.0% (9) |
| effect-verify (complete SQL read path) | **0.0%** (0/90) | 0.0% | 72 | 50.0% (9) |

- **silent-wrong-effect rate** = fraction of ALL runs where the independent
  ground truth says a WRONG effect persisted AND the arm still reported success
  (the wrong write would go undetected).
- **undetected-wrong rate** = P[reported success | a wrong effect actually
  occurred] -- the apples-to-apples oracle comparison.
- **false-abort rate** = P[the arm halted | the effect was actually correct].

**The realistic headline is the ladder, and the middle rung is the number a
real deployment ships.** A production deployment typically stands up ONE
out-of-band record oracle over the surface the workflow touches -- the
`effect-verify (REST record oracle)` arm -- and that arm's residual silent-wrong
rate is 10.0%
(9/90), driven
entirely by the collateral-write class. The
`effect-verify (complete SQL read path)` arm reaches
0.0%
(0/90) ONLY
because its read path is widened to cover every mutable surface -- the
least-realistic, most-instrumented deployment. Do not read the `0%` as the
expected field result; the honest field number is the middle rung.

## Per-fault, per-arm outcome

| scenario | ground-truth effect | screen-verify (banner) | effect-verify (REST record oracle) | effect-verify (complete SQL read path) |
|---|---|---|---|---|
| `ok` | correct | clean pass | clean pass | clean pass |
| `no_persist` | WRONG (absent) | SILENT WRONG | caught (halt) | caught (halt) |
| `partial` | WRONG (partial) | SILENT WRONG | caught (halt) | caught (halt) |
| `duplicate` | WRONG (duplicate) | SILENT WRONG | caught (halt) | caught (halt) |
| `wrong_record` | WRONG (wrong_record) | SILENT WRONG | caught (halt) | caught (halt) |
| `stale` | WRONG (collateral_loss) | SILENT WRONG | caught (halt) | caught (halt) |
| `collateral_unaudited` | WRONG (collateral_write) | SILENT WRONG | SILENT WRONG | caught (halt) |
| `optimistic` | WRONG (absent) | caught (halt) | caught (halt) | caught (halt) |
| `session` | WRONG (absent) | caught (halt) | caught (halt) | caught (halt) |
| `timeout` | correct | false-abort | false-abort | false-abort |

## What slips through, and why (reported honestly)

- **screen-verify** misses every 2xx-but-wrong persistence fault: the banner is
  painted regardless of what landed, so it silently accepts phantom, partial,
  duplicate, wrong-record, stale, and collateral writes.
- **effect-verify (REST record oracle)** catches the record-surface faults by
  reading the encounters record out of band -- but it silently accepts
  `collateral_unaudited`: a collateral write to the **billing** surface its read path does
  not cover. This is not a bug in effect verification; it is the structural
  limit of an out-of-band oracle -- **it catches exactly what its read path can
  read.** The independent ground truth catches it via a full table-delta audit.
- **effect-verify (complete SQL read path)** closes that gap by auditing every
  mutable surface (read-only SQL over encounters AND billing), driving the
  end-to-end silent-wrong-effect rate to
  0.0%
  (0/90) -- but
  see the closed-world caveat immediately below: that `0` is "zero within the
  audited system of record," not an absolute zero.

The `optimistic` (409), `session` (401), and `timeout` (unknown outcome)
classes are handled by the actuation layer's no-double-write contract in BOTH
arms, before any oracle is consulted -- so they do not differentiate the
oracles. `timeout` commits the row server-side yet the governed actuator HALTs
(the outcome is unknown to the client), which is a safe false-abort, not a
silent wrong effect.

## The `0` is zero in a closed world (load-bearing caveat)

The independent ground truth audits every persisted table it finds in the
SQLite system of record (dynamically, from `sqlite_master`), so it is open-world
over that database -- a collateral write to any surface, even one added later,
is caught. Two honest limits remain, and the `effect_full` `0` is conditioned on
both:

1. **Outside the database is invisible.** An effect that lands OUTSIDE this
   SQLite system of record -- an outbound HL7 or message-queue publish, a
   filesystem side-channel, a downstream service call -- is seen by neither the
   `effect_full` read path nor the ground truth. No in-database audit can see
   it. So `0/90` means "zero silent-wrong-effects within the audited SQLite
   system of record," not "zero silent-wrong-effects" in the absolute.
2. **Shared specification, not shared code.** The ground truth and the effect
   contract read through independent code and independent connections, but they
   encode the SAME business intent (the target patient/type and the intended
   note). A fault class no one thought to define is invisible to all three
   paths. Independence of code and read path is not independence of
   specification.

The realistic, foregrounded result is therefore the middle rung
(9/90 residual
under one out-of-band oracle); the `0` is the best case under a complete
in-database read path, in a closed world.

## How to read these numbers (deterministic, not sampled)

Every run here is localhost, model-call-free, and deterministic: a given
(arm x fault) produces the same outcome every repeat, so the 9 repeats have
approximately ZERO sampling variance. These counts are a COVERAGE MATRIX over a
small, hand-authored fault taxonomy (the differentiating middle block is a
handful of classes; the `effect_rest`-vs-`effect_full` gap rests on exactly one,
`collateral_unaudited`), NOT an estimate of a population incidence rate. We do
not report confidence intervals because they would be vacuous on variance-0 data.
The taxonomy is an adversarial, transaction-fault lineage chosen to differentiate
oracles; it is NOT weighted to any measured real-world EMR/lending incident
distribution, so a rate like the screen arm's should be read as "fault coverage
under this taxonomy," never as an expected production frequency.

## Reproduce

```
python -m benchmark.effect_e2e.run --n 9
```

Serves a local SQLite-backed record service, drives each fault end-to-end
through the real replayer under all three arms, and judges every run by the
independent ground truth. `$0`, no network beyond localhost, no model calls.
The claims are pinned in CI by `tests/test_effect_e2e_harness.py`.
