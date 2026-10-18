# Approval modes

An approval mode decides whether a person approves an item before the
workflow saves it. Flow has three modes:

| Mode | What happens before each consequential save |
| --- | --- |
| `each_item` | The run waits before the save until a person approves it in Desktop's Run it, then saves exactly what the person saw. This is the mode for a missing or unknown value. |
| `problems_only` | "Only ask a person about problems." The save proceeds without a person only when every check below passes. Any other result goes to a person. |
| `not_gated` | No per-item pause, as before approval modes existed. The run report records `not_gated` so the absence of a pause is visible. |

This page describes the engine part in `openadapt_flow/approval_mode/`, the
commands Desktop calls, and how readiness drills run from Desktop. Cloud
switch records, the Desktop list queue and spot checks aren't part of Flow
yet, so a customer can't turn `problems_only` on today. The requirements
report (below) says so for every workflow.

## Which mode a run uses

| Command | Mode |
| --- | --- |
| `openadapt-flow run` | `--approval-mode`, else `runtime.approval_mode` in the deployment file. A missing or unknown value means `each_item` under every profile, including Demo and runs with no profile. Only an explicit `not_gated` turns the gate off, and the report records it. |
| `openadapt-flow qualify run-case` | The same flags. It always runs under Standard, so the default is `each_item`: the practice item waits for a person. A drill never asks (see below). |
| `openadapt-flow resume` | The mode the run was admitted with. Asking for another mode is refused. |
| Cloud-dispatched runs (`runner/commands.py`, the connector) | `not_gated`, passed explicitly, so the report records it. |

The resolved mode is bound into the run's `GovernedRunAuthorization`. A
run whose gate doesn't match its authorization is refused before any step.
The mode isn't part of Cloud's dispatch binding digest: the runner binds it.

## Per-item approval (`each_item`)

Before a save step resolves its target, the run writes an approval request
in its run directory and waits:

- `item-approval/request-<id>.json`: the run, the save step, the bundle and
  input digests, a digest of the form's field state, and when it expires
  (at most 4 hours).
- `item-approval/<id>.png`: the filled form the person is shown.

Desktop reads the requests and answers one of them:

```bash
openadapt-flow item-approval show RUN_DIR            # JSON; waiting requests
openadapt-flow item-approval approve RUN_DIR \
  --request-id ID --request-sha256 SHA256 \
  --decided-by human --route desktop_run_it \
  --approver-id CLOUD_USER_ID --auth-method cloud_aal2   # or --unauthenticated
openadapt-flow item-approval decline RUN_DIR ... --reason wrong_values
```

- An answer names the request digest the person saw. Only
  `--decided-by human` from the `desktop_run_it` route is accepted. A
  decline carries one closed reason: `wrong_values`, `wrong_record` or
  `screen_problem`.
- Answers are created once and never replaced. The run checks every answer
  itself, so an answer file written around the command is held to the same
  rules. An approval is used for exactly one save; a loop that reaches the
  same save again asks again.
- After an approval the step takes a fresh frame and resolves its target
  and right-record check again. At the last point before the input, the run
  reads the form again; a field that changed while waiting stops the item
  before the save. In a browser the run reads the page's form controls;
  elsewhere it reads the words on the screen.
- A decline stops the item before that save. Earlier saves of the same
  item stay as they are.
- `--approval-wait SECONDS` (default 900, at most 14,400) sets how long the
  run waits. With no answer by then, it stops before the save. In a durable
  run that leaves an `item_approval` pause: an approval given later through
  `item-approval approve` prints `"resume_required": true`, and
  `openadapt-flow resume RUN_DIR` can then make that save. `resume` also
  picks up an approval given while the run was giving up waiting. Continue,
  Skip and the generic `approve` command can't answer this pause.
- A resume saves only the form the person approved, on screen. When the run
  launched its own browser, that browser closed with the run, so a resume
  opens a new one without the filled form: it can't find the save, writes
  nothing, and the approval is spent. Start the item again instead. A resume
  in a signed-in browser the run attached to, where the form is still open,
  checks the form against the approved request before the save.

The report's `approval` block lists every answer as codes and digests: no
values, no screenshot and no form state. An item is `approved` only when a
person approved every save that was delivered and the engine's own outcome
is `VERIFIED`. It never counts as saved without a person.

