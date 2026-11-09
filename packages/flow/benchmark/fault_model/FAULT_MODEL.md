# Transactional fault-model study

How the current compiled replay handles the failure classes that matter for **consequential writes** — the ones a record system must never get wrong. Prior rigor studies (cosmetic_drift, dense_surface, reliability) stressed *UI drift*; this one stresses the *persistence boundary*, which UI drift never touches.

Generated: 2026-07-12 08:44:47  
Platform: Darwin arm64 py3.12.9  
Bundle: 11 steps (login → open referral → new encounter → Triage → note → **Save Encounter**).  
Repeats per fault class: 10.  
Model calls: **0** (compiled replay; `ANTHROPIC_API_KEY` unset, grounder rung never installed).  

## Method

The bundled MockMed app is a client-side SPA with no backend, so a "save" only mutates in-page state — the UI *is* the source of truth. This study adds a real persistence boundary (`openadapt_flow/mockmed/fault_server.py`): a flag-gated `?fault=` hook in the app (mirroring the existing `?drift=` hooks) routes the Save write through a backend API with an in-process DB. **With no `?fault` query the app never calls the API and the normal benchmark is byte-for-byte unaffected** (pinned by a test). Each fault class is injected at that boundary; the SAME recorded bundle is replayed against it, and the outcome is judged by `GET /api/db` (ground truth) plus whether the replay halted — never by the replay's self-report.

### Outcome taxonomy

| outcome | meaning |
|---|---|
| SUCCESS | ran to completion; exactly one correct, complete row written |
| SAFE-HALT | stopped without completing; **no** side effect |
| WRONG-ACTION | a wrong write landed (duplicate / lost update / persisted-after-halt) |
| FALSE-ABORT | the write landed but the replay reported failure (effect unverified; retry double-writes) |
| UNDETECTED-FAILURE | replay reported **success** but nothing was written, or it was written wrong (phantom / partial) |

## Results by fault class

| fault class | title | injected at the boundary | outcome (n) | replay said | silently mishandled? |
|---|---|---|---|---|---|
| (control) no fault | Clean write (control) | backend persists the row normally | SUCCESS ×10 | SUCCESS | no |
| 1. Partial save | Partial save | backend commits the row but drops the note field | UNDETECTED-FAILURE ×10 | SUCCESS | **YES (10/10)** |
| 2. Duplicate submission | Duplicate submission / non-idempotent retry | the save is submitted twice with no idempotency key | WRONG-ACTION ×10 | SUCCESS | **YES (10/10)** |
| 3. Timeout after write | Backend timeout after successful write | backend commits the row, then hangs past the client timeout | FALSE-ABORT ×10 | FAILURE | no |
| 4. Optimistic-UI success, async reject | Optimistic UI success then server rejection | UI paints success immediately; the server rejects the write | UNDETECTED-FAILURE ×10 | SUCCESS | **YES (10/10)** |
| 5. Session expiry | Session expiry mid-workflow | the write returns 401; the app bounces to the login screen | SAFE-HALT ×10 | FAILURE | no |
| 6. Stale data / concurrent modification | Stale data / concurrent modification | last-write-wins over a row a concurrent actor just changed | WRONG-ACTION ×10 | SUCCESS | **YES (10/10)** |
| 7. Double-click delivered twice | Double-click registered by the environment | the save click is delivered twice, both reach the backend | WRONG-ACTION ×10 | SUCCESS | **YES (10/10)** |
| (fix) at-most-once via idempotency key | Idempotency key (the recommended fix) | save submitted twice, but the server de-duplicates on a key | SUCCESS ×10 | SUCCESS | no |

## The headline: silently mishandled transactional faults

A silently-mishandled fault on a consequential write — the replay reports a clean success while the record system is wrong — is the dangerous case. On this corpus the current system **silently mishandles 5 of the 7 transactional fault classes** (the replay reports success while ground truth is wrong):

- **Partial save** — UI says saved and the replay reports success, but the persisted row is missing the clinical note — no postcondition reads the DB.
- **Duplicate submission / non-idempotent retry** — Two encounter rows are written; the replay reports a single clean success. Classic non-idempotency hazard, undetected.
- **Optimistic UI success then server rejection** — The screen says saved, the replay reports success, and NOTHING is in the DB. Phantom success — the headline undetected failure.
- **Stale data / concurrent modification** — A concurrent clinician's urgent note is silently overwritten (lost update); the replay reports a clean success.
- **Double-click registered by the environment** — Same effect as a non-idempotent retry: two rows written, one reported success. The replayer has no at-most-once guard.

The duplicate/double-click cases are the sharpest: a **wrong write actually executes** (two encounter rows) behind a green report. The phantom-success case (optimistic-UI reject) is the quietest: a **green report over an empty DB**.

## What the current system covers, and what it does not

The compiled replay verifies each step with **vision postconditions** —
`text_present`, `region_stable`, `url_changed`. Every one of these reads the
*screen*. For a consequential write, the screen is the wrong witness: it shows
what the UI painted, not what the backend persisted. The study makes the gap
concrete:

- **Detected (safe-halt).** Only the fault that also breaks the *screen*
  (session expiry bounces to the login page, so the saved-banner
  postcondition is never met) is caught. The replay halts with no side effect.
- **Silently mishandled.** Every fault that leaves the success screen intact
  is reported as a clean success while ground truth disagrees:
  partial save (note dropped), optimistic-UI rejection (nothing persisted),
  duplicate submission / double-click (two rows), and stale overwrite
  (a concurrent change lost). None of these is a drift problem — the recorded
  pixels match perfectly — so no amount of self-healing or template tolerance
  would catch them.
- **Conservatively wrong.** Timeout-after-write halts (safe now) but leaves
  the effect unverified; the natural human/agent response — retry — turns it
  into a duplicate write, which the system also cannot detect.

## Recommended first-class handling

For consequential writes, idempotency and effect-verification are safety
requirements, not niceties:

1. **At-most-once via idempotency keys.** Attach a per-intent idempotency key
   to any write step and require the backend to de-duplicate on it. The
   `idempotent` control in this study shows the duplicate/double-click hazard
   collapsing to a single row once a key is present. This is the single
   highest-value fix: it neutralizes fault classes 2 and 7 outright and makes
   *any* retry (fault 3's dangerous follow-on) safe.
2. **Effect-verification postconditions.** A write step must be able to assert
   its effect against a *structured* read of the record system (an API/DB read
   or a structured DOM read of the saved record), not only against a banner.
   `optimistic` and `partial` become detectable the moment the postcondition
   reads back the persisted note instead of trusting the toast.
3. **Explicit write outcomes over optimistic banners.** Treat a write as
   pending until the backend confirms it; do not let an optimistic banner
   satisfy a postcondition. This closes the phantom-success class (fault 4).
4. **Concurrency / version checks.** Carry a version or etag on the target
   record and refuse a last-write-wins overwrite; surface the conflict as a
   halt rather than a silent lost update (fault 6).

These are properties of the *write step contract*, not of the vision layer.
The reference postcondition system is vision-only today, so on this corpus it
provides **no** transactional-safety coverage beyond the incidental
session-expiry case.


## Reproduce

```
python -m benchmark.fault_model.run
```

Deterministic: every fault is injected at the boundary and the compiled replay is fixed, so repeats agree. Counts are shown for completeness.
