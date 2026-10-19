# Compile an agent's first verified run

`compile-agent` lets a computer-use agent do a task once, checks the record
it wrote, and compiles that run into a program. Every later run executes the
program: no model calls, every save read back from the system of record.

The agent runs against the app at `--url` and makes real entries there. Point
it at a test copy of the app with test records.

```bash
openadapt-flow compile-agent \
  "Open the referral for Jane Sample, start a Triage encounter, and save this note: {note}" \
  --url http://localhost:8080/#tasks --verify verify.yaml \
  --param note="Follow up in two weeks" \
  --expect patient_id=p1 --on-screen "Jane Sample" --on-screen "MRN P1" \
  --canary-param note="Canary test note" \
  --admit-key agent.pem --admission-policy signed-policy.json

openadapt-flow compile-agent run .openadapt-compile-agent/<task key> \
  --param note="Recheck knee in ten days"
```

`verify.yaml` needs only a `verifier:` section that reads the system of record
(`rest`, `sql`, `fhir`, `graphql` or `file`), the same section
`record --verify` and `verify-run` read. The agent is Claude
(`claude-opus-5-5`) on your own key from `ANTHROPIC_API_KEY`; OpenAdapt holds
no key. `--agent steps.json` runs a scripted agent with no model calls.

## What happens

1. **Run 1, in the harness.** The agent does the task while
   `openadapt_flow.jit` records every action, exactly as for any other JIT
   capture ([JIT.md](JIT.md)).
2. **The record check.** The system of record is read when run 1 starts and
   after it ends. Run 1 passes only when exactly one record appeared, no other
   record changed or disappeared, the new record has every `--expect` field
   with that value, and every `--param` value landed in one of its fields.
   Otherwise the draft is set aside and nothing can be admitted.
3. **Proposed checks, confirmed by a person.** The change becomes proposed
   record checks (the `record --verify` proposer). A person reads them at a
   terminal, can leave out fields that change on every save
   (`--drop-field`), and confirms with their name. They become the program's
   `verify.yaml`. The same person confirms the parameters, which recompiles the
   draft with them. A field named in `--expect` can't be dropped, and every
   parameter must still be read back.
4. **Admission.** Canaries, then the JIT admission
   ([JIT_ADMISSION.md](JIT_ADMISSION.md)): a registered admitter key, or a
   registered agent under an admission policy a person signed. The recording
   is signed with the driving agent's `principal_id`. When that is the same
   registered agent that admits, it is self-admission, which the policy must
   allow (`allow_self_admission`); either way the agent needs
   `min_clean_canaries` clean canaries and a read-back on every write step.
   Without a key, `--admit-as NAME` at a terminal shows that person every step
   and the parameters, asks them to confirm the review, runs one canary, shows
   its result, and asks them to admit. A canary is a real save on the record
   `--expect` names, so its values come from the operator
   (`--canary-param NAME=VALUE`); there is no default. Each canary also gets a
   `verify-run` receipt against the confirmed checks; both must end VERIFIED.
5. **Emit.** An Agent Skill (`compiled/skill/<name>/SKILL.md`), an MCP
   server with one tool (`compiled/mcp/server.py`), and the one-line
   `openadapt-flow compile-agent run` command. All three call
   `run_compiled`, so every call goes through the admission check, the
   read-back and the receipt. Neither copies the bundle out of the task
   directory.

Every later run executes the admitted program with no model calls, and gets a
`verify-run` receipt (`receipts/run-*/effect-receipt.json`). A run that does
not end VERIFIED goes to the exception inbox, and the agent is not called.

The command is re-entrant. Run it again with the same arguments and it
continues where it stopped: the agent runs only when the task has neither a
draft nor an admitted program. Without an interactive terminal it stops after
run 1 with the proposed checks waiting for a person (exit code 3).

## The harness: structure first, screenshots when needed

The agent does not get a screenshot every turn. It gets the page's structure
as short lines of text: headings, text, table rows, and every visible control
with a ref:

```
page: MockMed (http://127.0.0.1:8080/#encounter)
Jane Sample — MRN P1 — DOB 1980-01-01
# New Encounter
Encounter Type
[e1] button "Triage" (selected)
[e2] button "Consult"
Note
[e3] textarea "Note" value=""
[e4] button "Save Encounter"
```

It acts with `click(ref)`, `type_text(text, ref)` and `press_key(key)`, and
gets a new listing after each action. A ref still has to name a control with
the same label in the same table row when it is used; otherwise the action is
refused and the agent observes again. It can call `screenshot` when the
listing is not enough. After an action that committed a write, it gets a
screenshot of the result. An action on a ref becomes an ordinary click at the
control's center, so the recording and the compiled program are the same as
for any other capture. The page itself is never modified.

The listing is scrubbed with `openadapt_flow.privacy` (Presidio, when
`openadapt-privacy` is installed and `OPENADAPT_FLOW_SCRUB` is not `off`),
except for the values the operator put in the task: the parameters, the
`--expect` values and the `--on-screen` identity. The agent needs those to
find the right row.