## Readiness drills from Desktop

Check it's drills (wrong record, two matches, record changed mid-task, save
didn't land, weak record check) are fault qualification cases. A fault case
needs a fault driver: something outside the detector that changes the real
input at one gate, so the engine's ordinary check must refuse it. A local
campaign lets Desktop run them without a separately configured one:

1. `openadapt-flow qualify drills BUNDLE --json` gives the project's five
   drills the practice case's exact inputs and action scope, with the
   practice case's first save as the fault step (`--from-case`,
   `--save-step` choose others), and makes the project trust this
   computer's drill key. The private key stays in
   `$OPENADAPT_HOME/qualification-drills/` (default `~/.openadapt`). This
   changes the project, so run it before recording any case result.
2. `openadapt-flow qualify run-case BUNDLE --case-id DRILL ...` runs a
   drill with that driver (`--fault-driver NAME` loads a configured driver
   instead). The signed receipt and the mutation record are kept in
   `RUN_DIR/qualification-fault/` as the case result's `fault_receipt` and
   `fault_mutation` evidence; `status.json` says whether the drill was
   staged.

What a drill proves doesn't change. The driver only changes the real page,
or the verifier it is handed, and never returns a verdict. A drill passes
only when the engine's own detector refused the exact changed input before
any input was delivered, with the stage and code its kind requires. At a
drill's fault step nothing is ever delivered, in any mode and on the screen
or the API path, and nobody is asked to approve it: a drill whose fault
wasn't staged or wasn't caught saves nothing and doesn't pass. The driver
holds the case's values in memory only; its records hold counts and digests.

## How a caller uses it

Pass an `ApprovalGate` to the `Replayer`. One gate instance serves one item
(one run):

```python
from openadapt_flow.approval_mode.gate import ApprovalGate

gate = ApprovalGate(
    "problems_only",
    record=switch_record,          # runner copy of the clinic's switch record
    log=switch_log_path,           # the runner's local switch log
    pinned_status_keys=cloud_keys, # pinned at runner registration
    fetch_status=fetch_from_cloud, # returns Cloud's signed status
    environment_digest=observer,   # reads the live application version
    runner_id=runner_id,
    signed_in_user=signed_in_check,
    dedicated_user_key=hmac_key,
    item=item_context,             # Desktop's list parsing and history
)
report = Replayer(backend, approval_gate=gate, ...).run(workflow, ...)
report.approval  # the item's decision and checks, as closed codes
```

The item context is single-use. To reuse a gate for the next item, call
`gate.next_item(context)` first; otherwise the next item gets a default
context that fails every item check. Without a gate, replay and the run
report are unchanged.

## Before each save

The gate runs at the last point before the save's input is delivered, after
the target, the right-record check and the record check's pre-state
resolved. A save is a step the run gate's classifier calls consequential.
The runtime also asks before an identity-checked navigation click, such as
opening the patient's chart; that click isn't a save, so the gate lets it
through, and the engine's own identity check still applies. In
`problems_only` the gate checks, in order:

1. **Item checks.** The item came from a Desktop list with a header row,
   its values were parsed strictly, it isn't a retry, repeat or resumed run,
   its record key wasn't entered recently, it uses the deployment's own
   configuration under the Regulated profile with model egress off, and the
   runner holds the switch's lease. A failure sends the item to per-item
   approval and doesn't count as a problem.
2. **P1 `switch_on`.** The local switch log is intact and shows the switch
   on for this record, no earlier item was interrupted, the bundle,
   environment and runner match the record, and a Cloud status signed by a
   pinned key answers this runner's nonce and latest log event within the
   last 15 minutes on the monotonic clock. A stale status pauses the item
   and clears by itself on the next fresh status.
3. **P2 `signed_in_user`.** The application shows the dedicated OpenAdapt
   user.
4. **P3 `right_record`.** Every identity check so far, and the save's own,
   affirmatively verified.
5. **P4 `same_screens`.** No repair, model call, drift rescue, `grounder`
   rung, rung below the step's floor from passing cases, or undeclared
   dialog dismissal so far.
