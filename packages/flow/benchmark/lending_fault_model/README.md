# Lending (MockLoan) - the second-domain effect-verification study

This directory answers a specific generalizability question: **does the governed
record -> compile -> replay + effect-verification method hold beyond healthcare?**
All prior transactional evidence (`benchmark/fault_model`,
`benchmark/effect_readback`, `benchmark/silent_wrong_action`) ran against
**MockMed**, a clinical fixture. This study replicates the same rigorous method
on a **second, non-healthcare system of record with a distinct UI**: **MockLoan**
(`openadapt_flow/mockloan/`), a loan-origination console whose consequential
write **authorizes a disbursement of funds to a borrower** - an irreversible
money-movement write.

Nothing here is a synthetic-fixture shortcut of the measurement: every number is
produced by the REAL `Replayer` and/or the shared EffectBench scoring contract,
judged by an independent ledger read, with zero model calls.

## The fixture

- `openadapt_flow/mockloan/` - a self-contained, deterministic static SPA
  (login -> funding pipeline -> loan -> new disbursement -> **Authorize
  Disbursement**), a distinct finance UI from MockMed. All data is fake/synthetic
  (no real PII).
- `openadapt_flow/mockloan/fault_server.py` - adds a real persistence boundary
  (an in-process **ledger**) and a flag-gated `?fault=<mode>` hook that mirrors
  MockMed's. **With no `?fault` query the app never calls the API** and the
  normal benchmark is byte-for-byte unaffected (pinned by a test). The ledger is
  the independent GROUND TRUTH, read at `GET /api/db` - a path the SPA itself
  never calls.

## The three measurements (real numbers in the generated files)

1. **Governed loop under fault injection (real Replayer, screen-only baseline)**
   - `run.py` records + compiles ONE disbursement bundle and replays it through
   the REAL `Replayer` under every transactional fault, judged by the ledger.
   -> `results.json`, `LENDING_FAULT_MODEL.md`.
   Headline: the screen-only replay **silently mishandles 5 of the 7**
   transactional fault classes (partial / duplicate / optimistic / stale /
   double), exactly reproducing the MockMed result on a different domain.

2. **Silent Wrong-Effect Rate, screen-only vs effect-verified** - `swer.py`
   scores the same faults through the shared EffectBench contract
   (`openadapt_flow.benchmark.effectbench.score_episode` + `summarize`) under two
   arms. -> `swer_results.json`, `SWER.md`.
   Headline: **screen-only SWER collapses to 0.0 under effect verification**; the
   residual cost is over-halt (safe). Includes a C6 wrong-record / homonym task
   that exercises the **identity gate on the consequential step**.

3. **Resolution-ladder behavior** - `resolution_ladder.py` replays the clean
   write under a template-breaking `?drift=theme` and a `?drift=rename`. ->
   `resolution_ladder_results.json`, `RESOLUTION_LADDER.md`.
   Headline: the **full deterministic ladder** (structural + template + OCR +
   geometry) recovers cosmetic drift **model-free**; the stricter template-only
   rung **halts before the money-movement step** rather than acting on a
   low-confidence resolve. Neither ever books a wrong disbursement.

## Oracle independence (same design as the sibling EffectBench effort)

The judge never trusts the screen or the agent's self-report. It reads pre/post
system-of-record state from the ledger (`GET /api/db`), a channel the SPA does
not call. In the two-arm SWER the benchmark oracle is a DISTINCT
`RestRecordVerifier` instance from the `effect_verify` arm's own verifier, so the
arm cannot influence the judge. Every trial binds a **trial-unique** memo (and
idempotency key), so the oracle checks THIS run's exact write and cross-trial
contamination is detectable. This is the same non-gameable, pre/post-against-the
system-of-record method the MockMed anchor uses - applied to a second domain.

## Reproduce

```bash
python -m benchmark.lending_fault_model.run                # real Replayer study
python -m benchmark.lending_fault_model.swer               # two-arm SWER
python -m benchmark.lending_fault_model.resolution_ladder  # drift ladder
```

CI coverage:

- `tests/test_lending_swer.py` - fast, no browser: the taxonomy, the ledger, and
  a LIVE two-arm SWER assertion (screen-only silently wrong; effect-verified 0).
  Runs in the required `test` gate.
- `tests/test_mockloan.py` - the app screens + the inert-without-`?fault` pin.
- `tests/e2e/test_lending_fault_model.py` - the real-`Replayer` study end to end,
  plus the resolution-ladder recovery. Runs in the required `e2e-browser` gate.

## Licensing / packaging

MockLoan is first-party, MIT, fully synthetic. It ships in the wheel under
`openadapt_flow/` like MockMed; this `benchmark/` directory is not packaged into
the wheel. No third-party or copyleft material is copied, vendored, or embedded.
