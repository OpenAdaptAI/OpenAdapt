# After a stop

A stop isn't a rollback. A workflow can save a record on one tab, then stop on
another tab because a check failed. The first save is still in the
application. Flow doesn't hide that or guess about it. It tells a person what
may need undoing, flags programs that write before they check, and runs a
declared undo only after a person approves it.

This page covers three parts:

- [The change ledger](#the-change-ledger): what a stopped run may have changed.
- [Write-ordering checks](#write-ordering-checks): writes that come before a
  check the program could have done first.
- [Declared undo steps](#declared-undo-steps): a compensating step that a
  person approves and Flow verifies.

The code is in `openadapt_flow/after_stop.py` and `openadapt_flow/cli_undo.py`.
The end-to-end tests are in `tests/test_after_stop_mockmed.py`.

## The change ledger

Every run report (`report.json`) has a `change_ledger`. It lists, in run
order, each step that could have changed the application:

- The step is consequential, or it declares an effect.
- The run didn't record that the step stopped before its action was
  delivered.

Each entry has:

| Field | Meaning |
| --- | --- |
| `position` | The step's place among the run's executed steps. |
| `step_id`, `intent` | Which step. |
| `attempt_state` | `delivered`, `actuated_api` or `delivery_uncertain`. |
| `effects` | Each declared effect: its kind, the author's description, its contract hash, and its `status`. |
| `status` | `confirmed` (the system of record shows the change), `refuted` (the system of record contradicts it) or `unknown` (not checked, or the record couldn't be read). A step with no declared effect is `unknown`. |
| `may_need_undo` | False only when the system of record proved that nothing the step declared landed. |
| `undo_id` | The bundle's declared undo for the step, if any. |

The ledger is built from evidence the run already has. It holds step ids,
author text, effect kinds and contract hashes. It never holds selector values
or read-back values.

When a run stops, the ledger appears in three places:

- The message the `run` and `replay` commands print.
- `openadapt-flow explain <run-dir>`.
- A "Changes before the stop" table in `REPORT.md`.

Example, from the MockMed test where step 1 saves a Triage encounter and the
server rejects the step 2 save:

```text
Changes before the stop (in order):
  1. step `save_triage` (save Triage encounter): confirmed in the system of record; may need undoing
       - record_written: one Triage encounter for the patient: confirmed
       declared undo: `undo-triage` (needs a person)
  2. step `save_vitals` (save Vitals encounter): refuted by the system of record; nothing landed
       - record_written: one Vitals encounter for the patient: refuted
Flow never runs an undo on its own. A person must review and approve each one: openadapt-flow undo list runs/r1 --bundle bundle
```

A step without a declared effect is listed as `unknown` and `may need
undoing`. Flow can't tell whether it changed anything, so a person must check.

## Write-ordering checks

`openadapt-flow lint <bundle>` warns when a write comes before a check that the
program could have done first. Two findings:

- `write_before_identity_check`: a write runs before any right-record check,
  and a later read-only step does check the record. A wrong record is caught
  only after the application has changed.
- `write_before_check`: a later read-only step can stop the run after an
  earlier write already landed. That step either has a precondition (`guard`)
  that halts, or is a right-record check before a later write.

Each finding names the steps and suggests an order: move the check, with the
navigation steps that lead to it since the last write, ahead of the first
write. The suggestion is also in the finding's `suggested_order` field.

Lint never reorders a bundle. The check may depend on the write, and moving it
may need extra navigation. A person decides, then re-records or edits the
bundle.

The check covers straight-line bundles. A program graph (`program`) has no
single order to suggest, so it isn't checked.

## Declared undo steps

An undo is a compensating step for one step's effect. For example: "delete the
Triage encounter that step `save_triage` saved."

### Declare an undo

An undo lives in the bundle's `undo_steps` and is part of the sealed bundle
content. Adding or editing one changes the bundle digest, so a certified
bundle needs certifying again.

Each undo has:

- `id`: a short name.
- `undoes`: the step id whose effect it compensates.
- `intent`: plain words the person reads before approving.
- `source`: `demonstration` or `hand_written`.
- `steps`: the compensating actions. At least one step must declare an effect
  that says what the system of record shows afterwards, for example a
  `record_written` effect with `expected_count: 0`. Flow refuses an undo it
  can't check.

From a demonstration: record a person doing the undo, compile the recording,
then attach it. Flow copies the compiled steps and their image crops into the
bundle. Add the effect the undo must produce with `--effects-file` if the
compiled steps don't declare one.

```bash
openadapt-flow undo declare bundle --id undo-triage --undoes save_triage --intent "delete the Triage encounter saved at step 1" --from-bundle undo-bundle --effects-file no-triage.json
```

Hand-written: put the steps in a JSON list that uses the step schema.

```bash
openadapt-flow undo declare bundle --id undo-triage --undoes save_triage --intent "delete the Triage encounter saved at step 1" --from-file undo-steps.json
```

`lint` reports an undo that names a step the bundle doesn't have
(`undo_target_missing`) or a second undo for the same step
(`undo_duplicate_target`).

### Offer, approve, run, verify

1. **Offer.** After a stop, Flow offers an undo when the step's ledger entry
   may need undoing and the bundle declares an undo for it. Offers are listed
   latest step first, because the latest change should be undone first.
   Offering runs nothing.

   ```bash
   openadapt-flow undo list runs/r1 --bundle bundle
   ```

2. **Approve.** A person approves one undo at an interactive terminal. Flow
   shows the undo's steps and the result it will check, and the person types
   the undo id. The approval is written once to
   `<run-dir>/undo/<id>/approval.json` with the person's account name and the
   time. It's bound to a digest of the undo, the ledger entry, the exact
   `report.json` and the bundle digest. If any of them change, the approval no
   longer matches.

   ```bash
   openadapt-flow undo approve runs/r1 --bundle bundle --undo undo-triage
   ```

   The command refuses when stdin or stdout isn't a terminal, so a script or
   agent can't approve through it.

3. **Run.** The undo runs through the normal replay path, so identity checks,
   the risk gate and effect verification all apply. It takes the same backend
   and deployment flags as `replay`. An approval is used once: a claim file is
   created with exclusive create before anything runs.

   ```bash
   openadapt-flow undo run runs/r1 --bundle bundle --undo undo-triage --url https://emr.example.test --config deployment.yaml
   ```

4. **Verify.** The undo is `verified` only when every undo step ran and every
   effect declared on the undo's steps was confirmed in the system of record.
   Anything else is `not_verified`, and the application may still need a
   person's repair. The result is in `<run-dir>/undo/<id>/result.json`, and the
   undo's own run directory is `<run-dir>/undo/<id>/run`.

Flow never runs an undo on its own. The replayer has no path to the undo code,
and `undo run` refuses without a matching approval.

### Python API

```python
from openadapt_flow.after_stop import approve_undo, offer_undos, run_undo

offers = offer_undos(run_dir, workflow)
approve_undo(run_dir, workflow, "undo-triage", approver="nurse-a")
result = run_undo(run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle)
assert result.status == "verified"
```

`approve_undo` is the record a person's approval produces. Call it only from a
path where a person made that decision.

## What this doesn't do

- **No automatic rollback.** Flow doesn't undo anything by itself, and there
  is no transaction across a GUI workflow.
- **No network write buffering.** Flow doesn't hold writes back until the
  whole workflow has passed its checks.
- **No undo inside a loop.** An undo runs with the run's parameters, so it
  isn't offered for a step that ran more than once in the run (a loop body).
- **One attempt per approval.** If an undo isn't verified, a person repairs the
  record by hand. A second attempt needs a new run directory.
- **The approval is a local record.** Like a durable resume approval, it's as
  strong as the operator's control of the run directory. It isn't signed, and
  it isn't sent to the phone approval portal.
- **Undo crops from encrypted demonstrations.** `undo declare --from-bundle`
  refuses an encrypted demonstration bundle.
- **Lint covers straight-line bundles only**, and it can't tell whether a check
  depends on an earlier write.
- **The MockMed tests use a scripted screen.** They drive the real replayer and
  the real MockMed record store, but the screen and keyboard are scripted. The
  `undo run` command's browser path isn't exercised end to end in CI.
