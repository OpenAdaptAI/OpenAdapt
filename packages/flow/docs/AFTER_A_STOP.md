# After a stop

A stop isn't a rollback. A workflow can save a record on one tab, then stop on
another tab because a check failed. The first save is still in the
application. Flow doesn't hide that or guess about it. It tells a person what
may need undoing, flags programs that write before they check, and runs a
declared undo only after a person approves it.

This page covers four parts:

- [The change ledger](#the-change-ledger): what a stopped run may have changed.
- [Write-ordering checks](#write-ordering-checks): writes that come before a
  check the program could have done first.
- [Declared undo steps](#declared-undo-steps): a compensating step that a
  person approves and Flow verifies.
- [Proposed undo steps](#proposed-undo-steps): undo steps Flow finds for each
  saving step and tries on a test copy before a person approves them.

The code is in `openadapt_flow/after_stop.py`, `openadapt_flow/undo_proposal.py`
and `openadapt_flow/cli_undo.py`. The end-to-end tests are in
`tests/test_after_stop_mockmed.py` and `tests/test_undo_proposal.py`.

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
| `effects` | Each declared effect: its kind, its run-bound contract hash, and its `status`. Evidence is matched to each contract by hash. |
| `status` | `confirmed` (the system of record shows the change), `refuted` (the system of record contradicts it) or `unknown` (not checked, or the record couldn't be read). A step with no declared effect is `unknown`. |
| `may_need_undo` | False only when the system of record proved that nothing the step declared landed: every declared contract read absent, with exact hash coverage. This is the same rule the transaction outcome uses. |
| `undo_id` | The bundle's declared undo for the step, if any. |

The ledger is built from evidence the run already has. It holds step ids,
step intents, effect kinds and contract hashes. It never holds selector values,
read-back values or an effect's `probe` text, because a probe the compiler
mined from a demonstration can quote record values.

When a run stops, the ledger appears in three places:

- The message the `run` and `replay` commands print.
- `openadapt-flow explain <run-dir>`.
- A "Changes before the stop" table in `REPORT.md`.

Example, from the MockMed test where step 1 saves a Triage encounter and the
server rejects the step 2 save:

```text
Changes before the stop (in order):
  1. step `save_triage` (save Triage encounter): confirmed in the system of record; may need undoing
       - record_written: confirmed
       declared undo: `undo-triage` (needs a person)
  2. step `save_vitals` (save Vitals encounter): refuted by the system of record; nothing landed
       - record_written: refuted
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
content. Adding one changes the bundle digest, so `undo declare` marks any
prior certification expired, and the bundle needs certifying again. Nothing is
written when the undo is refused.

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
bundle, under `templates/undo-<id>/`. Add the effect the undo must produce with `--effects-file` if the
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

1. **Offer.** After a stop, Flow offers an undo when all of these hold:
   - The step's ledger entry may need undoing, and the bundle declares an undo
     for it.
   - The bundle is the one the run used: the run's sealed digest is the
     bundle's current digest, or one the bundle had before `undo declare`
     changed it. `undo declare` records those earlier digests in
     `undo-lineage.json` in the bundle, so an undo declared after the stop
     still matches. This guards against passing the wrong bundle by accident;
     the approval is what binds the undo a person approved.
   - The workflow has no loop, and the run recorded no loop iterations.

   Offers are listed latest step first, because the latest change should be
   undone first. Offering runs nothing.

   ```bash
   openadapt-flow undo list runs/r1 --bundle bundle
   ```

2. **Approve.** A person approves one undo at an interactive terminal. Flow
   shows the undo's steps and the result it will check, and the person types
   the undo id. The approval is written once to
   `<run-dir>/undo/<id>/approval.json` with the person's account name and the
   time. It's bound to a digest of the undo, the ledger entry, the exact
   `report.json` and the bundle digest. If any of them change, the approval no
   longer matches, and a person can approve the new offer. An approval that was
   already used can't be replaced.

   ```bash
   openadapt-flow undo approve runs/r1 --bundle bundle --undo undo-triage
   ```

   The command refuses when stdin or stdout isn't a terminal, so a script or
   agent can't approve through it.

3. **Run.** The undo runs through the normal replay path, so identity checks,
   the risk gate and effect verification all apply. It takes the same backend
   and deployment flags as `replay`. It resolves them the way `replay` does and
   refuses to start when they name no application (a web target with no URL
   and no attached browser), so it can't act on the bundled demo app by
   mistake. An approval is used once: a
   claim file is created with exclusive create before anything runs.

   ```bash
   openadapt-flow undo run runs/r1 --bundle bundle --undo undo-triage --url https://emr.example.test --config deployment.yaml
   ```

4. **Verify.** The undo is `verified` only when every undo step ran and an
   independent read of the system of record confirmed every effect declared on
   the undo's steps. An on-screen read-back doesn't count.
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
result = run_undo(
    run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle
)
assert result.status == "verified"
```

`approve_undo` is the record a person's approval produces. Call it only from a
path where a person made that decision.

## Proposed undo steps

Writing every undo by hand doesn't scale. Most business apps already have an
inverse path: create and delete, add a line and remove it, set a field and set
it back. `undo propose` looks for that path for each saving step, tries each
candidate on a test copy, and writes only the ones that pass into the bundle,
as proposals. A person approves a proposal once. After that, Flow offers it
after a stop like a declared undo, and each run of it still needs a person's
approval at the time.

### Where the candidates come from

For each saving step with a bound record check (a `record_written` or
`field_equals` effect), Flow looks in four places:

| Source | What Flow proposes |
| --- | --- |
| Other recordings (`--from-recording`) | A compiled recording of a person deleting, cancelling, voiding or removing a record. Its steps are used as recorded. If none of them declares an effect, Flow adds the save's inverse: the record the save created is gone. |
| Captured traffic (`--recording`, from `record --capture-network`) | If the recording sent a `DELETE` to the record the save created, that request. Otherwise, for a JSON `POST` whose response carried a record id, a `DELETE` to that record's URL (REST convention). For a JSON `PUT` or `PATCH` that edits a field, the same request with the field's old value. |
| The field's old value | For a field edit typed from a parameter, the same steps again with the value read back before the change. |
| The app's own pages | A Delete, Cancel, Void or Remove control on a page loaded after the save. Flow reports it but doesn't build steps from it; record a person using it and pass that recording. |

A browser form post isn't turned into an API undo: it needs the live page's
session token. Record a person doing the undo instead.

### How a candidate is verified

Only on a target the deployment file declares a test copy, with the same gate
as `induce --probe-on` ([PROBE_INDUCTION.md](PROBE_INDUCTION.md)):
`test_copy.declared: true`, a reset (`reset_url` or `reset_command`), a reward
contract for the read-back, no production runtime profile, and a fresh
session for every run.

For each candidate, Flow:

1. Resets the copy and reads every record through the reward contract's
   oracle. When earlier steps also save, it first runs the steps ahead of the
   save, reads every record, and resets, so "before" is the state just before
   this save.
2. Runs the workflow up to and including the save, with the deployment's own
   effect verifier, and reads every record again. The save must complete and
   must change something; otherwise there is nothing to undo.
3. Runs the candidate undo in a new session, the way `undo run` would. The
   undo's own declared effect must be confirmed by an independent read-back.
4. Reads every record again and compares them with step 1. The candidate
   passes only if they are the same records with the same values: the saved
   record is back as it was, and no other record changed (the collateral
   guard). `--ignore-field` skips a field such as a modified-at timestamp.
5. Resets the copy.

Each candidate's evidence (digests and counts of the three snapshots, how each
run ended, and the reason) is in the report. A failed candidate is reported
with its reason and never written. For example:

```text
1 saving step(s): 0 with a verified undo, 1 without.
  step_010: no verified undo. network: after the undo the test copy differs from before the save: 1 record(s) added since before the save remain (also: no other recordings were given; the save creates a record; there is no old value to restore)
    To close the gap: record a person doing the app's own delete or cancel path and pass it with --from-recording; if the only difference is a timestamp or audit field, a person may name it with --ignore-field.
```

That is MockLoan in a mode where the save also writes a fee row: the
`DELETE` removes the disbursement and leaves the fee.

The evidence file holds no record values. The report names fields, never
values.

### Finding the record at undo time

An undo that deletes "the record the save created" needs that record's id,
which the app assigns during the save. A proposed undo can carry:

- `locate`: before the save runs, the replayer keeps the ids of the records
  that already match the save's record check. At undo time, the one matching
  record that isn't in that list is the one the save created, and its id is
  bound to the parameter `undo_record_id`. No such record, or more than one,
  refuses the undo before anything runs.
- `restore`: before the save runs, the replayer keeps the old value of each
  edited field from the one matching record. The undo writes it back.

The replayer writes these values to `<run-dir>/undo/prior/` from the
pre-action read it already makes for the save's record check, and only for a
step whose bundle holds an undo with `locate` or `restore` when the run
starts. A run made before the proposal was written has no such values, and
its undo refuses. The file is
owner-only, like `params.json`, and it holds record values, so treat the run
directory as sensitive. They are never copied into the report. `undo run` reads
the system of record through the deployment file's effect verifier
(`--config`) to find the record.

### Propose, approve once, run with approval

```bash
openadapt-flow undo propose bundle --config test-copy.yaml --recording rec --from-recording delete-rec-bundle --report propose.json
openadapt-flow undo accept bundle --undo undo-save_triage-network-1
```

`undo propose` without `--config` lists the candidates and runs nothing.
Written proposals carry their evidence in the bundle under `undo-evidence/`,
bound by its SHA-256. Writing them re-seals the bundle and marks any
certification expired, as `undo declare` does.

`undo accept` works only at an interactive terminal. It shows the undo, why it
was proposed and where it was verified, and the person types the undo id. It
refuses when the evidence file is missing or changed, or the undo changed
since it was verified. Until then the proposal is in the bundle but Flow
doesn't offer it, and the change ledger names no undo for the step.

After a stop, the approved proposal is offered, approved and run exactly as in
[Offer, approve, run, verify](#offer-approve-run-verify).

### Measured coverage

`benchmark/undo_proposal/` records each synthetic app's canonical
demonstration in a real browser with network capture, then runs
`undo propose` against the app as a test copy.

| App | Run | Saving step | Verified undo | Why not, and what would close the gap |
| --- | --- | --- | --- | --- |
| MockMed | live | Save Encounter | Yes: `DELETE /api/encounter/{undo_record_id}` (captured traffic, REST convention) | |
| MockLoan | live | Authorize Disbursement | Yes: `DELETE /api/disbursement/{undo_record_id}` (captured traffic, REST convention) | |
| MockLoan, fee-row mode | live | Authorize Disbursement | No | The save also writes a fee row on a second surface, and the `DELETE` leaves it, so the records after the undo don't match. Closes with a recording of the app's own reversal path. |
| OpenEMR fixture, new patient | offline | `step_039` (a click Flow counts as a possible write) | No | No record check is bound to the step, so there is nothing to invert. Closes with a bound record check. |
| OpenEMR fixture, new patient | offline | `step_040` (the save) | No | The save is a browser form post, so no API undo is built. The only control after the save is Cancel. No recording deletes a chart. Closes with a recording of the app's own delete path. |
| openIMIS fixture, claim | offline | `step_015` (the save) | No | The recording has no network capture and no recording deletes a claim. The live fixture wasn't started. Closes with a recording of the app's own delete or cancel path, or a new recording with `--capture-network`. |

Total: 6 saving steps, 2 with a verified undo (both synthetic apps' plain saves), 4 without. On the two real-app fixtures, 0 of 3: each needs a recording of the app's own delete or cancel path, or a bound record check.

Live rows record the demonstration in a real browser with network capture
and run `undo propose` against the app declared a test copy. Offline rows read
an existing bundle and recording and run nothing; they answer whether any
candidate can be built. Every candidate's source, status, basis and reason is
in [`benchmark/undo_proposal/results/`](../benchmark/undo_proposal/results/).

## What this doesn't do

- **No automatic rollback.** Flow doesn't undo anything by itself, and there
  is no transaction across a GUI workflow. A proposed undo runs only on a test
  copy until a person approves it, and then only after a stop, with a
  person's approval at that time.
- **Proposals need a bound record check.** A save whose record effect is a
  placeholder gets no candidate.
- **Exact restore only.** A candidate passes only if every record is back as
  it was. An app that cancels by changing a status, or that writes an audit
  row on delete, fails unless those fields are ignored with
  `--ignore-field`. Ignoring a field is the person's call.
- **One test-copy run per candidate.** A pass shows the undo worked on that
  copy, with the bundle's parameters, once. It isn't a proof for every
  record.
- **No undo built from an app control.** A Delete or Cancel control on the
  saved record's page is reported, not driven. Record a person using it.
- **No network write buffering.** Flow doesn't hold writes back until the
  whole workflow has passed its checks.
- **No undo in a workflow with a loop.** An undo runs with the run's base
  parameters, while a loop iteration wrote with its row's values, so no undo is
  offered for a workflow that has a loop.
- **One attempt per run.** If an undo isn't verified, a person repairs the
  record by hand.
- **The approval is a local record.** It's as strong as the operator's
  control of the run directory. Unlike a durable resume approval, which a
  registered approver key signs, it isn't signed, and it isn't sent to the
  phone approval portal.
- **Undo crops from encrypted demonstrations.** `undo declare --from-bundle`
  refuses an encrypted demonstration bundle.
- **Lint covers straight-line bundles only**, and it can't tell whether a check
  depends on an earlier write.
- **The MockMed tests use a scripted screen.** They drive the real replayer and
  the real MockMed record store, but the screen and keyboard are scripted. The
  `undo run` command's browser path isn't exercised end to end in CI.
