# UI second read on real apps: results

Faults and predictions were committed before any run (`PREREG.md`, including
the chaos addendum). Raw rows are in `results/run1/` and `results/run2/`, one
JSON line per case, with each read's `independence` evidence. Synthetic data
only. Run on 2026-11-04 against OpenEMR 7.0.3 and Dolibarr 21.0.4 on the fixture host
fixture lane. The verifier's deployment had no API, SQL or FHIR section, and
its session aborted any request to an API path: no API request was made (the
guard's count was 0 in every run).

## Summary

- Apart from the expected miss below (F6a), no planted fault ended CONFIRMED,
  in any run, direct or behind the chaos proxy. These runs predate the
  `sign_in.post_url` binding and the `index_lag_s` re-read at settle; they
  were not rerun after those changes.
- Correct saves: 10 of 10 confirmed direct. Behind the `vpn-bad+replica-lag`
  proxy, after the fixes below (run 2), 8 of 10 confirmed. Two OpenEMR saves
  stopped: one record read timed out, and one audit-log read failed. Before
  those fixes (run 1), 4 of 10 confirmed.
- Late changes (F5) were caught at the final read in every run they ran in;
  unchanged provisional saves (F5c) settled VERIFIED.
- The one expected miss happened as predicted. A collateral edit to another
  patient (F6a) is CONFIRMED when no history view is configured. With the
  audit log as the history view (F6b), it was REFUTED in run 2. In run 1 it
  stopped INDETERMINATE because the audit log couldn't be read.

## Direct (no chaos)

| Case | Fault | Dolibarr | OpenEMR | Predicted |
|---|---|---|---|---|
| C1-C5 | correct save | 5 of 5 CONFIRMED | 5 of 5 CONFIRMED (run 1 and run 2) | CONFIRMED |
| F1 | wrong value | REFUTED | REFUTED | REFUTED |
| F2 | duplicate (Dolibarr: two UI saves; OpenEMR: second row planted by SQL) | REFUTED, count 2 | REFUTED, count 2 | REFUTED |
| F3 | edit landed on another patient | not run (pre-registered for OpenEMR only) | REFUTED | REFUTED |
| F4 | save never clicked | REFUTED, no absence claim | REFUTED, no absence claim | not CONFIRMED |
| F5c | provisional, unchanged | provisional, then VERIFIED at settle | provisional, then VERIFIED | provisional, then VERIFIED |
| F5 | late change by a second session | provisional, then RECONCILIATION_REQUIRED (`changed`) | provisional, then RECONCILIATION_REQUIRED (`changed`) | same |
| F6a | collateral edit, no history view | not run | CONFIRMED (expected miss) | CONFIRMED |
| F6b | collateral edit, audit log as history | not run | run 1: INDETERMINATE (audit log unreadable after the save); run 2: REFUTED (12 new entries about another patient) | REFUTED |
| F7 | read opens the page the save left | INDETERMINATE (`distinct_view: no`) | INDETERMINATE (`distinct_view: no`) | not CONFIRMED |

## Behind the chaos proxy (`vpn-bad+replica-lag`)

The proxy added 400 to 1800 ms of latency per request, a 502 or 504 on about
1 in 50 requests, and served a pre-save copy of the search view for 3 s after
each save. `index_lag_s` came from the pre-registered calibration
(`results/calibration.jsonl`): Dolibarr 4 s, OpenEMR 6 s.

| Case | Dolibarr run 1 | Dolibarr run 2 | OpenEMR run 1 | OpenEMR run 2 |
|---|---|---|---|---|
| C1-C5 correct | 4 of 5 CONFIRMED; C3 stopped (sign-in timed out) | 5 of 5 CONFIRMED | 0 of 5 CONFIRMED (see below) | 3 of 5 CONFIRMED; C3 record read timed out, C4 audit log unreadable |
| F1 wrong value | REFUTED | REFUTED | INDETERMINATE | REFUTED |
| F2 duplicate | REFUTED | REFUTED | INDETERMINATE | REFUTED |
| F3 wrong patient | | | REFUTED | REFUTED |
| F4 missing save | REFUTED | REFUTED | INDETERMINATE (sign-in) | REFUTED |
| F5c provisional | VERIFIED at settle | VERIFIED at settle | INDETERMINATE (refused landing-page request), no entry written | VERIFIED at settle |
| F5 late change | RECONCILIATION_REQUIRED | RECONCILIATION_REQUIRED | harness error (acting save timed out) | harness error, rerun once: RECONCILIATION_REQUIRED |
| F6a collateral, no history | | | INDETERMINATE | CONFIRMED (expected miss) |
| F6b collateral, history | | | INDETERMINATE | REFUTED |
| F7 same view | INDETERMINATE | INDETERMINATE | INDETERMINATE | INDETERMINATE |
| B5 batch of 5 | harness error (acting save) | 5 provisional, 5 VERIFIED in one settle | | |

Run 1 showed three causes of false stops, none of them a verdict rule:

1. OpenEMR's landing page keeps POSTing a reminders counter after sign-in.
   Under latency, that refused request landed during the record read and was
   counted against the record page, which then stopped as "incomplete".
2. A 502 or 504 on the record page's own load gave a page without the ready
   marker, so the read timed out.
3. A 502 on the sign-in page meant no session.

Changes between runs 1 and 2 (commit "UI read-back retries gateway errors and
sign-in, and counts a blocked request only against the page that sent it"):

- A refused request counts against the record page only when that page sent
  it. Every non-read request is still aborted; a real-browser test pins that
  a record page that posts still stops.
- A page load answered with 502, 503 or 504 is fetched again, up to three
  times with backoff.
- The same goes for a failed sign-in, in the same fresh context.

None of these changes what counts as a confirmation. Run 2 is the same case
list on fresh records. OpenEMR direct was run again after the changes; it gave
the same verdicts as run 1, except that F6b was now REFUTED.

## Deviations from the pre-registration

- The OpenEMR history view first false-stopped a correct save (debug run C1,
  before the logged runs): a demographics save also writes `http-request-update`
  and `other-insert`/`other-update` audit entries, some with patient 0 or
  none. I took the entry types one correct save writes from a separate
  calibration save (not a case), declared them `explained`, and declared
  patient `0` and blank as `unattributed_records`. A collateral edit made in
  OpenEMR's own `other-*` entries with patient 0 would therefore pass. Reads
  (`-Query`, `view`, `login`) are `ignored`.
- The OpenEMR history view is behind a GET filter form, so `HistoryView` gained
  `submit_selector`. Dolibarr's list view reformats phone numbers, so the
  Dolibarr cases compare name and alias instead.
- Dolibarr's calibration likely understates the stale window: the shim serves
  a stale copy only of a URL it fetched before the save, and calibration names
  are new. Correct Dolibarr saves still confirmed in run 2 with 4 s.
- The chaos shim moved from this folder to `benchmark/lab_chaos/` (shared lab
  infrastructure). Settings unchanged.

## Cost

Seconds the separate session spent per item (launch, sign-in, reads):

| | Dolibarr | OpenEMR (record + audit log) | OpenEMR (record only) |
|---|---|---|---|
| direct | 1.1 to 2.3 | 13 to 15 (one outlier at 47) | 4 to 9 |
| chaos run 2 | 12 to 15 | 33 to 44 | 16 to 23 |

Batching: one settle session for five provisional Dolibarr saves took 3.0 s
direct (0.6 s per item, against 1.1 to 2.3 s for a session per item) and
30.8 s under chaos (6.2 s per item, against 12 to 16 s).

## What this doesn't show

- Two web apps, one write each, a handful of cases per fault: counts, not
  rates.
- The read sees what the view shows. A fault in a field the view doesn't
  display, or a collateral change the app doesn't log, passes.
- The provisional/final flow ran with a quiet window of 0 s; the late change
  was made before the settle on purpose.
- No desktop or Citrix app: the independence rules apply there by
  construction (a non-browser acting session is a different view), but this
  run didn't exercise it.
