# Fall back to the agent when a wrapped program drifts

A wrapped task runs its compiled program with no model calls. When the
program stops, the default is to hold the task: every later call returns
`CACHE_HALTED`, the agent is not run again, and a person reconciles, repairs
or releases the hold. That stays the default.

For a task where a person would rather have the agent finish the work than
wait, the fallback is an opt-in per task. It hands the task back to the agent
only when the program's stop is proved to have written nothing, and the agent
runs with a person approving each save.

```bash
openadapt-flow wrap-dispatch --task task.json --request-id r-118 \
    --param note="Synthetic follow-up" \
    --fallback agent --fallback-max-cost-usd 0.50
```

In Python, wrap the task's `SkillCache`:

With no terminal (a service), a person approves each save from a paired phone
or with `openadapt-flow item-approval approve` instead:

```bash
openadapt-flow wrap-dispatch --task task.json --request-id r-118 \
    --param note="Synthetic follow-up" \
    --fallback agent --fallback-max-cost-usd 0.50 \
    --fallback-gate item-approval --fallback-approvals-dir PORTAL_RUNS_ROOT
```

In Python, wrap the task's `SkillCache`:

```python
from openadapt_flow.wrap.fallback import FallbackPolicy

policy = FallbackPolicy(
    cache, max_cost_usd=0.50, agent_factory=make_agent
)  # gate: the terminal confirm
run = policy.run(prompt, params, None)
```

`agent_factory` is called only once a fallback may start; without it the
policy uses the agent `run` is given. Either way, `run` only replays the
admitted program: with none it returns `NO_ADMITTED_PROGRAM` and starts
nothing.

## When the agent gets the task

All of these must hold. Each is read from files on disk, not from the reply's
status text:

- The cache's hold is a terminal halt (`phase: halted`). A hold left in
  `running` means a reply was lost and a save may exist.
- The halted run's `report.json` belongs to the admitted bundle: its workflow
  name, its bundle digest, the digest in the hold and the digest in the
  verified admission all match.
- The report says `HALTED` and `HALTED_BEFORE_EFFECT`, and
  `steps_requiring_reconciliation` (from `openadapt_flow.transaction`) lists
  no step.
- The halted reply recorded exactly zero model calls.
- The cache's own record check of the halted run read the system of record
  both before and after (`readable: true`) and found no new, changed or
  removed record of the task's record, and no change to any other record
  during the run (`outside_run`; it may be the program's own write
  elsewhere, so another user's write also stops the fallback). An
  unreadable store also reports zero changes, so it never counts as
  "nothing written". The policy keeps that
  check beside the halt, so a later call that finds the task held can fall
  back on the same halt.

It never falls back on `RECONCILIATION_REQUIRED`, a lost reply,
`RECORD_CHECK_FAILED`, `RECEIPT_NOT_VERIFIED`, `MODEL_CALLS_NOT_ZERO`, a
missing baseline, or a halt the policy did not see with a clean record check.
Those stay held, exactly as without the policy.

These are the checks `SkillCache.open_repair` makes before it offers a
repair, without its extra rule that the fix is a one-step target
replacement. A drift that needs more than one new step can fall back; it
then gets no repair candidate.

## What the fallback run is

The fallback is a run 1 through compile-agent's harness, in its own
directory beside the task's cache (`<cache_dir>/fallback/<task>/<request>/`):

- the right-record check before every save;
- a pre-commit gate before every save. The default `TerminalGate` is
  compile-agent's terminal confirmation; with no terminal it refuses, and the
  agent is not started (`FALLBACK_GATE_UNATTENDED`). `ItemApprovalGate`
  (below) needs no terminal. Any object with `__call__(request) -> bool` can
  be the gate;
- the app's origin fence;
- a read-back of every save from the system of record, and the record check
  of run 1;
- its own spend cap, `--fallback-max-cost-usd` (no default). The policy sets
  the agent's own cap to the lower of the two for the run, and the gate
  refuses a save when the spend so far is over the cap or unknown. An agent
  with model calls and no settable cap is not started
  (`FALLBACK_COST_UNBOUNDED`);
- its own request ID, which runs once. `wrap-dispatch` derives it from the
  dispatch request ID by digest (`fallback-` and 40 hex characters), so any
  valid request ID (up to 128 characters, any characters) works, and records
  the dispatch request ID as `parent_request_id`.

A fallback is `VERIFIED` only when run 1's record check passed, every gate
was approved by the person, and a final task-scoped read (the baseline taken
as the run started against a fresh read after it) confirms exactly one new
record of the task's record with its values and nothing else changed on it.
When the final read disagrees (a duplicate, a changed or removed record, an
unreadable store), or a stopped run can't show it wrote nothing (including a
change to another record), the status is
`RECONCILIATION_REQUIRED`: no repair is opened, the in-flight marker stays,
and `fallback.run_status` keeps what the run itself reported, beside both
readings (`record_check` and `fallback.task_record_check`).

The run's draft program is never confirmed or admitted. A verified fallback
leaves the task held and opens a repair candidate with
`SkillCache.open_repair` when the halt is a one-step target replacement. The
repair token is kept in `repair.json` in the run directory (mode 0600). A
person demonstrates and approves the repair, and
`SkillCache.complete_repair` runs its canaries and selects it. Nothing in the
fallback path does either step.

