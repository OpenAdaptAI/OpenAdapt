# Per-class admission on the OpenEMR stand-in

OpenAdaptAI/OpenAdapt#1506: the fault gate admitted the OpenEMR referral
contract at 18 of 18 faults killed, while an independently written suite
showed it missing 8 of 17 cases. The gate only wrote faults into the one table
the check reads, so a check that can't see the rest of the store looked
complete.

This branch adds two frame fault classes that write outside the check's read:
`other_table` (a record in another table edited) and `side_write` (a row in
another table added for the saved record). It also admits a check only when
every class of every part it claims is tested and fully killed.

## What ran

`gate.py` runs the independent suite's own `gate_shipped`
(`benchmark/verification_coverage/independent/run.py` on the
`verification-coverage/independent` branch) for its `shipped` and `framed`
checks. It runs on the suite's stand-in, an SQLite copy of the pinned OpenEMR
fixture's own dump with the clinical slice of 24 tables, and applies the
default `AdmissionPolicy`.

- `results/before.json`: `origin/main`, 2026-10-24.
- `results/after.json`: this branch.

## Results

| check | main | this branch |
|---|---|---|
| `shipped` | 18/18 killed, admitted | 18/20, refused: `coverage_class_missed:other_table` |
| `framed` | 18/18 killed, admitted | 18/20, refused: `coverage_class_missed:other_table` |

Per class on this branch, for both checks, every class is `killed` except:

- `other_table` is `missed`: an edit to `clinical_rules_log` is invisible to a read of `patient_data`;
- `side_write` is `untestable`: several `*id` columns are shared with other tables, so the copy can't tell which one names the patient. Pass `subject_field="pid"` to `SqliteCopy` to test it.

Either result refuses the check. A check that claims only `delta` isn't held
to the frame classes.

## Limits

- The gate's faults are written directly to the store, so the app's audit log
  never records them, and audit attribution can't excuse them either.
- One store, one contract.

```bash
python gate.py WORK OUT   # with the suite and an engine on sys.path
```
