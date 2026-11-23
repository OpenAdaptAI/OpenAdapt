# Opt-in fallback on drift (MockMed)

What happens when a wrapped task's compiled program meets a changed app and
the task has opted in to `FallbackPolicy` ([WRAP_FALLBACK.md](../../docs/WRAP_FALLBACK.md)).

```bash
cd packages/flow
python benchmark/wrap_fallback/measure.py --out benchmark/wrap_fallback/results.json
```

For each MockMed drift mode, a scripted agent's run 1 is compiled and
admitted on the unchanged app (a registered key admits it after two
canaries). Then the app drifts, and the task runs once through the policy.
The fallback agent is scripted: it knows the drifted labels, and it makes no
model calls. A scripted gate stands in for the person and approves every
save that reaches it; a second trial per mode has it refuse. After each
trial the script reads MockMed's own store: `correct` is exactly one record
for the task's patient with the typed note, `none` is nothing saved, and
`wrong` is anything else.

| Mode | What changed | Program | Gate approves | Gate refuses |
| --- | --- | --- | --- | --- |
| redesign | The save is renamed, given a new id and moved; a decoy "Submit feedback" is added | HALTED | agent_fallback: VERIFIED, saved correct, 1 approval | agent_fallback: GATE_REFUSED, saved none, 1 approval |
| relink | The control that opens the form is renamed and moved | HALTED | agent_fallback: VERIFIED, saved correct, 1 approval | agent_fallback: GATE_REFUSED, saved none, 1 approval |
| rename | Labels renamed (`Submit Encounter`, `View`) | VERIFIED | not reached (program verified) | not reached (program verified) |

Summary: 3 drift modes, 6 trials. The program halted before the save on 2 modes and verified on 1 (the compiled program found the renamed labels). Fallbacks: 4, of which 2 verified (every approved one). Wrong saves: 0. Saves after a refusal: 0. Approvals asked for: 4. Model calls: 0.

## Limits

- Synthetic app, scripted agent, scripted gate, three drift modes. It shows
  the policy's routing and safety on real browser runs; it does not measure
  how often a model-driven agent finishes a drifted task.
- The compiled program runs in process on a thread, not over MCP, until the
  MCP worker-thread fix in #2406 lands.
- `repair_opened` is false when the halted step is not a one-step target
  replacement, so `SkillCache.open_repair` offers no candidate.

Results: [results.json](results.json).
