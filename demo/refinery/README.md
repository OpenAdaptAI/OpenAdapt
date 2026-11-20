# Refinery: recorded workflow to audited task environment

One recorded MockMed workflow (add a Triage note) becomes a small RL-style
task environment: an instruction, a starting state, a reference trajectory and
a success check against the system of record. We then audit that environment's
success check with our own verifier audit kit
(`openadapt_flow.verifier_audit`, #1783) before anyone trains or scores
against it.

**Status: research demo.** All data is synthetic (MockMed, OpenAdapt's own test
app). No real, customer or patient data is used. One recording, three generated
tasks, one machine.

```bash
python demo/refinery/pipeline.py
```

That runs in about a second from the committed capture, with an in-process
MockMed and no browser. It needs `openadapt-flow` 2.7.0 or later
(`pip install -e packages/flow`). To replay the recording in headless Chromium
first, add `--capture` (needs `python -m playwright install chromium-headless-shell`).

## Results

From `results/refinery.json` (the per-grader kit reports are in
`results/audit/`). Each cell is one task and one fault family; 3 tasks.

| Generated success check | False accepts | False rejects | Caught |
|---|---|---|---|
| `mined_postcondition`: a row with the recorded values exists | 9: extra write to another patient (3), duplicate write (3), write after the success claim (3) | 0 | 9 |
| `trajectory_replay`: the agent sent exactly the recorded calls | 3: write after the success claim (3) | 3: one permitted extra read (3) | 15 |
| `effect_at_claim`: the store diff equals the expected one, at the claim and at the end | 0 | 0 | 18 |

The pipeline ships `effect_at_claim` and would ship nothing if every check had
a case.

- **Seeded loophole.** `mined_postcondition` is the record check you get by
  mining the recording's store diff: it looks for the saved row and never asks
  what else changed. The audit's `unauthorized_side_effect` family keeps the
  task's write and adds a copy aimed at another patient (`patient_id='p2'`
  for the `p1` task). MockMed stores both rows and the check returns PASS.
  The case, with its effect diff, is in `refinery.json` under
  `audit.mined_postcondition.false_accepts`.
- **False reject.** `trajectory_replay` fails an agent that reads the record
  list once more before claiming success, a call the environment permits and
  that changes nothing.
- **Write after the claim.** Both weaker checks look only at the end state
  or the call list, so a write sent after `complete` passes. The kit judges
  that family on the store at the claim.
- `partial_write` is refused on every task: each task has one write.

## What the pipeline does

| Step | Where | What it does |
|---|---|---|
| Capture | `capture.py` | Replays `packages/flow/tests/fixtures/jit/playwright_mcp_add_note.json` (6 recorded Playwright MCP actions) in headless Chromium against MockMed's fault server with `?fault=ok`, starting at the task list. Keeps each API request the page sent and the store before and after. Writes `results/capture.json`. |
| 1. Lift | `pipeline.py lift()` | Each API request becomes a call; `POST`, `PUT`, `PATCH` and `DELETE` are writes. A field whose value is a recording input (`note`) or that names a record (`patient_id`) is a parameter; `type` stays the recorded constant. Fields in the store diff that no request sent (`id`, `key`, `source`) are server-assigned. The lift stops unless the store diff is exactly the write bodies. |
| 2. Tasks | `generate_tasks()` | Fills the three synthetic rows in `VARIANTS` into the lifted trajectory. Each task has an instruction, a starting state (the store after a reset that seeds another clinician's encounter on `p1`, read back), the reference trajectory with a `complete` claim appended, and the expected effect. |
| 3. Checks | `GRADERS` | Three success checks, each derived from the capture alone. |
| 4. Audit | `audit()` | Runs the kit's 8 default families against `RefineryEnv` once per check. Each run starts from a fresh reset over HTTP. Ground truth is the store diff read from MockMed's `GET /api/db`, never a check's verdict. |

The environment's tools are the lifted `POST /api/encounter`, a read
(`GET /api/db`) and `complete`. The recording made no API read, so the read is
declared to the kit as the permitted incidental call.

## Limits

- The fault families are the kit's own, and `effect_at_claim` was written by
  the same people. A clean audit says the kept check survives these 8
  families on 3 tasks. It is not an independent audit.
- One recording and one write per task. The lift has not been tried on a
  multi-step or multi-write workflow, or on a real application.
- The episode keeps running after `complete`, so a late write can land. An
  environment that ends the episode at the claim would make that family moot.
- MockMed accepts writes to any patient id, so wrong-record edits always land.

## Tests

`tests/test_refinery_demo.py` (root CI, about 2 seconds) rebuilds the results
from the committed capture and checks they match the committed files. It also
checks that the seeded loophole and the false reject are reported, that only
`effect_at_claim` ships, and that removing the claim-time clause from
`effect_at_claim` brings back the late-write false accept.
