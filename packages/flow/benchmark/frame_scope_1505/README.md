# The #1505 frame fix on the OpenEMR stand-in

OpenAdaptAI/OpenAdapt#1505: a frame with `audit_read` attribution excused any
write the bot made for the saved patient, in any table. The fix: the frame
names the tables the save writes (`writes`) and how to read the table from a
log entry (`scope_field`, `scope_encoding`, `scope_pattern`). A bot log entry
for the saved patient in any other table, or one that names no table, is
collateral.

## What ran

The 17 cases of the independent verification-coverage suite
(`benchmark/verification_coverage/independent/` on the
`verification-coverage/independent` branch), on its stand-in: an SQLite copy
of the pinned OpenEMR fixture's own dump, clinical slice. `drive.py` runs the
suite's `shipped` and `framed` checks; for the patched engine it adds the
scope to the suite's frame:

```yaml
scope_field: comments      # OpenEMR logs each statement here, base64-encoded
scope_encoding: base64
scope_pattern: '(?i)^\s*(?:insert\s+(?:ignore\s+)?into|replace\s+into|update|delete\s+from)\s+`?(?P<scope>[A-Za-z0-9_]+)'
writes: [patient_data, history_data]   # what the New Patient save writes
```

The pinned fixture's own `log` rows store the statement this way, for
example ``INSERT INTO `oauth_clients` (...)``.

- `results/before.json`: `origin/main` at 2026-10-24.
- `results/after.json`: this branch.

## Results

| check | before | after |
|---|---|---|
| `framed`, faults missed | 3 of 15 | 2 of 15 |
| `framed`, false alarms | 0 of 2 | 0 of 2 |
| `shipped`, faults missed | 8 of 15 | 8 of 15 |

The one change is `unrelated_table_insurance_new_patient`: `verified` before,
`reconciliation_required` after (`collateral_scopes: [insurance_data]`). The
clean save and both concurrent edits by another user still verify. The two
remaining misses are other issues: a wrong provider (#1507) and a missing
`history_data` row (#1508).

## Limits

- The stand-in's audit rows are emulated. On the live fixture, OpenEMR logs
  REST API writes with no user and `patient_id` 0 (#1523), so audit
  attribution sees none of them; the fix applies to what the log does name.
  The live re-run needs the shared fixture slot.
- `writes` is checked per table. Fields are compared field by field only for
  the records the frame reads (`patient_data` here).

```bash
python drive.py WORK OUT BASELINE_SQL [--narrow]   # with the suite on sys.path
```
