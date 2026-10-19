# Self-repair

When a vendor changes a screen, a compiled workflow halts before it writes
anything. The save button has a new label, a new place, or a new DOM id, and
none of the recorded ways to find it work. Without self-repair, the run stops
and waits for a person to re-teach the step.

Self-repair closes that loop. A computer-use agent proposes a fix for the
halted step. Flow replays the fix up to the save without saving, and checks
that the form and the record are the same as in the recording. Then one
person accepts the fix with one tap, on the phone or at a terminal, and the run
resumes. Healthy runs never call the agent and record zero model calls.

Module: `openadapt_flow.repair.self_repair`. CLI: `openadapt-flow run
--self-repair`. Tests: `tests/test_self_repair.py`,
`tests/test_self_repair_cli.py`.

## What happens when a run halts

1. **Halt.** The run halts as usual. Self-repair starts only when all of
   these hold:
   - no input reached a step that saves, and no effect was observed;
   - the halted step is a click that never acted;
   - the halt is one of the ladder's own two refusals: it found no target, or
     it found the target only by geometry and refused to act on that;
   - the workflow has exactly one save (for now).

   A halt on the record check (an identity mismatch) is never repaired, and
   neither is an item a person declined at the approval gate.
2. **Propose.** The repair agent is shown the step's intent, the recorded
   evidence (the target crop, its label, its place) and the live screen. It
   proposes one action in its own dialect. Its model calls and their cost go
   into the ledger.
3. **Patch.** Flow builds a patched anchor from the live screen at the
   proposed point: a new template crop, the DOM locator under the point, and
   the label read there. Identity evidence is copied unchanged, so the patch
   can't weaken the record check (the repair invariants check this again).
   Flow refuses the proposal when:
   - the step saves and the new control does not read as a save (for example,
     `Consult` for a step recorded on `Save Encounter`);
   - the step does not save and the new control reads as a save;
   - the new control is one that another step already uses.
4. **Probe.** The patched bundle replays in a fresh session, admitted by the
   same run gate with its own authorization. A gate stops it at the last point
   before the first save, after the target and the record check resolved.
   Nothing is written. The probe passes only when every check passes:

   | Check | Passes when |
   | --- | --- |
   | `nothing_written` | No earlier step that saves received input. |
   | `target_found` | The deterministic ladder found the patched target by its DOM locator, template, or label, not by geometry or a model. |
   | `same_record` | Every identity check in the probe verified, including the save's own. |
   | `same_fields` | Each value the workflow typed is in a form field. |
   | `same_form` | The form is exactly as it was when the run halted (when the halt was at this save). |

   A failed probe goes back to the agent with the reason, up to
   `max_attempts` proposals.
5. **Campaigns.** A passing probe becomes an ordinary repair candidate in the
   [repair store](REPAIR_LIFECYCLE.md): source `model_suggestion`, provenance
   `repaired_by: self_repair`, with the agent's id. The replay and fault
   campaigns run before anyone is asked:
   - When the step's identity can be read from pixels (an OCR band or
     identity tokens), Flow runs the existing frame campaigns, plus the live
     probe as one more healthy case.
   - When the step's identity is read from the DOM, the frame campaigns can't
     judge it. The live probe is the healthy case, and each fault probe is an
     adversarial case: a session that presents a fault (for example, the
     record is missing) and must stop before the save. Without fault probes
     the candidate stops here with status `needs_fault_probes`.
6. **Approve.** Flow writes one approval request into the halted run's
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

- **One action.** A proposal is exactly one click or double click, matching
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
openadapt-flow run bundles/triage --config deploy.yaml --url "$APP_URL" --run-dir runs/item-17 --self-repair --repair-agent mypkg.agents:make_agent --repair-screen-access-by ops-lead --repair-fault-url "$FAULT_URL"
```

`run` admits the bundle as usual. The first run uses the same authorization
and approval gate as without `--self-repair`. Probes and the resumed run
admit the patched bundle through the same run gate settings, with a fresh
authorization each. They don't use the deployment's approval mode: the
person's answer to the repair request is the approval for the item's one
save, and the resume refuses the save unless the form is the one they saw.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--self-repair` | off | Turn on self-repair for this run. |
| `--repair-agent MODULE:NAME` | required | An agent object, class, or factory. |
| `--repair-screen-access-by NAME` | required | Who allowed the agent to see screens. |
| `--repair-screen-share-mode` | `scrubbed_only` | Or `allow_unscrubbed`. |
| `--repair-model-provider`, `--repair-model-key-file` | none | Load the customer's own key. |
| `--repair-store DIR` | `repair-store` | The repair store. |
| `--repair-attempts N` | 3 | Proposals per halt. |
| `--repair-fault-url URL` | none | A fault probe; repeatable. |
| `--repair-approval-wait SECONDS` | 900 | How long to wait for the person. |

The CLI refuses to start self-repair when the run is bound to the exact
original bundle:

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
| The repair store | The candidate, its campaigns, its transitions, who reviewed and approved it, and its canary metrics. |

The run's own `model_calls` stays the count for the deterministic run, which
is 0. The agent's calls are counted in `self_repair.model_calls`.

## Driving it in a demo

MockMed's `redesign` drift (`?drift=redesign`) replaces the save control: the
label changes to `Submit to chart`, the DOM id changes to `submit-encounter`,
and the control moves to the right. It also adds a `Submit feedback` button
outside the patient record. Without the query, MockMed is unchanged.

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

## Limits

- Only clicks on linear steps are repaired, one halted step per run, in a
  workflow with exactly one save. When the patched run halts at another step,
  the probe fails with that reason.
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
