# Self-repair

When a vendor changes a screen, a compiled workflow halts before it writes
anything. The save button has a new label, a new place, or a new DOM id, and
none of the recorded ways to find it work. Without self-repair, the run stops
and waits for a person to re-teach the step.

Self-repair closes that loop. A computer-use agent proposes a fix for the
halted step: one corrected click, or a short bridge of up to three new
actions when the vendor added a required dialog or moved a control into a
menu. Flow replays the fix up to the save without saving, and checks that the
form and the record are the same as in the recording. Then one
person accepts the fix with one tap, on the phone or at a terminal, and the run
resumes. Healthy runs never call the agent and record zero model calls.

Module: `openadapt_flow.repair.self_repair`. CLI: `openadapt-flow run`,
on by default once a repair agent is configured. Tests:
`tests/test_self_repair.py`, `tests/test_self_repair_cli.py`,
`tests/test_self_repair_bridge.py`, `tests/test_auto_repair.py`,
`tests/test_auto_repair_cli.py`.

## Repair is the default

Once a deployment names a repair agent, every stop that wrote nothing goes to
it. Nobody has to start a repair. Module: `openadapt_flow.repair.auto_repair`.

```yaml
# deploy.yaml
repair:
  agent: mypkg.agents:make_agent
  screen_access_by: ops-lead
  fault_urls: ["https://emr.test/?fault=missing"]
```

Each stop is routed by what the run may have written:

| The run ended | Where the stop goes |
| --- | --- |
| `VERIFIED` | Nowhere. The agent is never called. |
| `HALTED_BEFORE_EFFECT` (or a halt with no outcome word and no effect) | To the repair agent, then the probe, the replay and fault cases, one approval (or a standard change), and the resumed item. A halt self-repair doesn't handle, such as a wrong-record halt, goes to a person. |
| `RECONCILIATION_REQUIRED` | Never to repair. A write may have happened, so it goes to reconciliation: the change ledger in `REPORT.md` and `openadapt-flow undo list <run_dir>` ([AFTER_A_STOP.md](AFTER_A_STOP.md)). |
| Anything else | To a person. |

The agent is shown the stop (the halted step, its intent and why it halted),
the screen evidence (the live screen and the recorded crop, scrubbed unless
set otherwise) and the record check (`request.record_check`: each save's
effect contracts with literal values masked, and a digest of its record
identity contract). It proposes a re-anchored step, a bridge of up to three
steps, or a fix a signed policy pre-approves. A fix never changes the record
check: the probe, the cases and the resumed run check it again. For a
worklist, `WorklistQueue` holds the items that stopped the same way, repairs
on the test record, and releases them all with one approval.

Every stop, repaired or not, appends one row to
`<repair-store>/auto-repair/ledger.jsonl`: the route, the halted step, the
fixes proposed, whether one passed the probe and the cases, who approved it
and how long the answer took, whether the item resumed and verified, and the
agent's model calls and cost. `openadapt_flow.repair.auto_repair.summarize`
turns the rows into the repair success rate, failures included.

To turn it off for one run, pass `--no-auto-repair`. To turn it off for a
deployment, set `repair.auto: false`. A run that can't be repaired (for
example one pinned to `--pin-digest`) is not refused: it runs plainly and
prints why repair is off for it. `--self-repair` still asks for repair
explicitly, and then such a run is refused.

