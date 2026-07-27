# O2C reconciliation benchmark (two systems + spreadsheet write-back)

Synthetic, deterministic, multi-system workflow benchmark. Not a customer
environment, not publication evidence of production reliability.

## The workflow (one governed run, healthy path)

Order-to-cash billing reconciliation across TWO separate local applications
plus two spreadsheet surfaces on disk:

1. **Intake** (deterministic compare pre-pass, disclosed below): read system
   A's nightly EXPORT spreadsheet (`billing_export.csv`, dropped in a shared
   folder by the billing fixture), read system B's ledger API, and derive one
   worklist row per order with a disposition: `match`, `adjust` (with the
   signed delta and the observed prior amount), or `missing`.
2. **Per order** (workflow-program LOOP body, executed by the real
   `Replayer` through the api actuation tier):
   - **BRANCH** three ways on disposition:
     - `adjust`: enter the billing adjustment in the ledger through the **UI
       gateway** (this system has no adjustment API; `POST
       /api/adjustment/new` answers 405), carrying optimistic-concurrency
       fields (`expected_prior`), then mark reconciled (API),
     - `match`: mark reconciled (API),
     - `missing`: route to an **explicit halt terminal** (a billed order with
       no ledger entry is a human-review case; the workflow never
       auto-creates a ledger record),
   - write the order's result row back to the results spreadsheet on system A
     (`recon_results.csv`), verified by RE-READING the file from disk.
3. A summary row in the results sheet, then a success terminal.

Healthy-path shape: 10 worklist orders (5 match, 5 adjust), **26 executed
consequential actions**, 2 applications, 5 input/output modalities (CSV in,
CSV out, REST API, UI gateway, two SQLite systems of record), a 3-way branch,
plus 4 designed exception scenarios.

## Arms

- `naive`: demo profile; every write "verified" only against the
  applications' own painted acknowledgement banners.
- `governed`: sealed bundle admitted by the real Standard-profile run gate,
  with the resulting single-use authorization bound to the exact inputs;
  exact API identity contracts (order id plus customer name quorum on the
  adjustment write); persisted-state verification per surface (read-only SQL
  over the ledger SQLite file; the results CSV re-read from disk).

## Scenarios and measured outcomes (n=3 per cell, deterministic)

| scenario | naive (banner oracle) | governed |
|---|---|---|
| `healthy` | completes; `COMPLETED_UNVERIFIED` (never billable) | **`VERIFIED`** (billable) |
| `missing_in_ledger` | processes the prior order, then halts at the explicit terminal | same; `HALTED_BEFORE_EFFECT`; no ledger entry auto-created |
| `ambiguous_duplicate` | safe halt (UI gateway refuses the 2-entry order) | safe halt; `RECONCILIATION_REQUIRED` (the request was sent and no verifier read the ledger, so absence is unproven); both entries untouched per ground truth |
| `stale_snapshot` | safe halt (optimistic-concurrency 409) | safe halt; `RECONCILIATION_REQUIRED` (the request was sent and no verifier read the ledger, so absence is unproven); amount unchanged per ground truth |
| `phantom_writeback` | **SILENT WRONG** (row acknowledged, never written to the sheet) | **caught** by re-reading the file; halts |

Headline (30 base runs: 15 per arm): governed silent-incorrect-success
**0/15**, healthy-path over-halts **0/3**, model calls **0**; naive
silent-incorrect-success
**3/3** on the phantom write-back. Ground truth opens both SQLite files and
both CSV files through a separate read path, derives expectations from the
immutable source seeds rather than the compare worklist, enforces allowed
record transitions across every non-echo table, and verifies the export was
never mutated. It is not a separate service or failure domain.

The compare pre-pass is deliberately NAIVE (first ledger entry wins, snapshot
trusted): the measured property is that the ENGINE still refuses at act time
when that worklist is wrong (duplicate rows, stale snapshot, missing record).

Reproduce: `python -m benchmark.o2c_recon.run --n 3` (localhost only, $0).
Pinned in CI by `tests/test_o2c_recon_benchmark.py`.

## What this proves, and what it does not

Proves (within a synthetic closed world):

- a genuinely multi-application flow: one workflow program driving two
  separate fixture applications and two file surfaces, with per-surface
  persisted-state verification including a spreadsheet re-read oracle;
- conflict and duplicate handling refuse BEFORE any write (optimistic
  concurrency, ambiguity, missing record), and the phantom-file-write class
  is caught only by an oracle that actually re-reads the file;
- the same honest metrics as the other benchmarks: runs, verified, halts,
  silent-incorrect-success (0 governed), over-halts (0).

Does NOT prove:

- anything about GUI perception (every action is actuated through the
  replayer's api tier; the "UI gateway" models a screen with no API but is
  driven over HTTP). See the MockMed/OpenEMR/canvas/RDP benchmarks for the
  visual ladder.
- production incidence rates (hand-authored deterministic fault taxonomy;
  cooperative local fixtures; synthetic data only).
- real ERP/accounting-system behavior or any customer environment.
