# Skill cache: run 1 by an agent, repeats by the compiled skill

Measured on 2026-10-28 with `measure.py` in this folder, model
`claude-sonnet-5-5`.

The agent is a LangGraph graph (`ToolNode`) on `ChatAnthropic`, with the
skill cache's browser tools (`openadapt_flow.integrations.agents.skills`).
Run 1 goes through compile-agent:

1. The agent does the task.
2. The system of record must show exactly the record the task named.
3. The operator confirms the proposed checks. In this measurement the script
   confirms them as the operator.
4. One canary run, then admission by a registered admitter key.
5. The program is emitted as an MCP server.

Runs 2 to 10 call that server's one tool over stdio, from a new process each
time.

**Scope:** every later run repeats the same task on the same record (Jane
Sample's referral) with a new note. This measures repeats. It doesn't
measure serving a task for another record: the cache treats that as a
different task and sends it to the agent.

## MockMed (synthetic clinic app): 3 trials of 10 runs

The task is "Open the referral for Jane Sample, start a Triage encounter, and
save this note: {note}", with a new note on every run. Each trial compiles
from scratch in a new folder. MockMed's fault server runs in the measuring
process (`?fault=ok&idempotency=demo`), and it's reset before every task,
because its demo idempotency key makes a second note on the same patient a
duplicate.

| | Run 1 (agent, then compile) | Runs 2 to 10 (compiled skill over MCP) |
| --- | ---: | ---: |
| Runs | 3 | 27 |
| Write verified | 3 / 3 (record check) | 27 / 27 (receipt VERIFIED) |
| The store shows exactly that note on Jane Sample (read separately) | n/a, see below | 27 / 27 |
| Model calls per run | 6 | 0 |
| Model cost per run | $0.0224 | $0 |
| Wall time per run (median) | 23.5 s | 7.8 s (6.9 to 11.5 s) |
| Of which: the agent's own run | 8.3 s | |
| Of which: the program's run | | 6.9 s |

How to read it:

- The skill saves the model cost and the model calls. Its wall time, about
  8 s, is close to the agent's own 8.3 s on this short task. Each skill run
  starts a new MCP server process and a browser, then reads the store back.
- Run 1's 23.5 s includes the agent, the recording, the compile and the
  canary. It happens once per task.
- Run 1's store check reads "n/a": the canary resets MockMed after run 1, so
  run 1's note is gone by the time it could be read. Run 1's write is judged
  by compile-agent's record check, which read it before the canary.
- Cost uses $2 per million input tokens, $10 output, $2.50 cache write and
  $0.20 cache read (`PRICES_PER_MTOK`). Token counts were nearly the same in
  all three trials.
- The host is shared. Its 1-minute load average was 14 to 34 during these
  runs.
- These runs predate the skill path's second record check (this task's own
  record and values, which `SkillCache` now runs after every skill call).
  The separate store read in the table checks the same thing.

`results/mockmed/summary.json` holds the aggregate. The per-run rows are kept
as private evidence.

## Trac 1.6 (open-source issue tracker): not past run 1

Trac 1.6 (BSD license) was installed with pip and served by `tracd` over
SQLite. The contract reads Trac's own `ticket` table
(`apps/trac.verify.yaml`).

Run 1's write verified in Trac's database in 7 of 12 attempts, but no program
was admitted, so there is no repeat row. Every stop came before a wrong
write:

- A comment on an existing ticket: the save check reads the form's visible
  text, and Trac's ticket form shows the ticket's summary only inside an
  input, so the save was refused as unidentified.
- A new ticket: a create-new form shows no existing record, so the save
  compiles without an identity, and `clinical-write` refuses it. That policy
  also requires an idempotency key for a save, which Trac's form doesn't
  have.

The setup changed between the attempts (task, policy, tools), so they are not
trials of one configuration.

## OpenEMR: not measured

The local OpenEMR task (`benchmark/openemr_local`) registers a new patient.
That is the create-new form case above, and it also needs OpenEMR's sign-in,
which `SkillCache` doesn't pass to compile-agent.

## Reproduce

```bash
set -a; source .env; set +a   # ANTHROPIC_API_KEY
PYTHONPATH=. python benchmark/skill_cache/measure.py --app mockmed --runs 10 --trial 1 --work /tmp/skill-cache-1
python benchmark/skill_cache/summarize.py mockmed /tmp/skill-cache-1/runs.jsonl
```

`measure.py` needs `openadapt-flow[dev,agent,langgraph]` and
`langchain-anthropic`, plus Playwright's Chromium.