`--vision every_turn` is the baseline for comparison: Claude's computer
toolset (`computer_toolset_20260801`) with a screenshot after every action.

`--vision code` gives the agent one terminal-style tool instead: it writes
short scripts that read the structure, act by role and name, read back, and
keep notes in a sandboxed working directory, with a screenshot only when it
asks. Every action becomes a structured step that is checked against the
compiled program. See [CODE_AS_ACTION.md](CODE_AS_ACTION.md).

### The save gate

Every action that can commit a write is gated unless it plainly does not:

- a click on a control whose label names a write (Save, Submit, Sign, Approve,
  Confirm, Send, Post, Delete, Remove, Update, Apply, Done, Finish, Finalize,
  Complete), whatever its tag or role;
- a click on an element the harness can't read;
- Enter, a typed line break, Space on anything but a text field, and any
  keyboard shortcut.

A click on a control with another label (Open, New Encounter), typing without
a line break, and navigation keys pass without a check. For a gated action,
the harness:

1. checks that every `--on-screen` string appears, as whole words ("MRN P1"
   is not "MRN P12"), in the row, dialog, form or section that holds the
   control (or the focused field). A name in a sidebar or a worklist does not
   count. If one is missing, the action is not performed and run 1 ends
   `refused_wrong_record`: nothing is saved and nothing is compiled, even if
   the agent catches the refusal and carries on;
2. keeps a screenshot as evidence (`attempts/<stamp>/gates/`);
3. asks the hold when there is one. At a terminal the CLI asks before every
   save (`--no-hold` turns it off). A hold that says no ends the run
   `refused_by_hold`.

