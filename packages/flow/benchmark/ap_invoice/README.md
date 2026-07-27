# AP invoice benchmark (email + PDF + two apps + 3-way match)

Synthetic, deterministic, multi-system workflow benchmark. Not a customer
environment, not publication evidence of production reliability.

## The workflow (one governed run, healthy path)

A vendor emails an invoice as a PDF attachment; the AP workflow processes the
whole intake queue end-to-end across TWO separate local applications:

1. **Intake** (deterministic pre-pass, disclosed below): read the request
   emails from the INBOX maildir, extract each PDF attachment, parse its
   fields, hash the document, and derive one worklist row per invoice.
2. **Per invoice** (workflow-program LOOP body, executed by the real
   `Replayer` through the api actuation tier):
   - enter the invoice draft in the ERP through the **UI gateway** (this
     system has no invoice-entry API; `POST /api/invoice/new` answers 405),
   - attach the PDF's SHA-256 to the record (API),
   - run the **3-way match** (invoice vs purchase order vs receipts) (API),
   - **BRANCH** on the match route: a clean match continues; a price
     mismatch is routed to the **AP exception queue** and the vendor gets a
     hold notice by email,
   - **BRANCH** on discount terms: eligible invoices get the early-payment
     discount applied first; expired terms pay net,
   - approve for payment (UI gateway), schedule the payment (API), and email
     the vendor a confirmation through the mail gateway (a second
     application, maildir-backed).
3. **Batch completion** record, then a success terminal.

Healthy-path shape: 5 worklist invoices, **32 executed consequential
actions**, 2 applications, 5 input/output modalities (email in, PDF document,
REST API, UI gateway, email out), 2 branch points, plus 4 designed exception
scenarios.

## Arms

- `naive`: demo profile; every write is "verified" only against the
  application's own painted acknowledgement banner (what a screen-echo
  automation trusts).
- `governed`: sealed bundle admitted by the real Standard-profile run gate,
  with the resulting single-use authorization bound to the exact inputs; an
  exact API identity contract on every consequential write; effect
  verification routed per record surface (read-only SQL over the ERP
  SQLite file, a REST payments oracle, and the OUTBOX **maildir read from
  disk** for every sent email), a collateral guard on the adjacent grid row,
  and an at-most-once idempotency ledger.

## Scenarios and measured outcomes (n=3 per cell, deterministic)

| scenario | naive (banner oracle) | governed |
|---|---|---|
| `healthy` | completes; `COMPLETED_UNVERIFIED` (never billable) | **`VERIFIED`** (all effects confirmed through separate persisted-state reads; billable) |
| `missing_po` | safe halt at entry | safe halt; `RECONCILIATION_REQUIRED` (the request was sent and no verifier read the ledger, so absence is unproven); nothing persisted per ground truth |
| `duplicate_invoice` | safe halt at entry | safe halt; `RECONCILIATION_REQUIRED` (the request was sent and no verifier read the ledger, so absence is unproven); still exactly one invoice per ground truth |
| `collateral_approve` | **SILENT WRONG** (adjacent invoice corrupted, banner says success) | **caught**: collateral guard refutes; `RECONCILIATION_REQUIRED` |
| `payment_confirm_outage` | completes (cannot know the write landed) | `RECONCILIATION_REQUIRED`; retry under the same idempotency key SUPPRESSED (`REJECTED_POLICY`); ground truth: exactly one payment |

Headline (30 base runs: 15 per arm, plus 6 duplicate-attempt checks): governed
silent-incorrect-success **0/15**, healthy-path over-halts **0/3**, model calls
**0**; naive
silent-incorrect-success **3/3** on the collateral scenario. Every run is
judged through a direct persisted-state read path that opens the SQLite file
and maildir (no HTTP, banner, or verifier verdict reaches it), derives
expectations from immutable source-fixture bytes, and enforces the allowed
record transitions across every non-echo table. This is not a separate service
or failure domain.

Reproduce: `python -m benchmark.ap_invoice.run --n 3` (localhost only, $0).
Pinned in CI by `tests/test_ap_invoice_benchmark.py`.

## What this proves, and what it does not

Proves (within a synthetic closed world):

- the engine's Phase-2 workflow-program machinery (loop + guarded branches +
  explicit exception routing) executes a 30+ step, two-application,
  email/PDF/spreadsheet-era back-office flow deterministically at $0;
- the governed contract stack (identity bindings, per-surface persisted-state
  effect verification including a maildir file oracle, collateral guards,
  idempotency, the Section-3 transaction taxonomy) yields zero
  silent-incorrect-successes across the designed fault classes while a
  banner-echo oracle silently accepts the collateral overwrite;
- uncertain delivery is routed to `RECONCILIATION_REQUIRED` and is never
  blind-retried.

Does NOT prove:

- anything about GUI perception. Every consequential action here is actuated
  through the replayer's **api tier** (the fixture's "UI gateway" models a
  screen that has no API, but it is still driven over HTTP). Pixel-level
  resolution, identity-band OCR, and visual drift are measured by the
  existing MockMed/OpenEMR/canvas/RDP benchmarks, not this one.
- anything about model-based document understanding. The PDF and email
  parsing in intake is a deterministic fixture parser over fixture documents;
  real invoice extraction (OCR/ML) is out of scope and unmeasured.
- production incidence rates. The fault taxonomy is hand-authored and
  deterministic (a coverage matrix, not a sampled population); the fixture
  applications are cooperative and local.
- customer-environment behavior. All vendors, amounts, and emails are
  synthetic; no real ERP or mail server is involved.
