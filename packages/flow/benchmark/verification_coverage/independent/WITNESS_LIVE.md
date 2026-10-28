# Live witness for the whole-database contract

## Live witness, MySQL path (2026-10-28)

The first run of the whole-database snapshot on the pinned OpenEMR 8.0.0.3
fixture (MariaDB), through a read-only account and the one-session
`docker exec` shim. One correct save through the New Patient form, read
before and after (`measure/phase0-independent`, commit `38ab956d2`).

- **The snapshot runs:** 283 tables in 3.0 s; a second read matches the
  first exactly; the baseline is 2.2 MB of MACs.
- **Tables the save moved:** `clinical_rules_log` (1 added, 0 removed, 0 changed), `contact` (2 added, 0 removed, 0 changed), `history_data` (1 added, 0 removed, 3 changed), `log` (321 added, 0 removed, 0 changed), `log_comment_encrypt` (321 added, 0 removed, 0 changed), `patient_data` (1 added, 0 removed, 0 changed), `recent_patients` (1 added, 0 removed, 0 changed), `sequences` (1 added, 1 removed, 0 changed), `session_tracker` (1 added, 0 removed, 0 changed), `user_settings` (12 added, 0 removed, 0 changed), `uuid_mapping` (3 added, 0 removed, 0 changed), `uuid_registry` (8 added, 0 removed, 0 changed).
- **Columns the form fills on a new chart** (not at their declared
  default; many are empty strings where the default is NULL):
  `DOB, billing_note, birth_fname, birth_lname, birth_mname, care_team_status, city, country_code, created_by, date, dupscore, email, financial_review, fname, gender_identity, guardianaddress, guardiancity, guardiancountry, guardianemail, guardianphone, guardianpostalcode, guardianrelationship, guardiansex, guardiansname, guardianstate, guardianworkphone, hipaa_allowemail, hipaa_allowsms, id, imm_reg_status, industry, interpreter_needed, lname, nationality_country, occupation, patient_groups, phone_home, pid, postal_code, preferred_name, pronoun, protect_indicator, providerID, publicity_code, pubpid, ref_providerID, regdate, sex, sex_identified, sexual_orientation, state, street, street_line_2, suffix, title, tribal_affiliations, updated_by, uuid`. On the history row: `created_by, date, pid, uuid`.
- **OpenEMR backfills `uuid`** on existing `history_data` rows when a chart
  is opened (3 rows changed, `uuid` only).
- **Non-ASCII capitals:** "12 Calle Peña" is stored "12 CALLE PEÑA" and
  "Straße" as "STRASSE": the browser's Unicode `toUpperCase()`, not ASCII.

Three findings in the checker, to fix before the held-out suites run live:

1. A SELECT-only account doesn't see triggers on MariaDB; with `TRIGGER`
   granted it does. The snapshot must refuse when it can't see them.
2. `patient_data`'s `AUTO_INCREMENT` column (`id`) isn't its primary key,
   so the counter check had no largest key and flagged every clean insert.
3. `session_tracker` and the uuid backfill are app bookkeeping the
   contract's allow-list doesn't cover yet (the per-app profile).
