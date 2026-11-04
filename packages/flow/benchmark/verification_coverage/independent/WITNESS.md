# Witness: what one correct New Patient save writes

SPEC.md was committed before any run (ad4bdbfe2) and is not edited. This file
holds the amendments its "Witness run" section allows, each with its evidence.

## Evidence used so far

The live witness run (`live.py`, case `clean`, table deltas) is queued for the
shared fixture slot. Until it runs, the evidence is an earlier measurement on
the same pinned app, kept in the repository: `benchmark/openemr_local/fixture.py`
`EXPECTED_TABLE_DELTAS`. Those are exact table-count deltas of one UI New
Patient save (`compiled` and `agent` arms) and one REST save (`api` arm).

| table | UI save | REST save | SPEC.md before amendment |
|---|---|---|---|
| `patient_data` | +1 | +1 | P1 |
| `history_data` | +1 | 0 | P4 (required) |
| `contact` | +1 | 0 | not named |
| `recent_patients` | +1 | 0 | not named |
| `user_settings` | +2 | 0 | not named |
| `clinical_rules_log` | +1 | 0 | not named |
| `uuid_registry` | +14 | +2 | F3 bookkeeping |
| `uuid_mapping` | +12 | +1 | not named |
| `log`, `log_comment_encrypt` | +229 to +311 | +16 | F3 bookkeeping |
| `api_log` | +1 to +13 | +2 | F3 bookkeeping |

## Amendments

1. **F3 bookkeeping gains** `recent_patients`, `user_settings`,
   `clinical_rules_log` and `uuid_mapping`. They are per-user UI state and
   OpenEMR's own logs, written for any signed-in user who opens a chart.
2. **`contact`** (one row linking the chart to its address record) is written
   by the UI save and not by the REST save. It is listed as an observed side
   record, not added to P4: the fault suite has no case that removes it, and no
   check in this measurement reads it.
3. **The REST save makes no `history_data` row.** Under P4, a chart created
   through `POST /api/patient` is incomplete. The stand-in's seeded charts and
   `live.py`'s seeded charts add the row the UI would have written, so that F2
   has a target.

The stand-in (`standin.py` `Store.register`) writes the UI save's rows above
except the uuid mapping and the log volume: one `patient-record-insert` audit
row per chart table, one `other-insert` for `contact`, and one
`patient-record-select`.

## Live witness (2026-10-24)

The `clean` case on the pinned fixture (`results/live-2026-10-24/RESULTS.md`): one
New Patient save through OpenEMR's form in a browser, signed in as the bot.
The table deltas over the run window:

| table | delta | |
|---|---:|---|
| `patient_data` | +1 | P1 |
| `history_data` | +1 | P4, as predicted |
| `contact` | +1 | as the earlier measurement showed |
| `recent_patients`, `clinical_rules_log` | +1 each | bookkeeping (amendment 1) |
| `user_settings` | +2 | bookkeeping |
| `uuid_registry`, `uuid_mapping` | +8, +3 | bookkeeping |
| `log`, `log_comment_encrypt` | +248 each | bookkeeping |
| `api_log`, `api_token`, `api_refresh_token`, `oauth_trusted_user` | +4, +2, +2, +1 | the harness's own API sign-ins and reads, not the save |

Two more amendments from the live app:

4. **Street and city are stored in capitals.** OpenEMR's form saved
   `41 Larch Way, Cambridge` as `41 LARCH WAY`, `CAMBRIDGE`. SPEC.md's
   "exact after trimming" for those two fields was wrong for OpenEMR; they
   compare case-insensitively. A run with the referral in mixed case
   (raw rows in openadapt-corpus) shows what this does to
   OpenAdapt's contract, which compares exactly: the correct save is refuted
   (`reconciliation_required`) by both checks. The suite then used the
   referral in capitals, as the production soak's workload does.
5. **OpenEMR's audit log does not attribute API writes.** Every write made
   through the REST API, by the bot or by clinician2, was logged as
   `patient-record-*` with an empty `user` and `patient_id` 0 (the audit rows are in openadapt-corpus). The UI save logged the bot's user, but `patient_id` 0 on all
   but one row. The stand-in's emulated audit rows (a user and a patient id on
   every write) were therefore too generous; see README.md. OpenEMR's
   `api_log` table does record `user_id` and `patient_id` for API calls.
