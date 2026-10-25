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

When the save is an API step ([`API_TIER.md`](API_TIER.md)), the person
approves the request it sends instead of a screenshot: the image and the
phone's rows list the values, the record the check reads back and the
request line. The request's `form_state_kind` is `api_request`, and its
digest binds every byte the save sends, the record and the record check.

Desktop reads the requests and answers one of them:

```bash
openadapt-flow item-approval show RUN_DIR            # JSON; waiting requests
OPENADAPT_JIT_KEY_PASSPHRASE=... openadapt-flow item-approval approve RUN_DIR \
  --request-id ID --request-sha256 SHA256 \
  --decided-by human --route desktop_run_it --unauthenticated \
  --approver-key APPROVER_KEY.pem
openadapt-flow item-approval decline RUN_DIR ... --reason wrong_values
```

- An answer names the request digest the person saw. A person answers from
  the `desktop_run_it` route, or from their phone on the runner's decision
  portal (`decision_portal`; see
  [`APPROVAL_AND_VERIFICATION.md`](APPROVAL_AND_VERIFICATION.md)), which
  echoes the signed task for that exact request. A decline carries
  one closed reason: `wrong_values`, `wrong_record` or `screen_problem`.
- A person's answer counts only when a registered approver key signs it
  (below). `--decided-by human`, the route, `--unauthenticated` and a
  Cloud user id are claims any process could make, so on their own they are
  refused (`human_answer_unsigned`). A claimed Cloud sign-in
  (`--auth-method cloud_aal2` or `cloud_aal1`) is refused too
  (`auth_method_unverified`): nothing on the runner can check it.
- Answers are created once and never replaced. The run checks every answer
  itself, so an answer file written around the command is held to the same
  rules. An approval is used for exactly one save; a loop that reaches the
  same save again asks again.
- After an approval the step takes a fresh frame and resolves its target
  and right-record check again. At the last point before the input, the run
  reads the form again; a field that changed while waiting stops the item
  before the save. In a browser the run reads the page's form controls;
  elsewhere it reads the words on the screen. An API step prepares its
  request again instead; a request whose digest changed stops the item
  before the save, and the GUI steps don't run in its place.
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

### The approver key

A person's answer carries an Ed25519 signature over the whole answer. The
signer must be on the **signed approver roster** (`approvers.json` in the
key directory, `$OPENADAPT_JIT_KEY_DIR`, default `~/.openadapt/jit`), and the
run must **pin the roster's root**. The run checks both on every answer,
including an answer file written around the command
(`approval_mode/approver_roster.py`).

- **The roster.** Its first entry is the root approver key, signed by
  itself. Every later change, adding or removing an approver, is signed by
  an approver already on the roster. Each entry also signs its position
  (`seq`) and the hash of the entry before it (`prev`), so an entry can't be
  replayed, reordered or dropped. Editing the file adds nobody: the first
  entry that doesn't check out stops the replay, and nothing after it
  counts. `principals.json` no longer lists approvers, and `jit register`
  no longer registers one.
- **The pin.** A run names the root approver key it trusts.
  - A governed run, or one an agent started (`--started-by`), takes the root
    only from its deployment config's `approvers.root_public_key`;
    `--approver-root` and `OPENADAPT_APPROVER_ROOT` are ignored, since whoever
    starts the run sets them. Without a config pin, every person's answer in
    that run is refused.
  - Other commands also accept `--approver-root` or `OPENADAPT_APPROVER_ROOT`;
    two sources that disagree are refused.
  - A deployment config that can't be read refuses the run rather than
    dropping its pin.
  - Nothing in the run directory or the environment is a trust anchor.
    Verification takes the root only from the caller, never from
    `OPENADAPT_APPROVER_ROOT`. A reader (`item-approval`, a resume) brings its
    own pin, from its deployment config or `--approver-root`, and is refused
    without one.
  - The roster must extend every head recorded outside the run: the one in
    the key directory's `approvers.head.json` (written by every roster
    change) and the deployment config's `approvers.head` (Desktop writes its
    Keychain's). A roster the run can't verify at start refuses the run.
    The sidecar sits in the key directory, so a process that can write the
    key directory can delete it and put an older roster back. Only a head
    kept elsewhere (the deployment config's, or Desktop's Keychain) catches
    that, which is one more reason to keep the key directory away from the
    agents. `jit approver add` and `remove` update only the sidecar; they
    never update a deployment config's or Desktop's head.
  - The agent's task ledger and its guard server take their root only from
    the deployment config (`--config`); the environment never counts there.
  - With `--config-sha256`, Flow opens the deployment config once (not
    through a symlink; it must be a regular file), refuses it if its digest
    differs, and from then on parses only those bytes whenever the command
    loads that path, including the approver pin. Replacing the file, or a
    symlink put in its place, changes nothing (Desktop passes it for every
    run).
  - The deployment config's `approvers` section is read once per command,
    so swapping the file mid-run changes nothing.
  - The run records the root, the key directory and the roster's head when
    it starts. A governed run's resume refuses when that record is missing. After that, an `item-approval` answer or a resume that names
    another root (its own deployment config's included) or key directory is
    refused, and so is a pin planted in the run directory before the run.
    A roster that doesn't extend the recorded head (an older copy put back)
    is refused (`approver_roster_rolled_back`).
  - A deployment config can also pin `approvers.key_dir`; a run given
    another `--key-dir` is then refused.
- **The first approver.** On a Mac, Desktop sets it up behind Touch ID: the
  root is Desktop's approver key, signed after a presence prompt. Elsewhere,
  a person starts the roster with `openadapt-flow jit approver init`, and
  the deployment config pins its root.

Each route signs with its own key:

- **Desktop's Run it** signs with the Desktop approver key.
- **The decision portal** signs with the key at `OPENADAPT_PORTAL_APPROVER_KEY`
  after the person answered on their phone. Without it, the portal records
  no answer. Its key is added to the roster like any other approver.
- **At a terminal**, a person passes their own key with `--approver-key`. The
  key's passphrase comes from `OPENADAPT_JIT_KEY_PASSPHRASE`, never from the
  command line.

To start a roster and add an approver:

```bash
openadapt-flow jit keygen --out ROOT.pem
openadapt-flow jit approver init --principal front-desk --key ROOT.pem
openadapt-flow jit approver add --principal night-desk \
  --public-key PUBLIC_KEY_HEX --signer-key ROOT.pem --approver-root ROOT_HEX
```

Neither command asks for a terminal or a typed confirmation: a change counts
because an approver signed it.

The run also refuses:
- a key that holds another role, or an approver id that names an agent or
  model;
- an answer signed by the other route's key;
- an answer changed after it was signed;
- an answer signed by whoever started the run.

Keep the key directory, the deployment config and the approver keys away
from the agents the run governs. This pins the runs a deployment governs. A
process that can rewrite the deployment config itself, or that starts its
own run with its own pin, is outside it.

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

By default only a person answers a save. A run can carry an approver grant
a person signed (`--approver-policy SIGNED.json`) that lets named agents or
systems answer some step classes, and a signed webhook
(`--approver-webhook`) to ask. An unsigned or tampered grant gives no
authority, and a delegate never answers a run it started. Desktop and Cloud
show who approved each save: a person, or the delegate's name and the
grant's digest. See [`APPROVAL_AND_VERIFICATION.md`](APPROVAL_AND_VERIFICATION.md).

## Not built yet

- The field-delta effect check, the three new drills and the `customer_stop`
  case kind.
- A Desktop list queue for batch review.
  [`DECISION_TRIAGE.md`](DECISION_TRIAGE.md) describes the engine part that
  orders a list's verified saves for review.
