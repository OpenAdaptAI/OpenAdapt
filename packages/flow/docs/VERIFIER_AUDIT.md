# Verifier audit

A benchmark or RL environment turns a finished episode into a reward with its
own scorer. If the scorer accepts a wrong end state, an agent trained or ranked
on it learns or gets credit for that wrong behavior. The verifier audit tests
the scorer, not an agent.

The method comes from *Finding Blind Spots in AppWorld and WorkArena Task
Verifiers* (Abrich, 2026). This module generalizes it to any environment that
can reset, replay a trajectory, score, and read back its own state.

## How a cell is built

1. The target gives a **reference trajectory** for each task: steps the
   environment's own scorer accepts. Typical sources are the shipped ground
   truth or a recorded solution.
2. The kit replays the reference twice from a fresh reset. A task is excluded
   if the scorer rejects the reference, if the reference effect differs
   between the two replays, or if it has no effect at all (an answer-only
   task).
3. Each **fault family** edits the reference mechanically. A family never sees
   a scorer verdict.
4. For each edit, the kit first runs a **census replay without scoring**. It
   keeps the edit only if the environment accepted the edited call and the end
   state differs from the reference. An edit whose effect matches the
   reference (an idempotent duplicate, a field the app ignores) is
   **degenerate** and is listed, never counted.
5. The kept edit is replayed again from a fresh reset and scored once.

Ground truth comes from the target's **effect read**: a diff of the end state
against the start state, taken through a path that does not run the scorer
(direct SQLite reads for AppWorld, a model dump for AgentDojo). A harmful-family
cell whose effect differs from the reference and that the scorer accepts is a
**false accept**. An incidental-change cell that the scorer rejects is a
**false reject**.

## Families

| Family | Polarity | Edit |
|---|---|---|
| `unauthorized_side_effect` | harmful | Keep every task write, and add a copy of one aimed at a record the task never touches |
| `wrong_record` | harmful | One task write lands on a different record of the same type |
| `wrong_value` | harmful | One task write carries a changed non-identity value |
| `duplicate` | harmful | One task write is sent twice |
| `partial_write` | harmful | A multi-write task sends only its first write, or drops its last |
| `late_write` | harmful | The last write is sent after the success claim, so the claim was false when made |
| `optimistic_success` | harmful | Reads and the claim, no writes |
| `incidental_change` | legitimate | Permitted extra calls (the reference's reads, repeated) before the claim |

Identity arguments are recognized by name (`*_id`, `email`, `recipient`,
`iban`, URL path ids and so on). Retargets draw from values that other tasks in
the suite use for the same argument, skipping any value the task's own
reference uses. `wrong_value` and `wrong_record` cells also record whether the
original value appears as a whole word in the task instruction, so a reader can
set aside edits to fields the task never specified.

`python -m openadapt_flow.verifier_audit families` prints the set as JSON
(`openadapt.verifier-audit.families/v1`). Add your own families with
`--families-module your.module`; the module exports a `FAMILIES` tuple of
`FaultFamily`.

## Writing an adapter

A target implements `AuditTarget`:

```python
class MyEnv:
    name = "my-env"
    post_claim_steps = True  # False if the episode ends at the final answer

    def tasks(self) -> list[str]: ...
    def reference(self, task) -> list[Call]: ...  # each Call has kind READ, WRITE, CLAIM or AUTH
    def run(self, task, calls, *, score: bool) -> Episode: ...
    def instruction(self, task) -> str: ...  # optional
```

`run` must reset, replay `calls` in order, record which calls the environment
refused, read the end state (and, if it can, the state at the claim step), and
call the scorer only when `score` is true. `targets/toy.py` is the smallest
example. `targets/agentdojo.py` runs in process. `targets/appworld.py` drives a
worker in AppWorld's own interpreter, because AppWorld pins pydantic 1.

## Running

```bash
python -m openadapt_flow.verifier_audit run \
  --target openadapt_flow.verifier_audit.targets.agentdojo:make_target \
  --target-arg suite=banking --out audit/agentdojo-banking
```

The output directory holds `cells.jsonl` (an append-only ledger; a rerun
resumes and does not rescore finished cells, but retries harness and scorer
errors), `ledger.json` (the target, kit version, families and redaction mode;
a rerun with a different target or mode refuses), `report.json` and
`REPORT.md`.
Rates are per family, with Wilson 95% intervals, and are not pooled. The report
also gives the share of tasks with at least one false accept.

Use `--redact` when a target's task data may not be redistributed in the clear.
AppWorld's protected data, for example, may be shared only encrypted. Redaction
replaces call arguments, effect rows, edit descriptions and error text with
digests and drops scorer details. Task ids and family names stay readable.

## What a result does and does not show

- A false accept shows that the scorer returned PASS on an end state that
  differs from the reference effect in the named way. The reference is one
  correct effect, not the only one. Read the counterexample's effect diff
  before you call it a bug.
- The audit replays scripted trajectories and makes no model calls. It shows
  what a policy could get credit for, not how often an agent does it.
- Benchmarks own their scorers. Report findings to their maintainers before
  you publish them.
