# Skill cache: run 1 by an agent, later runs by the compiled skill

Measured on 2026-10-28 with `measure.py` in this folder, engine commit
`7c7f36b1a2` (in this branch's history; later commits change no engine
code), model `claude-sonnet-5-5`.

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
  all three trials: $0.02243, $0.02241, $0.02232.
- The host is shared. Its 1-minute load average was 14 to 34 during these
  runs (in every row).

Files: `results/mockmed/runs.jsonl` holds one row per run, and
`results/mockmed/summary.json` is computed from it by `summarize.py`. No row
holds a typed value.

## Trac 1.6 (open-source issue tracker): not reached past run 1

Trac 1.6 (BSD license) was installed with pip into its own virtual
environment and served by `tracd` on 127.0.0.1:9580 over SQLite. The contract
reads Trac's own `ticket` table (`apps/trac.verify.yaml`).

Run 1's write verified in Trac's database 7 times in 12 attempts. No attempt
reached run 2: every program was refused or halted before it was admitted.
The cause is in the engine, not the agent:

| Task and setup | What stopped it |
| --- | --- |
| Comment on an existing ticket | The save gate reads the form's visible text, and Trac's ticket form shows the summary only inside an input. The save was refused as unidentified. |
| File a new ticket, on `main` | The save compiled without an identity, and `clinical-write` refused it. This is the create-new form case that #1581 addresses. |
| File a new ticket, with #1581 merged locally | The identity was armed. `clinical-write` still requires an idempotency key, which Trac's form doesn't have. With the `permissive` policy: (1) the recorder named `<input type=submit value="Create ticket">` a textbox called "submit" (fixed on this branch); (2) Trac's radios named `action` shadow `form.action`, so #1581's submit guard cancelled every real submit (fix proposed to #1581); (3) after that fix, the durable checkpoint refused #1581's form-identity evidence ("verified identity evidence is only a status label") and the run raised. |

Each of these stopped the run before a wrong write. None let an unverified
write through. The attempt rows are in `results/trac-attempts/rows.jsonl`.
The setup changed between attempts (task, policy, tools), so they are not
trials of one configuration. Their spend was $0.35.

## OpenEMR: not measured

The OpenEMR fixture is shared through a lease queue, and this measurement was
seventh in it. The local OpenEMR task (`benchmark/openemr_local`) registers a
new patient, which is the same create-new form case that blocks Trac. It also
needs OpenEMR's sign-in, which the skill cache doesn't yet pass to
compile-agent.

## Reproduce

```bash
set -a; source .env; set +a   # ANTHROPIC_API_KEY
PYTHONPATH=. python benchmark/skill_cache/measure.py --app mockmed --runs 10 --trial 1 --work /tmp/skill-cache-1
python benchmark/skill_cache/summarize.py mockmed
```

`measure.py` needs `openadapt-flow[dev,agent,langgraph]` and
`langchain-anthropic`, plus Playwright's Chromium.