See [Measured](#measured) for how often it works.

## What happens when a run halts

1. **Halt.** The run halts as usual. Self-repair starts only when all of
   these hold:
   - no input reached a step that saves, and no effect was observed;
   - the halted step is a click, and the halt is one of two kinds:
     - **replace:** the click never acted, because the ladder found no target
       or found it only by geometry and refused to act on that;
     - **after:** a click that doesn't save acted on the target the ladder
       found, but its expected screen never came (a new dialog appeared);
   - the workflow has exactly one save (for now).

   A halt on the record check (an identity mismatch) is never repaired, and
   neither is an item a person declined at the approval gate.
2. **Propose.** The repair agent is shown the step's intent, the recorded
   evidence (the target crop, its label, its place), the live screen, the
   halt kind, and for `after` the text of the screen the step expected. It
   proposes one action, or a bridge of up to three, in its own dialect. Its
   model calls and their cost go into the ledger.
3. **Patch.** Flow builds the patch on the halted page, one action at a time.
   - **replace:** the last action becomes the halted step's new target. Flow
     builds it from the live screen with the heal code: a new template crop,
     the DOM locator under the point, and the label read there. Identity
     evidence is copied unchanged, so the patch can't weaken the record check
     (the repair invariants check this again). Actions before it, such as
     opening a menu, become new steps that run first.
   - **after:** every action becomes a new step after the halted one. The
     last new step takes over the halted step's expected screen, and the
     halted step now expects the new dialog.
   - **New steps.** Each new click is captured from the live screen and then
     performed, so the next action sees the screen it will act on. It gets
     the same evidence a recorded click has: a template crop, its DOM
     locator, a postcondition derived from the before and after screens, and
     an identity check. The identity is the record the page shows under the
     click, hashed with the bundle's salt, and it must match a record the
     recording verified.

   Flow refuses the proposal, before performing anything that remains, when:
   - the bridge has more than three actions;
   - a saving step's new control does not read as a save (for example,
     `Consult` for a step recorded on `Save Encounter`), or a step that
     doesn't save would point at one that does;
   - a new step would click a control that reads as a save, or one with no
     readable label (an unlabeled control may save);
   - a new step's control is not inside the recorded record;
   - the new control is one that another step already uses;
   - the bridge types a value that is neither one of the item's parameters
     (the step is then bound to that parameter) nor text the screen shows at
     that moment.

   After an attempt that performed bridge actions, the next attempt first
   reaches the same halt again in a fresh session, behind the probe gate.
4. **Probe.** The patched bundle replays in a fresh session, admitted by the
   same run gate with its own authorization. A gate stops it at the last point
   before the first save, after the target and the record check resolved.
   Nothing is written. The probe passes only when every check passes:

   | Check | Passes when |
   | --- | --- |
   | `nothing_written` | No earlier step that saves received input. |
   | `zero_model_calls` | The run made no model call before the save. |
   | `target_found` | Every new or re-targeted step ran, and the deterministic ladder found its target by DOM locator, template, or label, not by geometry or a model. |
   | `same_record` | Every identity check in the probe verified, including the save's own. |
   | `same_fields` | Each value the workflow typed is in a form field. |
   | `same_form` | The form is exactly as it was when the run halted (when the halt was at this save). |

   A failed probe goes back to the agent with the reason, up to
   `max_attempts` proposals.
5. **Campaigns.** A passing probe becomes an ordinary repair candidate in the
   [repair store](REPAIR_LIFECYCLE.md): source `model_suggestion`, provenance
   `repaired_by: self_repair`, with the agent's id. The replay and fault
   campaigns run before anyone is asked:
   - When the patch re-targets one step and its identity can be read from
     pixels (an OCR band or identity tokens), Flow runs the existing frame
     campaigns, plus the live probe as one more healthy case.
   - When the step's identity is read from the DOM, or the patch adds steps,
     the frame campaigns can't judge it. The live probe is the healthy case, and each fault probe is an
     adversarial case: a session that presents a fault (for example, the
     record is missing) and must stop before the save. Without fault probes
     the candidate stops here with status `needs_fault_probes`.