6. **P5 `record_check_ready`.** The verifier is reachable, reads at
   `INDEPENDENT_SYSTEM`, has an identity the switch record names, and its
   effects read back every input the item typed or selected since the
   previous save, and every input an API binding puts in its request. The
   last save also refuses when anything is typed or selected after it. Every
   executable path declares an `exact_new_set` and an idempotency key, as E2
   requires: `field_equals` and `record_written` don't compare with the
   record's state before the save, so they would confirm an update whose
   value was already there, or a record that already existed.

Any failure stops before the save. P2, P3 and P5 failures read "stopped
before saving"; the others wait for a person to approve the item, as in
`each_item`. In a gated mode, a run in which any input would come from the
recording is refused before any step.

## After the run

- **R1 `verified`.** The transaction outcome is `VERIFIED` and agrees with
  the execution outcome and the outcome envelope. Every save is one the gate
  passed, and was confirmed on the first read, with no reconciliation, by a
  bound system-of-record verifier at `INDEPENDENT_SYSTEM`.
- **R2 `whole_run_clean`.** The whole run used no repair, model call,
  rescue, `grounder` rung, lower rung or undeclared dialog.

The item counts as saved without a person only when both pass. Otherwise it
goes to a person. The outcome words never change: an item that fails R2
after a confirmed save still reads "Done and checked", and still goes to a
person.

## When the mode turns itself off

The gate writes a `switched_off` event to the runner's hash-chained switch
log, which applies before the next save, offline too. Cloud can't undo it,
and a missing or broken log also reads as off.

| Reason | Trigger in Flow |
| --- | --- |
| `uncertain_result` | The item ended "Check the record", "Finished, not checked", rolled back, had an unknown or inconsistent outcome, or an earlier item was interrupted. Also when the gate let a save through and its input was delivered, but the outcome says nothing was written: the read-back showed only that the expected record is absent. |
| `too_many_problems` | 3 counted problems in a row, or 5 in the last 20 counted items. Items sent to approval by the item checks or by a stale status don't count. |
| `screen_changed` | P4 or R2 failed. |
| `record_check_lost` | P5 failed, or the engine's own record-check preflight refused the save. |
| `version_changed` / `environment_changed` | The bundle, the environment, the runner or the dedicated user changed. |
| Any reason in a Cloud "off" status | Cloud turned the switch off. |

These thresholds are the public defaults. A deployment can only make them
stricter.

## Requirements

`evaluate_requirements` in `openadapt_flow/approval_mode/requirements.py`
returns E1 to E8 in order with a closed reason for each unmet one. It reads
decisions people record, such as the readiness sign-off and the runner
setup attestations, and never makes them. Two requirements can't be met by
any workflow yet: E2 needs a field-delta check for updates, and E4 needs
three more drills (typed into the wrong field, another field changed, and a
record read at the first gate) that don't exist yet. The five existing
drills run from Desktop, as described above.

## What the record check doesn't see

The checks above confirm the values the record check names. They don't see:

- a field on the new record that no input reaches, such as a recorded choice
  that landed on the wrong option;
- a change to another, existing record;
- a second field that the same input was typed into.

The field-delta check and the whole-item snapshot in the design are meant to
close these. Until then, `tests/test_approval_mode_adversarial.py` keeps
strict expected-failure tests for them.

## Built elsewhere

- Desktop shows the approval request with **Approve and save** and
  **Don't save** (`src/ui/ItemApproval.tsx`, openadapt-desktop #184), and
  Needs you says when a workflow's switch is on or turned itself off.
- Cloud stores switch records and their hash-chained audit events, signs the
  status a runner checks, and shows the switch, the exceptions and each
  run's approval line (openadapt-cloud #395).
- openadapt-agent takes `serve --approval-mode each_item|problems_only|not_gated`
  and records the mode on each result.

## Delegated approvers

By default only a person answers a save. A run can carry an approver policy
(`--approver-policy`) that lets an agent, rule or system answer some step
classes, and a signed webhook (`--approver-webhook`) to ask. See
[`APPROVAL_AND_VERIFICATION.md`](APPROVAL_AND_VERIFICATION.md).

## Not built yet

- The field-delta effect check, the three new drills and the `customer_stop`
  case kind.
- A Desktop list queue for batch review.
  [`DECISION_TRIAGE.md`](DECISION_TRIAGE.md) describes the engine part that
  orders a list's verified saves for review.
- Delegated approvers in Desktop and Cloud. Flow records them; neither app
  shows who or what approved yet.
