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

```python
from openadapt_flow.wrap.fallback import FallbackPolicy

policy = FallbackPolicy(cache, max_cost_usd=0.50)  # gate: the terminal confirm
run = policy.run(prompt, params, agent)
```

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
- The cache's own record check of the halted run found no new, changed or
  removed record of the task's record. The policy keeps that check beside the
  halt, so a later call that finds the task held can fall back on the same
  halt.

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
  agent is not started (`FALLBACK_GATE_UNATTENDED`). Any object with
  `__call__(request) -> bool` can be the gate;
- the app's origin fence;
- a read-back of every save from the system of record, and the record check
  of run 1;
- its own spend cap, `--fallback-max-cost-usd` (no default). The policy sets
  the agent's own cap to the lower of the two for the run, and the gate
  refuses a save when the spend so far is over the cap or unknown. An agent
  with model calls and no settable cap is not started
  (`FALLBACK_COST_UNBOUNDED`);
- its own request ID (default `<request-id>.fallback`), which runs once.

The run's draft program is never confirmed or admitted. A verified fallback
leaves the task held and opens a repair candidate with
`SkillCache.open_repair` when the halt is a one-step target replacement. The
repair token is kept in `repair.json` in the run directory (mode 0600). A
person demonstrates and approves the repair, and
`SkillCache.complete_repair` runs its canaries and selects it. Nothing in the
fallback path does either step.

A fallback that raises, or ends without proving what it wrote, leaves an
in-flight marker. Every later fallback of the task then returns
`RECONCILIATION_REQUIRED` until a person checks the record and runs
`wrap-dispatch release TASK --person NAME --after-reconciling`.

## The dispatcher

`openadapt-flow wrap-dispatch` runs only the admitted program:

| Situation | Result `path` | `status` |
| --- | --- | --- |
| The program ran | `program` | its status (`VERIFIED`, `HALTED` ...) |
| No admitted program | `held` | `NO_ADMITTED_PROGRAM` (nothing runs; `openadapt wrap` makes the first run) |
| The task is held, no fallback | `held` | `CACHE_HALTED` |
| The fallback ran or was refused | `agent_fallback` | `VERIFIED`, `GATE_REFUSED`, `COST_CAP_EXCEEDED` ... |

Each run prints one JSON object with `openadapt wrap`'s per-run fields
(`request_id`, `run_id`, `status`, `verified`, `model_calls`, `cost_usd`,
`usage`, `usage_known`, `seconds`, `wire_checked`, `receipt`,
`receipt_outcome`, `receipt_binding`), with `path` as in the table, plus
`outcome` (`VERIFIED`, `ESCALATED`, `CAUGHT_ERROR` or `FAILED`) and
`fallback` (what the policy did, or null). `--result FILE` also writes it to
a file. Requests go through the same run store as `openadapt wrap`, so a
retried request ID returns its first result.

The hold verbs:

- `wrap-dispatch status TASK`: the hold's phase and reason, whether an
  admitted program is served, whether the hold may fall back and why, and
  the task's fallback runs.
- `wrap-dispatch release TASK --person NAME`: a person at a terminal types
  `release`. The hold file is archived next to a record of who released it.
  A hold that may have written (`running`, unreadable) or a stuck fallback
  needs `--after-reconciling`.
- `wrap-dispatch evict TASK`: sets the program aside as evidence; the next
  `openadapt wrap` run compiles afresh.

## Not covered yet

- Compiled browser runs over MCP need the worker-thread fix in
  [#2406](https://github.com/OpenAdaptAI/OpenAdapt/pull/2406) under mcp
  1.30.0. The end-to-end test runs the program in-process until then.
- `openadapt wrap run` does not dispatch through this policy yet (#2407).
- The gate is the terminal confirmation. The guard approval card and the
  flight gate are the next gates.
- Fallback runs have no receipt of their own (#2394); the evidence is the
  harness's gate log, read-backs and record check.

Measured on MockMed: [benchmark/wrap_fallback](../benchmark/wrap_fallback/README.md).