While a fallback may save, an in-flight marker names it: its request ID, a
nonce, its process ID and host. A fallback that raises, or ends without
proving what it wrote, leaves the marker, marked `ended`. Every later
fallback of the task then returns `RECONCILIATION_REQUIRED` until a person
checks the record and runs
`wrap-dispatch release TASK --person NAME --after-reconciling`. A run removes
the marker only while it is still its own.

## The gate without a terminal

`--fallback-gate item-approval` (`openadapt_flow.wrap.approval_gate.ItemApprovalGate`)
asks through the item-approval store that `each_item` runs and the agent
guard use. Before each save, the fallback run writes
`item-approval/request-<id>.json` with the gate screenshot under
`--fallback-approvals-dir/<fallback request id>/` (default: the fallback
run's directory), and waits up to `--fallback-approval-wait-s` (default 60).
A person answers on a paired phone through the decision portal, when the
portal's runs root holds that folder, or with
`openadapt-flow item-approval approve` and their registered approver key.
Desktop's Run it screen lists only runs Desktop started, so it does not show
these requests yet.

The save goes through only for an answer decided by a person, signed by an
approver key on the pinned approver roster, for that exact request, before
it expired, and not by the agent that asked. A decline, no answer in time, an
answer after expiry, an unsigned answer, a key off the roster and a missing
screenshot all refuse the save. The pin is `--fallback-approver-root`,
`OPENADAPT_APPROVER_ROOT`, or the `approvers` section of
`--fallback-approvers-config` (then the only pin counted). With no pin whose
roster verifies, no person's answer could count, so the agent is not started
(`FALLBACK_GATE_UNATTENDED`).

The agent's own run deadline keeps running while it waits at the gate: a
provider agent's run is at most 120 seconds (`--max-run-seconds`), so the
wait must be shorter than that, and the person has less time than the wait
when the agent is slow to reach the save. Whatever happens at the deadline,
the final read decides the outcome as above.

## The dispatcher

`openadapt-flow wrap-dispatch` runs only the admitted program:

| Situation | Result `path` | `status` |
| --- | --- | --- |
| The program ran | `program` | its status (`VERIFIED`, `HALTED` ...) |
| No admitted program, including one evicted after a `status` check | `held` | `NO_ADMITTED_PROGRAM` (nothing runs; `openadapt wrap` makes the first run) |
| The task is held, no fallback | `held` | `CACHE_HALTED` |
| The fallback ran or was refused | `agent_fallback` | `VERIFIED`, `GATE_REFUSED`, `COST_CAP_EXCEEDED` ... |

Each run prints one JSON object with `openadapt wrap`'s per-run fields
(`request_id`, `run_id`, `status`, `verified`, `model_calls`, `cost_usd`,
`usage`, `usage_known`, `seconds`, `wire_checked`, `receipt`,
`receipt_outcome`, `receipt_binding`), with `path` as in the table, plus
`outcome` (`VERIFIED`, `ESCALATED`, `CAUGHT_ERROR` or `FAILED`) and
`fallback` (what the policy did, or null). `--result FILE` also writes it to
a file. Requests go through the same run store as `openadapt wrap`, and the
stored result is this whole object, `NO_ADMITTED_PROGRAM` included: a retried
request ID returns its first result, `fallback` and all, and runs nothing.

The hold verbs:

- `wrap-dispatch status TASK`: the hold's phase and reason, whether an
  admitted program is served, whether the hold may fall back and why, and
  the task's fallback runs.
- `wrap-dispatch release TASK --person NAME`: a person at a terminal types
  `release`. The hold file is archived next to a record of who released it.
  Without `--after-reconciling`, only a hold whose own evidence proves
  nothing was written is released: a halt that meets every check above,
  including the kept record check (`wrap-dispatch` keeps it for a halt it
  saw, with or without `--fallback agent`). Any other hold (a
  `RECONCILIATION_REQUIRED` reply, a write, an unreadable read, `running`,
  unreadable) and an unfinished fallback need `--after-reconciling`. A
  fallback that is still running, or whose owner is on another host or
  can't be identified, is never released (`FALLBACK_RUNNING`).
- `wrap-dispatch evict TASK`: sets the program aside as evidence; the next
  `openadapt wrap` run compiles afresh.

## Not covered yet

- Compiled browser runs over MCP need the worker-thread fix in
  [#2406](https://github.com/OpenAdaptAI/OpenAdapt/pull/2406) under mcp
  1.30.0. The end-to-end test runs the program in-process until then.
- `openadapt wrap run` does not dispatch through this policy yet (#2407).
- A program hold left `running` by a live program replay is not told apart
  from a lost reply; release with `--after-reconciling` can archive it while
  that replay still runs.
- A fallback marker whose owner is on another host, or that can't be read,
  has no release command: a person checks the record and removes
  `in-flight.json` by hand.
- The gate's wait is checked against `--max-run-seconds`, not against the
  time the agent took to reach the save. An approval that arrives after the
  agent's run deadline can still let the click through; the final read then
  decides the outcome.
- The item-approval gate's request shows the gate screenshot and the
  control; it does not list the field values being saved (no `fields`).
- No real model agent has run the fallback; the benchmark's agent is
  scripted.
- Fallback runs have no receipt of their own (#2394); the evidence is the
  harness's gate log, read-backs and record check.

Measured on MockMed: [benchmark/wrap_fallback](../benchmark/wrap_fallback/README.md).