6. **Approve.** When a signed repair policy allows the fix (see
   [Standard changes](#standard-changes-repairs-without-a-person)), it
   applies without asking anyone. Otherwise Flow writes one approval request into the halted run's
   `item-approval/` folder, with a screenshot of the filled form and the new
   control outlined. One person answers it. That answer records both the
   review and the approval in the store. The approver is recorded as their
   Cloud user id, or as `<route>:person` (for example
   `decision_portal:person`) when they answered without signing in, as a
   paired phone always does.
   - The agent can never review or approve its own candidate.
   - Only a person's answer counts; an agent or rule can't answer it.
   - A decline rejects the candidate. No answer in time leaves it waiting.
   - In either case nothing is saved.
7. **Resume.** The approved bundle is staged and enters its canary window.
   The item re-runs from the start in a fresh session. At the save it runs the
   probe checks again and proceeds only if the form is exactly the one the
   person approved. The record check runs as usual, and the run is the
   canary's first observation. If the approved bundle can't run, the canary is
   rolled back and the status is `resume_failed`.

The re-run is safe because self-repair handles only a halt before any
effect. The item was never partly saved.

## Approving the fix

The request is a normal per-item approval request, so the existing surfaces
show it:

- **Phone.** The decision portal lists it when the halted run's directory is
  under the console's run root. One tap approves or declines it.
- **Terminal.** `run --self-repair` prints the exact command when it asks:

  ```bash
  openadapt-flow item-approval approve runs/item-17 --request-id <id> --request-sha256 <sha> --decided-by human --route desktop_run_it --unauthenticated
  ```

- **Reject in the store.** Rejecting the candidate with `RepairStore.reject`
  while the run waits ends the wait as a decline.

Self-repair waits `approval_wait_s` seconds (default 900, the same as each
item approval). When nobody answers in time, the run ends with status
`awaiting_approval`. The candidate stays in the store, and nothing is saved.

## Python API

Four objects make up the API:

- `RepairAgent`: you write it.
- `ReplayTarget`: tells Flow how to reach your app.
- `ScreenAccess`: records who allowed the agent to see screens.
- `SelfRepair`: runs the item.

```python
from openadapt_flow.repair.self_repair import (
    AppSession, RepairProposal, ReplayTarget, ScreenAccess, SelfRepair,
)
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.runtime.effects import RestRecordVerifier

URL = "http://127.0.0.1:8765/?fault=ok&idempotency=demo&drift=redesign#tasks"

def open_session(fault):
    backend, close = PlaywrightBackend.launch(fault or URL)
    return AppSession(backend=backend, close=close)

target = ReplayTarget(
    open_session,
    effect_verifier=RestRecordVerifier("http://127.0.0.1:8765", records_path="/api/db"),
    policy="clinical-write",
    params={"note": "Synthetic follow-up in two weeks"},
    entry_url=URL,
    execution_origin="http://127.0.0.1:8765",
)

result = SelfRepair(
    my_agent,
    target,
    store="repair-store",
    screen_access=ScreenAccess(enabled_by="ops-lead", mode="allow_unscrubbed"),
    fault_probes=[URL.replace("drift=redesign", "drift=redesign,missing")],
    approval_wait_s=900,
    on_approval_request=lambda run_dir, request_id, sha: print("ask", request_id),
).run("bundles/triage", "runs/item-17")

print(result.status, result.report.execution_outcome)
```

`ReplayTarget(open_session, ...)` calls `open_session(None)` for an ordinary
session and `open_session(fault)` with each `fault_probes` entry. For
`SelfRepairTarget`, implement `open(fault)` and `execute(...)` yourself; the
CLI does this.

`SelfRepair.run(bundle_dir, run_dir)` returns a `SelfRepairResult`. It
contains:

- `status`;
- the final `report` (the resumed run's report, or the halted run's report);
- `halted_report`;
- `candidate`;
- `patched_bundle`;
- `approval_request_id`.

| `status` | Meaning |
| --- | --- |
| `not_needed` | The run did not halt. The agent was not called. |
| `not_repairable` | The halt is one self-repair doesn't handle. The reason says which. |
| `no_fix` | No proposal passed its checks, or a campaign failed. Nothing was written. |
| `needs_fault_probes` | The step's identity is read from the DOM and no fault probe was given. |
| `declined` | A person declined. The candidate is rejected. |
| `awaiting_approval` | Nobody answered in time, or an answer was refused (for example, one not from a person). The candidate waits in the store. |
| `resumed` | The approved bundle re-ran. Read `report.execution_outcome`. |
| `resume_failed` | The approved bundle could not run, or an unexpected error stopped self-repair. Any canary was rolled back. |

### Writing a repair agent

```python
class MyAgent:
    dialect = "anthropic"        # any name in openadapt_flow.jit.dialects()
    agent_id = "agent:my-repair" # recorded; can never approve its own fix
    requires_model_key = True    # False only for an agent that calls no model

    def propose(self, request):
        # request.intent, request.recorded_label, request.recorded_crop_png,
        # request.live_png, request.viewport, request.previous_refusals,
        # request.model_key (the customer's key; never an OpenAdapt key)
        action = ask_my_model(request)   # e.g. {"action": "left_click", "coordinate": [846, 457]}
        return RepairProposal(action=action, model_calls=1, cost_usd=0.004)
```

- **A bridge.** `RepairProposal(actions=[...])` gives up to
  `request.max_actions` actions in order: clicks, double clicks, and typing.
  Type only a parameter's value (`request.param_values`) or text the screen
  shows. In `replace` mode the last action must be the halted step's kind of
  click.
- **One action.** A single-action proposal is one click or double click, matching
  the halted step's action. The agent may give a coordinate or, in the
  `playwright_mcp` dialect, a unique CSS selector. Anything else is refused.
  Return `None` to give up.
- **The customer's key.** An agent that calls a model must run on the
  customer's own key. Pass `model_key=load_customer_model_key("anthropic")`
  (see [JIT.md](JIT.md)). `SelfRepair` refuses to start without one unless the
  agent sets `requires_model_key = False`. Tests use a scripted agent, so CI
  makes no paid calls.
- **Reported cost.** Flow can't see the agent's model calls. The agent
  reports `model_calls` and `cost_usd` on each proposal, and Flow records
  them per attempt.
- **Screens.** Under the default `scrubbed_only`, the live screen and the
  recorded crop are scrubbed before the agent sees them. Pass
  `ScreenAccess(scrub=...)` or install openadapt-privacy. If scrubbing fails,
  no screen is shared and self-repair stops. `allow_unscrubbed` must be set
  explicitly and is recorded on the candidate. `ask_each_time` is not
  available for self-repair yet.

## CLI

```bash
openadapt-flow run bundles/triage --config deploy.yaml --url "$APP_URL" --run-dir runs/item-17 --repair-agent mypkg.agents:make_agent --repair-screen-access-by ops-lead --repair-fault-url "$FAULT_URL"
```

With the deployment's `repair` section set, the flags aren't needed. A flag
wins over the deployment file. `run` admits the bundle as usual. The first
run uses the same authorization and approval gate as a run without repair. Probes and the resumed run
admit the patched bundle through the same run gate settings, with a fresh
authorization each. They don't use the deployment's approval mode: the
person's answer to the repair request is the approval for the item's one
save, and the resume refuses the save unless the form is the one they saw.

| Flag | `deploy.yaml` `repair:` key | Default | Meaning |
| --- | --- | --- | --- |
| `--no-auto-repair` | `auto: false` | on | Leave stops for a person. |
| `--self-repair` | none | off | Require repair: refuse a run that can't be repaired. |
| `--repair-agent MODULE:NAME` | `agent` | none | An agent object, class, or factory. Repair is on when one is set. |
| `--repair-screen-access-by NAME` | `screen_access_by` | required | Who allowed the agent to see screens. |
| `--repair-screen-share-mode` | `screen_share_mode` | `scrubbed_only` | Or `allow_unscrubbed`. |
| `--repair-model-provider`, `--repair-model-key-file` | `model_provider`, `model_key_file` | none | Load the customer's own key. |
| `--repair-store DIR` | `store` | `repair-store` | The repair store, and the stop ledger. |
| `--repair-attempts N` | `attempts` | 3 | Proposals per halt. |
| `--repair-fault-url URL` | `fault_urls` | none | A fault probe; repeatable. |
| `--repair-approval-wait SECONDS` | `approval_wait_s` | 900 | How long to wait for the person. |

Repair is off for a run bound to the exact original bundle, and for a
worklist run (use `WorklistQueue`), because a stop there may come after
earlier items saved. With `--self-repair` such a run is refused; otherwise it
runs plainly. The bindings are:

- a signed qualification admission (`--qualification-authority-file`);
- a managed dispatch;
- a qualification case;
- `--pin-digest`.

In those cases the approved bundle needs its own admission first. Under the
CLI, the Standard profile requires a signed qualification admission. So a CLI
self-repair runs without a named profile or under Demo, and the record check
still runs. The Python API can run the Standard profile contract directly, as
the tutorial does.

## What is recorded

| Where | What |
| --- | --- |
| `report.json` of the run that resumed, and of the halted run | `self_repair`: status, every stage (`halt`, `propose`, `patch`, `probe`, `campaign`, `approval`, `resume`), the agent's id, dialect, model calls and estimated cost, the candidate id, the digests, and who approved. A healthy run records `status: not_needed`, 0 model calls, $0. |
| `REPORT.md` | A **Self-repair** section with the same stages. |
| `<run_dir>/self-repair/self-repair.json` | The same ledger, for every status except `not_needed`. |
| `<run_dir>/self-repair/attempt-N/` | The patched bundle, its heal evidence, and the probe's run. |
| `<run_dir>/self-repair/fault-N/`, `resume/` | Each fault probe's run, and the resumed run. |
| `<run_dir>/item-approval/` | The request, the screenshot the person saw, and the answer. |
| `<run_dir>/self-repair/preflight/` | The test record's run of the repaired bundle, when a fix applied without a person. |
| The repair store | The candidate, its campaigns, its transitions, who reviewed and approved it, and its canary metrics. |
| `<repair-store>/auto-repair/ledger.jsonl` | One row per stop: its route, what the agent proposed, the probe, the approval and its wait, the resumed outcome, model calls and cost. |

The run's own `model_calls` stays the count for the deterministic run, which
is 0. The agent's calls are counted in `self_repair.model_calls`.

## Driving it in a demo

MockMed's `redesign` drift (`?drift=redesign`) replaces the save control: the
label changes to `Submit to chart`, the DOM id changes to `submit-encounter`,
and the control moves to the right. It also adds a `Submit feedback` button
outside the patient record. Without the query, MockMed is unchanged.

The `relink` drift (`?drift=relink`) changes a control that is not the save:
`New Encounter` on the patient screen becomes `Start visit`, with a new DOM
id (`start-visit`), on the other side of the screen. A policy with
`relabel_or_move` repairs it without a person; `redesign` always goes to one,
because it changes the save.

1. Record and compile the tutorial bundle against the persistence boundary
   (`openadapt_flow.mockmed.fault_server.serve`,
   `openadapt_flow.tutorial.record_tutorial`, `compile_recording(...,
   mine_effects=True)`).
2. Run it with `drift=redesign`. It halts at `step_005` with
   `HALTED_BEFORE_EFFECT`, and `/api/db` holds no record.
3. Give `SelfRepair` an agent that proposes the `Submit to chart` control and
   a fault probe with `drift=redesign,missing`.
4. Answer on the phone, or call
   `openadapt_flow.console.save_approvals.decide_save_approval` as the tests
   do. The resumed run is `VERIFIED`, and `/api/db` holds one record.
5. To show a refusal, have the agent propose `Consult` (refused before the
   probe) or `Submit feedback` (the probe's record check stops it before the
   save).

`tests/test_self_repair.py` does all of this with a scripted agent in about 40
seconds.

Two more drifts need a bridge:

- `?drift=menu` moves the save behind a menu: **Actions → Billing → Submit
  encounter**. The run halts at the save (`replace`). A bridge of
  `#actions-menu`, `#menu-billing`, `#menu-submit` adds two new steps and
  re-targets the save.
- `?drift=provider` adds a required **Ordering provider** screen after New
  Encounter. The run halts there (`after`). A bridge of `#provider-1`,
  `#provider-use` adds two new steps. The screen also has a `Save as draft`
  button and an optional note field, for the refusals: clicking `Save as
  draft` is refused, and so is typing a note that is not a parameter value.

`tests/test_self_repair_bridge.py` runs both bridges to `VERIFIED` and the
three refusals (a bridge that would save, one that types an invented value,
one longer than three actions).

## Standard changes: repairs without a person

In ITIL, a *standard change* is a low-risk change that a person approves
once, as a class. Flow uses the same idea for UI drift. A person signs a
repair policy once. After that, a fix of a class the policy names applies
without anyone being asked, and every other fix goes to a person as above.
No model acts on a live record unchecked. Each fix is checked against the
admitted bundle and run on a test record before it applies, and any item run
afterward still checks the record and reads the save back.

Module: `openadapt_flow.repair.standard_change`. Queue:
`openadapt_flow.repair.preflight_queue`. Tests:
`tests/test_standard_change.py`.

### The policy

The repair policy is the `standard_repairs` field of the signed admission
policy (`openadapt_flow.jit.authority.AdmissionPolicy`). It is signed and
verified the same way: a registered policy signer signs it
(`openadapt-flow jit sign-policy`), and Flow checks the signer, the signature
and the expiry before every use. A changed policy needs a new signature.
Policies signed before this field existed keep their signatures.

```json
"standard_repairs": {
  "repair_agents": ["agent:my-repair"],
  "workflows": ["triage"],
  "relabel_or_move": true,
  "menu_steps": 2,
  "dialogs": [
    {"name": "survey", "text": "quick survey", "dismiss": ["Not now", "Close"]}
  ]
}
```

| Class | Allows | Field |
| --- | --- | --- |
| `relabel_or_move` | One control that is not the save got a new label, place or locator. | `relabel_or_move: true` |
| `dismiss_dialog` | One new click dismisses a new dialog. The dialog's text matches a declared pattern (a regular expression), and the click's label is one of its declared dismiss labels. | `dialogs` |
| `menu_path` | A control that is not the save moved into a menu, reached by at most `menu_steps` new clicks. | `menu_steps` (0 to 3) |

The signer can't be one of the repair agents. A dismiss label that reads as a
save (for example `Submit`) is refused when the policy is built. The
`dismiss_dialog` and `menu_path` classes judge fixes that add steps, which
self-repair proposes as a bridge (see [What happens when a run
halts](#what-happens-when-a-run-halts)). A bridge that fits neither class,
such as choosing a value in a new dialog or reaching the save through a menu,
goes to a person.

### When a fix applies without a person

After the probe and both campaigns pass, Flow applies the fix without a
person only when every one of these holds:

1. The policy verifies: a registered signer, a valid signature, not expired.
   It names the repair agent and covers the workflow.
2. The save step, its identity evidence and its record check are
   byte-identical to the admitted bundle, including every file they use (the
   save's template, for example). No new step saves.
3. No step's identity evidence changed, and the patch weakens nothing in the
   contract.
4. The change fits an allowed class, and it touches exactly one recorded
   step, which is a click. No recorded bundle file changed: the manifests'
   plaintext hashes are compared, so encrypted bundles are covered too.
5. The new control, and every new menu click, has a readable label of its
   own (from its accessible name or OCR, never the recorded intent). That
   label doesn't read as a save, or as an action like sign, void, delete,
   send, approve, cancel or close. A dismiss label is checked against the
   labels the person declared.
6. No automatic repair of this step was reverted, rolled back or stopped by
   its canary since a person last cleared the step (see
   [Undo](#audit-and-undo)).
7. The repaired bundle runs once, start to finish, on the deployment's test
   record (`TestRecord`, for example `TEST-001`). The run ends `VERIFIED`,
   with the record check reading the save back.

If any check fails, the run's self-repair ledger records the reason as a
failed `approval` stage ("not a standard change ..."), and a person is asked
as usual. The repair store checks items 1 to 6 again itself against the
bundles on disk (`RepairStore.approve_standard_change`). It also reads the
test record's `report.json` and refuses unless that run is `VERIFIED` and ran
exactly the proposed bundle. A caller can't skip a check. The approval names the policy, binds its digest, and records the
person who signed it.

```python
from openadapt_flow.jit.authority import SignedAdmissionPolicy
from openadapt_flow.repair.standard_change import StandardChanges, TestRecord

test_record = TestRecord("TEST-001", test_record_target)
standard = StandardChanges(
    SignedAdmissionPolicy.load("repair-policy.signed.json"),
    key_dir=Path("~/.openadapt/jit").expanduser(),
    test_record=test_record,
)
repair = SelfRepair(agent, target, store="repair-store",
                    screen_access=access, standard_changes=standard)
```

`test_record_target` is a `ReplayTarget` whose sessions open the test record,
with the test record's parameters.

### A worklist behind a pre-flight

`WorklistQueue` runs one bundle over a queue of items:

1. **Pre-flight.** Before item 1, the bundle runs on the test record with the
   full read-back. The queue starts only when that run is `VERIFIED`. A drift
   the pre-flight finds never reaches a real record.
2. **Repair on the test record.** When the pre-flight or an item halts on a
   changed screen, self-repair runs on the test record, never on a real one.
   An allowed fix applies after its own test-record run. Any other fix asks
   one person, and the phone card shows the test record's form, not a
   patient's.
3. **One approval releases the queue.** Items that halted at the same step
   for the same reason wait together. The card says how many it releases
   ("Approving also releases 5 queued item(s) halted at step_005"). One
   answer releases them all. Each released item re-runs from the start with
   the repaired bundle and its own record check, and each run counts in the
   repair's canary window.

The queue stops when the repair in use is reverted or leaves its canary
window. An item that halts for any other reason stays halted for a person.

```python
from openadapt_flow.repair.preflight_queue import QueueItem, WorklistQueue

queue = WorklistQueue("bundles/triage", test_record, "runs/worklist-2026-10-18",
                      self_repair=lambda target: SelfRepair(agent, target, ...))
result = queue.run([QueueItem(item_id, target_for(item_id)) for item_id in ids])
print(result.status, [(i.item_id, i.status) for i in result.items])
```

### Audit and undo

| Where | What |
| --- | --- |
| `report.json`, `self_repair.standard_change` | The class, what changed, the policy id, its digest, its signer, the test record, the test record's outcome, and the undo command. `approval_route` is `standard_change`. |
| `REPORT.md` | "Applied without a person" and "Undo" lines in the **Self-repair** section. |
| The run receipt | `standard_change`: the class, the policy digest and the candidate id. Nothing else. |
| `repair-store/standard-changes/ledger.jsonl` | One row per automatic repair, and one per revert. |
| `openadapt-flow repair standard-changes` | The daily digest: every automatic repair of the day, whether it is still in use, and its undo command. |

To undo one:

```bash
openadapt-flow repair revert <candidate-id> --store repair-store --by <your-name>
```

The prior bundle is active again. Only the repair in use can be reverted, so
reverting an older one never drops a newer one silently: revert the newer one
first. Repairs of the same step then go to a person until someone runs
`openadapt-flow repair clear-hold`. A `repair rollback` or a canary
regression holds the step the same way. Every automatic repair is written to
the ledger before its bundle goes live.

The phone portal does the same through the attended console. Start it with
`openadapt-flow console --attend --repair-store repair-store`.
`GET /api/standard-changes` lists the rows, with `revert` offered only where
it applies, and `POST /api/standard-changes/<id>/revert` reverts one. The
phone sends back the digest of the row it showed.

### Every backend

The rules compare the two bundles step by step, field by field and file by
file. They don't read the DOM, so they work the same for web, Windows, macOS,
Linux, RDP and Citrix bundles. The tests apply every class and refusal to the
recorded MockMed bundle as a browser sees it, as a pixel-only RDP kiosk sees
it, and with WinForms-style UI Automation ids. The live tests (a repair, a
pre-flight, a batch release) run on MockMed in a browser. No test drives a
live RDP or Citrix session.

## Measured

The default loop was measured with a real Claude repair agent
(`claude-sonnet-5-5`) and a scripted approver that approves every request
that reaches it, so a bad fix that passed the probe would have been saved.
Data, scripts and the full per-mode table are in
[benchmark/auto_repair](../benchmark/auto_repair/README.md).

| Where | Stops | Reached repair | Agent proposed | Passed the probe | Approved | Resumed and verified | Wrong saves | Person decisions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MockMed, 22 drift modes, 3 trials each (66 runs) | 30 | 21 | 12 | 6 | 6 | **6 of 21** | 0 | 6 |
| MockMed worklist, `relink` and `redesign`, 5 items each (the change ships after the pre-flight) | 10 | 10 | 2 (one repair per group, on the test record) | 2 | 2 | **10 of 10** | 0 | 2 |
| OpenEMR referral, #1347's recorded runs | 8 | 0 (all `RECONCILIATION_REQUIRED`) | - | - | - | 0 | - | - |
| openIMIS relabelled Save (#1331), before #1337 | 3 | 0 (all `RECONCILIATION_REQUIRED`) | - | - | - | 0 | - | - |

What worked and what didn't:

- **Repaired:** a renamed and moved control that opens the form (`relink`,
  3 of 3) and a redesigned save with a decoy next to it (`redesign`, 3 of 3).
  Each took one model call ($0.013 to $0.015) and one tap.
- **Not repaired, and nothing written:** a save moved into a closed menu
  (`menu`: the agent sees only "Actions"), a new interstitial (`notice`: the
  fix needs two clicks and the second control isn't on screen yet), a new
  required choice (`provider`: the agent declined to choose), and pixel
  postconditions that failed after a font or zoom change (`font`, `zoom`:
  false stops no click can fix). These need a look at more than one screen
  (the staging-copy bridge search of
  [#1302](https://github.com/OpenAdaptAI/OpenAdapt/pull/1302)), or a fix to
  the check itself.
- **Correctly never repaired:** a missing, absent or duplicated record (9
  stops) went to a person.
- **OpenEMR:** every recorded stop came after "Create New Patient", which the
  run gate counts as a save, so all 8 went to reconciliation. Repair can't
  help these; the fix for #1347 is the identity check's cause.
- **openIMIS:** before #1337 the relabelled Save stopped on the scroll before
  it with `RECONCILIATION_REQUIRED`, 3 of 3. The live re-run with #1337 and
  the risk review is not done yet.

The approver was scripted, so its answer times (milliseconds) are not a
person's. The measure of a person's time here is the count of decisions: one
tap per repaired stop, or one per group of worklist items that stopped the
same way.

## Limits

- Only a stop that wrote nothing is repaired. A stop after a step the run gate
  counts as a save has acted is `RECONCILIATION_REQUIRED` and goes to
  reconciliation, even when the app saved nothing; see [Measured](#measured)
  for OpenEMR, where every recorded stop was of this kind.
- The CLI repairs a single run. A worklist that stops after earlier items
  saved is not repaired by `run`; use `WorklistQueue`, which repairs on the
  test record and releases the waiting items.
- One halted click per run is repaired, by one corrected click or a bridge of
  up to three actions, in a linear workflow with exactly one save. When the
  patched run halts at another step, the probe fails with that reason.
- A bridge's new clicks need a DOM identity (a browser) and a readable,
  non-saving label. A bridge acts on the halted page while it is captured;
  each action is checked first, and the page is discarded afterwards.
- The probe stops before every step the run gate classifies as a save, the
  same fail-closed classifier the approval gate uses. A step that writes but
  that the classifier misses would run during a probe.
- The CLI supports browser runs that Flow launches. The Python API takes any
  backend whose sessions you can open, but the DOM-based checks
  (`same_fields`, the structural locator) apply only in a browser. Elsewhere
  `same_fields` falls back to on-screen text.
- An encrypted bundle is written back encrypted, but this path has no test.
- A fault probe must present a real fault. The campaign records what each
  probe's run did, and its quality is only as good as the faults the operator
  supplies.
