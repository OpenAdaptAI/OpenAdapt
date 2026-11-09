# Lending (MockLoan) transactional fault-model study

The non-healthcare replication of `benchmark/fault_model` on a distinct system of record: a loan-origination console whose consequential write authorizes a **disbursement of funds to a borrower** (an irreversible money-movement write). Prior rigor studies stressed *UI drift*; this one stresses the *persistence boundary*, which UI drift never touches - on a second, non-clinical domain.

Generated: 2026-07-21 02:00:01  
Platform: Darwin arm64 py3.12.7  
Bundle: 11 steps (login -> open loan -> new disbursement -> Personal -> memo -> **Authorize Disbursement**).  
Repeats per fault class: 10.  
Model calls: **0** (compiled replay; `ANTHROPIC_API_KEY` unset, grounder rung never installed).  

## Method

The bundled MockLoan app is a client-side SPA with no backend, so an "authorize" only mutates in-page state - the UI *is* the source of truth. This study adds a real persistence boundary (`openadapt_flow/mockloan/fault_server.py`): a flag-gated `?fault=` hook in the app (mirroring the `?drift=` hooks) routes the Authorize write through a backend API with an isolated SQLite ledger. **With no `?fault` query the app never calls the API and the normal benchmark is byte-for-byte unaffected** (pinned by a test). Each fault class is injected at that boundary; the SAME recorded bundle is replayed through the REAL `Replayer` against it, and the outcome is judged by `GET /api/db` (ground truth) plus whether the replay halted - never by the replay's self-report. `/api/db` is a read path the SPA itself never calls, so the oracle cannot be gamed by the screen.

### Outcome taxonomy

| outcome | meaning |
|---|---|
| SUCCESS | ran to completion; exactly one correct, complete disbursement booked |
| SAFE-HALT | stopped without completing; **no** side effect |
| WRONG-ACTION | a wrong write landed (duplicate / lost update / persisted-after-halt) |
| FALSE-ABORT | the disbursement landed but the replay reported failure (effect unverified; retry double-pays) |
| UNDETECTED-FAILURE | replay reported **success** but nothing was booked, or it was booked wrong (phantom / partial) |

## Results by fault class

| fault class | title | injected at the boundary | outcome (n) | replay said | silently mishandled? |
|---|---|---|---|---|---|
| (control) no fault | Clean disbursement (control) | core books the disbursement normally | SUCCESS x10 | SUCCESS | no |
| 1. Partial save | Partial save | core books the row but drops the funding memo field | UNDETECTED-FAILURE x10 | SUCCESS | **YES (10/10)** |
| 2. Duplicate submission | Duplicate submission / non-idempotent retry | the authorize is submitted twice with no idempotency key | WRONG-ACTION x10 | SUCCESS | **YES (10/10)** |
| 3. Timeout after write | Core timeout after successful write | core books the row, then hangs past the client timeout | FALSE-ABORT x10 | FAILURE | no |
| 4. Optimistic-UI success, async reject | Optimistic UI success then core rejection | UI paints authorized immediately; the core rejects the write | UNDETECTED-FAILURE x10 | SUCCESS | **YES (10/10)** |
| 5. Session expiry | Session expiry mid-workflow | the write returns 401; the app bounces to the login screen | SAFE-HALT x10 | FAILURE | no |
| 6. Stale data / concurrent modification | Stale data / concurrent modification | last-write-wins over a loan a concurrent officer just held | WRONG-ACTION x10 | SUCCESS | **YES (10/10)** |
| 7. Double-click delivered twice | Double-click registered by the environment | the authorize click is delivered twice, both reach the core | WRONG-ACTION x10 | SUCCESS | **YES (10/10)** |
| (fix) at-most-once via idempotency key | Idempotency key (the recommended fix) | authorize submitted twice, but the core de-duplicates on a key | SUCCESS x10 | SUCCESS | no |

## The headline: silently mishandled transactional faults

A silently-mishandled fault on a consequential write - the replay reports a clean success while the record system is wrong - is the dangerous case. On this corpus the screen-only replay **silently mishandles 5 of the 7 transactional fault classes** (the replay reports success while ground truth is wrong):

- **Partial save** - UI says authorized and the replay reports success, but the booked row is missing the funding memo - no postcondition reads the ledger.
- **Duplicate submission / non-idempotent retry** - Two disbursements are booked; the borrower is paid twice while the replay reports a single clean success. Classic non-idempotency.
- **Optimistic UI success then core rejection** - The screen says authorized, the replay reports success, and NOTHING was booked. Phantom success - the headline undetected failure.
- **Stale data / concurrent modification** - A concurrent officer's URGENT fraud hold is silently overwritten (lost update); the disbursement proceeds and the replay reports a clean success.
- **Double-click registered by the environment** - Same effect as a non-idempotent retry: two disbursements booked, one reported success. The replayer has no at-most-once guard.

## What the current system covers, and what it does not

The compiled replay verifies each step with **vision postconditions** -
`text_present`, `region_stable`, `url_changed`. Every one of these reads the
*screen*. For a consequential write, the screen is the wrong witness: it shows
what the UI painted, not what the loan-servicing core booked. The study makes
the gap concrete:

- **Detected (safe-halt).** Only the fault that also breaks the *screen*
  (session expiry bounces to the login page, so the authorized-banner
  postcondition is never met) is caught. The replay halts with no side effect.
- **Silently mishandled.** Every fault that leaves the success screen intact is
  reported as a clean success while ground truth disagrees: partial save (memo
  dropped), optimistic-UI rejection (nothing booked), duplicate submission /
  double-click (two disbursements - the borrower is paid twice), and stale
  overwrite (a concurrent fraud hold lost). None of these is a drift problem -
  the recorded pixels match perfectly - so no amount of self-healing or template
  tolerance would catch them.
- **Conservatively wrong.** Timeout-after-write halts (safe now) but leaves the
  effect unverified; the natural human/agent response - retry - turns it into a
  duplicate disbursement, which the system also cannot detect.

## Recommended first-class handling

For consequential money-movement writes, idempotency and effect-verification are
safety requirements, not niceties:

1. **At-most-once via idempotency keys.** Attach a per-intent idempotency key to
   any disbursement step and require the core to de-duplicate on it. The
   `idempotent` control shows the duplicate/double-click hazard collapsing to a
   single disbursement once a key is present.
2. **Effect-verification postconditions.** A write step must be able to assert
   its effect against a *structured* read of the record system (an API/DB read),
   not only against a banner. `optimistic` and `partial` become detectable the
   moment the postcondition reads back the persisted memo instead of trusting
   the toast. This is exactly what `benchmark/lending_fault_model/swer.py`
   demonstrates.
3. **Explicit write outcomes over optimistic banners.** Treat a write as pending
   until the core confirms it; do not let an optimistic banner satisfy a
   postcondition. This closes the phantom-success class.
4. **Concurrency / version checks.** Carry a version or etag on the target loan
   and refuse a last-write-wins overwrite; surface the conflict as a halt rather
   than a silent lost update.

These are properties of the *write step contract*, not of the vision layer.


## Reproduce

```
python -m benchmark.lending_fault_model.run
```

Deterministic: every fault is injected at the boundary and the compiled replay is fixed, so repeats agree. Counts are shown for completeness.
