# Verification coverage: fresh runs, per fault class

Measures the design in
[`docs/VERIFICATION_COVERAGE.md`](../../docs/VERIFICATION_COVERAGE.md): frame
conditions, the fault gate, and provenance. It reruns the OpenEMR fault
experiment of OpenAdaptAI/OpenAdapt#1365 as fresh agent runs, not rebuilt
database states, and runs the same suite on openIMIS. The results are in
[`RESULTS.md`](RESULTS.md).

## Protocol

For each item, a computer-use agent (Claude Sonnet 5.5, the scoreboard's
agent) enters one record through the app's own UI, on a local fixture with
synthetic data. A fault is injected only in the fixture's database, by a
trigger installed before the agent starts or a poller that acts once the new
record lands. The app itself is unchanged.

Three `verify-run` engines judge the same save. Each runs from its own
checkout and takes its own baseline before the agent starts:

| Engine | Commit | What it checks |
|---|---|---|
| `e1365` | the engine #1365 measured with | the declared record; another record removed |
| `main` | main before this work | the same, plus #1375: another record changed, per effect |
| `after` | this work | the frame with the app's own attribution, and the round trip against the source document |

Ground truth is read from the fixture's database after the run. A bad save
is caught when the engine's outcome is anything but `verified`. A clean save
that is refused is a false alarm.

## Fault classes

| Class | What happens | The save is |
|---|---|---|
| `ok` | nothing | clean |
| `partial` | a trigger blanks a field of the new record | bad |
| `wrong_value` | a trigger changes a field of the new record | bad |
| `duplicate` | the poller copies the new record | bad |
| `no_persist` | the poller removes the new record after it lands | bad |
| `collateral_loss` | the poller removes another, pre-existing record | bad |
| `collateral_edit` | the poller edits another record, with no audit trail | bad |
| `collateral_edit_bot` | the same edit, attributed to the bot's own user | bad |
| `concurrent_edit_other` | the same edit, attributed to another user | clean: a legitimate concurrent edit |
| `misread_source` | the source document holds one value; the agent is told another (a misread before the save) and saves it faithfully | bad |

Attribution comes from the app. OpenEMR writes each patient-record change to
its audit log (`log`, with the user and `patient_id`); the `after` engine
reads it with a SELECT-only database user (`audit_read`). openIMIS stamps
each claim row with its last writer and time (`AuditUserID`,
`ValidityFrom`); the `after` engine reads those (`record_field`). For the two
attributed edits, the poller writes what the app would have written: a
`patient-record-update` log row in OpenEMR, the writer and time on the
openIMIS row.

For every item the `after` engine also gets the source document (a synthetic
referral or claim form) and the provenance a first reader would produce. The
"Held before the save" column is the pre-save cross-check: a deterministic
second reading of the document disagreeing with what the agent was told.

## The fault gate

`openemr_gate.py` runs the fault gate (`openadapt_flow/fault_gate.py`) on the
OpenEMR fixture with no model calls, for two reads of the same contract: the
full patient list (the #1365 check) and a search for the run's email (the
kind of read a synthesized check picks).

## Running

The fixtures run as their own compose projects under the shared-machine
rules for this workspace (a lease, at most two fixture sessions, a disk and
load floor). With a fixture up and the three engines checked out:

```bash
PYTHONPATH=packages/flow/benchmark python -m verification_coverage.openemr_run \
  --run-id <id> --per-fault 5 \
  --engine-e1365 <checkout> --engine-main <checkout> --engine-after <checkout>
```

```bash
python -m verification_coverage.summarize --write
```

## What is public

The harness, the fault classes and these rows are public: synthetic data
only. Coverage floors a deployment chooses and any grown fault corpus are
not part of this directory.
