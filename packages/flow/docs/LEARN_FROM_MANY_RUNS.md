# Learning one program from many runs

OpenAdapt doesn't have to compile a single linear demonstration. Three paths
turn several runs of the same task into one program with branches. Each path
refuses when the runs don't settle a question, and none of them puts a new
branch into service without a person, or an agent under a policy a person
signed, admitting it.

| Path | Input | Output | Status |
|---|---|---|---|
| `induce` | Two or more recordings of the task | One program with parameters, loops and branches, or a refusal with a list of what to record next | Released (openadapt-flow 2.2.0 and earlier). Tested in CI on controlled demonstrations. |
| `teach-branch` | Two or more halted runs fixed the same way, plus optional normal runs | A guarded branch, written as a repair candidate | Released (openadapt-flow 2.2.0). Tested in CI on controlled, synthetic runs. |
| Shadow mining + `admit` | Episodes that Capture's shadow mode noticed in a person's real work | A candidate draft, then a program once the task is recorded and admitted | Capture side released (openadapt-capture 1.3.0, off by default). Flow side merged, not released. Tested only on synthetic episodes. |

None of these has field measurements yet. We have not measured how many real
items take a branch that no recording showed, or how many of those branches
these paths recover.

## What happens at a branch the program never saw

The runtime stops. A step whose screen, record or result doesn't match what
was qualified halts before any save, and the item goes to a person. The
program never guesses a path. The three paths below are how those halts, and
more recordings, become branches.

## `induce`: several recordings, one program

```bash
openadapt-flow induce rec-1 rec-2 rec-3 --out program
```

`induce_program` aligns the recordings and works out what is the same in all
of them (fixed steps), what changes (parameters), what repeats (loops), and
where they split (branches, each with the screen condition that tells them
apart). It is deterministic and makes no model calls.

When the recordings don't settle a question, it refuses instead of guessing:
`certified` is false and it prints a `record-next` worklist, such as "record
each of these two paths under a condition you can see" or "record the path
where this dialog doesn't appear". Record those and run it again.

Or, when a test copy of the app is available, let it answer:
`induce --probe-on test-copy.yaml` replays each recording with the disputed
step dropped and with it kept, scores each run by reading the record back,
and certifies the step only when exactly one reading works for every
recording. See [PROBE_INDUCTION.md](PROBE_INDUCTION.md).

`admit rec-1 rec-2` runs the same induction as its first step, then proposes
the checks and stops once for a decision (see
[SHADOW_CANDIDATES.md](SHADOW_CANDIDATES.md#how-a-candidate-is-admitted)).

## `teach-branch`: several halts fixed the same way

```bash
openadapt-flow teach-branch --bundle program \
  --example runs/halt-1 fix-1.json --example runs/halt-2 fix-2.json \
  --normal-run runs/ok-1 --out candidate
```

A single fixed halt (`teach`) can only key on what that one screen showed,
which may be a name or a reference number. `teach-branch` needs at least two
halts at the same step, fixed with the same corrective steps. It keys the
branch on screen text that every example showed and no earlier screen did.
If a normal run also shows that text, it narrows the guard with a run
parameter that separates the two cases, or refuses when none does.

The result goes through the same learning loop as any repair (induction,
regression gate, held-out check) and is written as a repair candidate. It is
not active. Activation follows the repair lifecycle: qualification runs,
approval and a canary (see [REPAIR_LIFECYCLE.md](REPAIR_LIFECYCLE.md)).

## Shadow mining: many real runs, watched in the background

Capture's shadow mode is off until the person acknowledges a notice. While on,
it keeps value-free symbols (application, control role, scrubbed URL pattern,
short chrome labels, and references in place of values) in a 24-hour encrypted
buffer on the computer, and opens no network connection. When it sees the same
sequence repeat, it offers a draft. If the person clicks **Review draft**, it
exports the episodes as `openadapt.shadow.episodes/v2`.

Flow's `induce_candidate` turns each episode into a trace and runs the same
`induce_program` over them, so differences between episodes become
parameters, optional steps and branches, or a refusal. The output is a
candidate: `admitted` and `runnable` are always false, anchors are
placeholders (shadow mining keeps no pixels), and suggested checks are listed
as suggestions, never applied. Today a script calls `induce_candidate` and
`write_candidate`; there is no CLI command for this step yet.

A candidate becomes a program only through `admit`:

1. Record the task once with Capture, so the program has real anchors.
2. `openadapt-flow admit --candidate ID --out DIR --name NAME` compiles the
   recordings, induces one program, proposes checks, and stops once.
3. A person accepts at a terminal or in the MCP client's form. An agent can
   accept only with `--delegate-approvals` and an approver grant that a
   registered person signed, which names the agent, covers the workflow, and
   lets an agent approve saves. An agent can't accept an admission it
   prepared, or one an unidentified MCP client prepared, and can't accept
   under the `regulated` policy pack.
4. Accepting applies the checks through the same oracle gate as
   `qualify accept`. It runs nothing. `qualify campaign` must still pass
   before the program is certified.

Accepting sets the `problems_only` approval mode for that exact bundle: runs
stop only for problems instead of before every save. That mode keeps its own
readiness gate. Until the workflow has earned the switch and it is on, every
save still waits for a person. A changed bundle, an expired grant, or a
decision record not signed by the machine's admission key drops back to a
person approving each save. Per-save approval stays available as the
strictest setting.

### Privacy

- Shadow episodes carry no screenshots, coordinates, typed values or keyed
  digests; Flow refuses a v1 export (which had digests), any unknown field, a
  label containing digits or `@`, and any hex-like token.
- Everything above runs on the computer. Mined episodes and candidates stay in
  local folders.
- The MCP admission tools return codes, counts and step ids only. Induction's
  record-next lines can quote labels from real recordings, so they appear
  only at the terminal.
- Recordings made for admission are ordinary Capture recordings, and `admit`
  sends none of them anywhere. Scrubbing of what Flow writes from them
  follows [PRIVACY.md](PRIVACY.md): pin `OPENADAPT_FLOW_SCRUB=on` with the
  `privacy` extra so a missing openadapt-privacy fails the run. Under the
  default `auto`, without the extra, Flow writes plaintext and warns.

## Code

- `openadapt_flow/compiler/induction.py`: `induce_program`
- `openadapt_flow/learning/branch_teach.py`: `teach_branch`
- `openadapt_flow/compiler/motif_induction.py`: `induce_candidate`,
  `write_candidate`
- `openadapt_flow/admit_pipeline.py` and `openadapt_flow/cli_admit.py`:
  `admit`
- `openadapt_flow/agent/admission.py`: the MCP admission tools