This is the right-record check and the hold from the guarded save design
([#1258](https://github.com/OpenAdaptAI/OpenAdapt/pull/1258)). It protects run
1 only. The compiled program carries its own identity checks and reads every
save back.

## Measured on MockMed

Ten live trials per arm with `claude-opus-5-5`, alternating order, a distinct
note per trial ([benchmark/compile_agent](../benchmark/compile_agent/README.md),
raw rows committed):

| Per run 1 (median) | Structure first | Screenshot every turn |
| --- | ---: | ---: |
| Passed the record check | 10 / 10 | 10 / 10 |
| Screenshots sent to the model | 1 | 15 |
| Prompt tokens | 10,332 | 76,053 (7.4x) |
| Cost | $0.028 | $0.148 (5.3x) |
| Model time | 8.9 s | 11.6 s |

Both arms made 5 model calls. Wall time depended mostly on the load of the
shared host; in the first three trials, at low load, run 1 took 15 to 18 s
structure first and 36 to 59 s with a screenshot every turn. Ten later runs of
the admitted programs ended VERIFIED with 0 model calls and 0 tokens.

## Python

```python
from openadapt_flow.compile_agent import ClaudeAgent, compile_first_run, run_compiled
from openadapt_flow.jit import load_customer_model_key

result = compile_first_run(
    "Open the referral for Jane Sample, start a Triage encounter, and save this note: {note}",
    params={"note": "Follow up in two weeks"},
    url="http://localhost:8080/#tasks",
    verify="verify.yaml",
    expect={"patient_id": "p1", "type": "Triage"},
    on_screen=["Jane Sample", "MRN P1"],
    agent=ClaudeAgent(vision="selective"),
    model_key=load_customer_model_key("anthropic"),
    confirm_checks="dana@clinic.example",     # or a callback that shows them to a person
    drop_fields=["key"],
    canary_params=[{"note": "Canary test note"}],  # a real save; choose safe values
    admitter=agent_principal,                  # principal_from_key(...)
)
result.status          # "admitted"
result.run1["agent"]   # model calls, tokens, cost, seconds
result.emitted         # {"skill": ..., "mcp_server": ..., "cli": ...}

later = run_compiled(result.dir, note="Recheck knee in ten days")
later.verified, later.model_calls, later.receipt_outcome   # True, 0, "VERIFIED"
```

`FirstRunCompiler` exposes the same steps one at a time (`record`,
`confirm_checks`, `admit`, `emit`, `run`) for a flow where a person confirms
later. A draft waiting for a person is admitted at a terminal with:

```bash
openadapt-flow compile-agent admit <task dir> --admit-as "Dana Reviewer" \
    --canary-param note="Canary test note"
```

It shows the person the proposed checks (unless someone confirmed them
already), the steps, and the canary result, and asks them to confirm each.
`--admit-key` admits with a registered key instead, once the checks are
confirmed.

## An agent over MCP

`openadapt-agent serve --teach verify.yaml` puts this pipeline behind MCP
tools, so an agent connected to the server (Claude Code, Codex, any MCP
client) teaches a task without leaving its conversation: `teach_start`,
`teach_observe` and `teach_act` for run 1, `teach_finish` for the record check
and the draft, `teach_confirm_checks` for a person to confirm the checks in the
client's form, and `teach_admit` for the canaries and the admission. The agent
is the inner agent here: `compile_agent.driven.DrivenRun` runs
`FirstRunCompiler.record` on a thread of its own and hands each tool call's
action to the harness. Admission follows [JIT_ADMISSION.md](JIT_ADMISSION.md):
the server records as the registered agent whose key the operator gave it,
so admitting is self-admission and needs a policy with
`allow_self_admission`; without a key and policy, a person admits with the
command above. An admitted program joins the server's `list_workflows` and
gets a `taught_<name>` tool. See the
[agent README](../../agent/README.md#teach-a-new-workflow-from-your-agent).

The MCP tools also run on Windows, macOS, Linux, RDP and Citrix: pass
`FirstRunCompiler(surface=<deployment backend section>)`. Off the web the
harness is `SurfaceHarness`. It reads the authoring session's
accessibility tree (no values, no window titles). It can't read which form or
row a control is in, so it gates more than the web harness: every click except
one, named by its ref, on a text field, a check box, a tab or a control whose
label plainly navigates (Open, New, Cancel), and every click at x,y, Enter,
typing outside a text field, single-letter keys and shortcuts. A person
approves each gated action (the hold); without a hold, it is refused. Over MCP
the approval is bound to the run, the exact action and the number of actions
performed so far, so it covers only what the person was shown. On
RDP and Citrix the canary is refused until pixel identity is qualified
([BACKEND_PARITY.md](BACKEND_PARITY.md)).

Any agent works if it has a `dialect` (an `openadapt_flow.jit` action format)
and `drive(request, harness)`. `ScriptedAgent` follows a list of steps by
control name and makes no model calls. `ClaudeAgent(client=...)` takes any
object with `messages.create`; the tests pass a fake.

## Statuses

| Status | Meaning |
| --- | --- |
| `admitted` | Later runs execute the program with no model calls. |
| `draft` | Run 1 passed its record check; the checks or the admission wait for a person. |
| `needs_person` | The draft is outside the agent's admission policy; a person decides. |
| `refused_wrong_record` | The save gate did not see the record's identity; nothing was saved. |
| `refused_by_hold` | The hold said no; nothing was saved. |
| `record_check_failed` | The system of record did not show exactly the record the task named. |
| `canary_failed` | A canary did not end VERIFIED, or its receipt did not. |
| `capture_failed`, `unsupported` | The agent failed or used an action the recording can't hold. |

## Files

```
<cache>/<task key>/
  compile_agent.json        the task, app, verifier file, expect, on_screen
  program/                  the JIT draft, then the admitted bundle (JIT.md)
  attempts/<stamp>/         run 1: run1.json (tokens, time, screenshots, gate log), gates/
  compiled/
    verify.proposed.yaml    the proposed checks (mode 0600)
    verify.yaml             the checks a person confirmed
    record_checks.json      who confirmed them, digests, dropped fields
    admission.json          canaries and who admitted
    skill/<name>/SKILL.md   the Agent Skill
    mcp/server.py           the MCP server
    emitted.json            the CLI line and the paths above
  receipts/<run>/           verify-run baseline and effect receipt per canary and run
  runs/                     one directory per canary and run
```

## Limits

- The `compile-agent` command runs web apps only so far: its Claude agent
  reads the page's DOM listing. A Windows, macOS, Linux, RDP or Citrix target
  (`--backend`, or a `--config` whose `backend.kind` names one) and any URL
  that is not http(s) are refused before anything runs; there is no fallback
  to a browser. The MCP teach tools run the same pipeline on those surfaces
  (above).
- The save gate recognizes a click that commits by the control's label. An
  icon with no label, or a label that names no write ("OK"), is not gated in
  run 1; the record check after run 1 still refuses a wrong record, but only
  after the write landed.
- The identity must be shown in the same row, dialog, form or section as the
  control. An app that shows the patient only in a page header outside them
  is refused at every save.
- The run-1 record check compares two reads of the system of record. A change
  someone else makes during run 1 makes it fail closed.
- A verifier read has no run parameters while recording, so it must name its
  records with literals (the `record --verify` rule).
- On a replay that writes to the wrong record, the program halts at the save
  step and goes to the inbox, but the `verify-run` receipt reports
  `HALTED_BEFORE_EFFECT` because the confirmed check found no matching record.
  The guarded save ([#1258](https://github.com/OpenAdaptAI/OpenAdapt/pull/1258))
  reports such a write as `RECONCILIATION_REQUIRED`.
- MockMed's demo idempotency key makes a second note on the same patient a
  duplicate, so the tests and the measurement reset MockMed before each run.
  A real app is never reset.
- A canary that fails its verify-run receipt is still recorded as passed in
  the JIT's canary log. `compile-agent` stops on it, but an admission made
  directly with `openadapt-flow jit admit` does not see the receipt.
- Screenshot-every-turn mode keeps every screenshot in the conversation, so a
  long run can reach the API's request size limit.
