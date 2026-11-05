# UI second read on real apps: pre-registered faults

Written and committed before any live run. Results go in `RESULTS.md` beside
this file; nothing here changes after the first run.

## Question

With the application's API and database read channels switched off for the
verifier, can a separate-session read through the application's own screens
(`effects.kind: ui_readback` with the independence rules, the history view
and the provisional/final check) tell correct saves from planted faults?

## Setup

- Apps: OpenEMR 7.0.3 and Dolibarr 21.0.4 on the fixture host (synthetic
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

## Addendum (before any run): the same cases behind a network chaos proxy

Added at the coordinator's request, before the first live run. Every case
above runs twice: once direct, once through this chain on the fixture host, for
both the acting session and the verifier's session:

    runner -> chaos shim (HTTP, fixture host) -> Toxiproxy (TCP, fixture host) -> app

| Setting | Value | Where |
|---|---|---|
| Latency on every request | 1100 ms +/- 700 ms (400 to 1800 ms), upstream | Toxiproxy `latency` toxic |
| Gateway errors | each request: 1 in 100 gets HTTP 502, 1 in 100 gets HTTP 504 (about 1 in 50 in all), seeded random | chaos shim |
| Lagging search/list view | for 3 s after the acting session's save (a POST to the app), a GET of a declared search or list path returns the response the shim last served for that URL before the save (a stale index) | chaos shim |

Toxiproxy works on TCP, so it can't send an HTTP status or replay a stale
page; the shim (stdlib Python, in this folder) does both.

The read-back's `index_lag_s` per app is set from calibration saves made in
the sandbox before the cases run (three saves on records that aren't case
records; the longest time until the list view showed the save, times 1.5,
rounded up to whole seconds). It is never tuned on case results.

Predictions under chaos:

- No fault case ends CONFIRMED (VERIFIED). Unchanged from above.
- Correct saves still CONFIRMED, except false stops: a 502/504 that hits the
  verifier's sign-in or read makes a read INDETERMINATE, so some correct
  saves will stop. Expected rate: a few percent of correct cases. Each one is
  reported with its cause.
- The stale list view never produces a CONFIRMED for a fault, and a correct
  save is still CONFIRMED when `index_lag_s` covers the lag.
- An acting-side failure caused by chaos (the save itself got a 502) is a
  harness event, reported, and the case is rerun once on a fresh record.
  Verifier verdicts are never rerun.

## Operator deployment configuration

Set `UI2R_OPENEMR_URL` and `UI2R_DOLIBARR_URL` to the isolated synthetic
fixture URLs before running. Set `UI2R_OPENEMR_SQL_COMMAND` to the operator's
SQL client command for that fixture; statements are passed on stdin. It has
no built-in host, container, account or password. Credential provisioning
remains a separate deployment step. These configuration changes do not
replace or add any measurements in the committed result files.
