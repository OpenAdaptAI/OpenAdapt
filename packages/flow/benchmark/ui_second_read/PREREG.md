# UI second read on real apps: pre-registered faults

Written and committed before any live run. Results go in `RESULTS.md` beside
this file; nothing here changes after the first run.

## Question

With the application's API and database read channels switched off for the
verifier, can a separate-session read through the application's own screens
(`effects.kind: ui_readback` with the independence rules, the history view
and the provisional/final check) tell correct saves from planted faults?

## Setup

- Apps: OpenEMR 7.0.3 and Dolibarr 21.0.4 on the zen fixture lane (synthetic
  data only).
- The acting session is a headless Playwright browser signed in as the acting
  user. It writes through the application's own forms.
- The verifier is the shipped `SeparateSessionReadbackVerifier` with the
  default Playwright driver: its own browser, a new empty context, signed in
  as a separate read-only user. Its deployment declares no REST, SQL or FHIR
  section, and its session aborts any request to the apps' API paths
  (`/apis/`, `/api/`): the read can only come from HTML screens.
- The harness may use SQL to seed synthetic records and to plant a fault
  (marked "planted by SQL" below). It never uses SQL or an API to decide a
  verdict.
- Cardinality is read from a search or list view, never the record card.

## Fault list

| ID | Fault | App(s) | How planted | Predicted verifier result |
|---|---|---|---|---|
| C | Correct save (control), at least 5 per app | both | UI | CONFIRMED (VERIFIED) |
| F1 | Wrong value: the form gets a different value than declared | both | UI | REFUTED |
| F2 | Duplicate: the same record created twice | Dolibarr (UI); OpenEMR (second row planted by SQL) | UI / SQL | REFUTED (count 2 in the search view) |
| F3 | Wrong record: the edit lands on another record | OpenEMR | UI | REFUTED (target unchanged); with history also REFUTED for the other record's entry |
| F4 | Missing save: form filled, save never clicked | both | UI | not CONFIRMED (REFUTED without an absence claim, or INDETERMINATE) |
| F5 | Late change: a second user changes the record after the provisional check | both | UI (second session) | provisional now; final settle RECONCILIATION_REQUIRED |
| F6 | Collateral: the save is correct but another record is also edited | OpenEMR | UI | CONFIRMED by the record read alone (expected miss); REFUTED when the history view is configured |
| F7 | Same view: the read is configured to open the page the save left on screen | both | config | never CONFIRMED (INDETERMINATE, distinct_view = no) |

## Expected misses, stated in advance

- F6 without a history view: a record or search read only sees the record it
  opens. Reported as a miss of that configuration.
- A fault in a field the read-back view doesn't display can't be seen.
- Dolibarr: no history view is configured in this run (its agenda events
  carry no per-field detail); collateral is measured on OpenEMR only.

## Measurements

- Verdict per case, with the receipt's `read_evidence`.
- Added time: seconds the separate session spent per item, and per item when
  one session settles a batch of final reads.
