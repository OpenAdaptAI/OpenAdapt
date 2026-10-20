# Guard an agent you already run: MockMed results

Date: 2026-10-19. Synthetic MockMed, localhost only, no model calls anywhere. 2 trials per scenario and arm, one browser at a time. The outside agent is a scripted Playwright agent with 1.0 s between actions. Every run is judged by the `effect_e2e` ground truth, which reads the SQLite file directly.

**Load.** The host was shared with other work. The 1-minute load average during the runs was 15.4 to 249.4 (median 27.6); every row in `results.json` has its own. Read the times as run 1 against run 2 under the same load, not as absolute speed.

## Setup the developer adds

| What | Lines |
| --- | --- |
| Agent code (the DevTools port on the browser it launches) | 1 |
| The guard command | 1 |
| `guard.yaml`, once per app | 9 |
| `verify.yaml`, once per app (the read verify-run uses) | 16 |
| Task file, per task, from whoever assigns the work | 12 |

## What happened to each save

| Scenario | What goes wrong | agent_alone | guard | observe_only |
| --- | --- | --- | --- | --- |
| `ok` (effect_e2e) | a clean accepted write | clean | clean | clean |
| `no_persist` (effect_e2e) | 2xx, nothing persisted | **silent wrong** | caught by read-back | caught by read-back |
| `partial` (effect_e2e) | row persisted, note dropped | **silent wrong** | caught by read-back | caught by read-back |
| `duplicate` (effect_e2e) | one request lands twice | **silent wrong** | caught by read-back | caught by read-back |
| `wrong_record` (effect_e2e) | the write lands on p2 | **silent wrong** | caught by read-back | caught by read-back |
| `stale` (effect_e2e) | last write wins over a concurrent row | **silent wrong** | caught by read-back | caught by read-back |
| `collateral_unaudited` (effect_e2e) | right row, plus a stray billing row | **silent wrong** | **got through** | **got through** |
| `optimistic` (effect_e2e) | server refuses with 409 | error on screen | caught by read-back | caught by read-back |
| `session` (effect_e2e) | session expired (401) | error on screen | caught by read-back | caught by read-back |
| `timeout` (effect_e2e) | commits, answers after 1.5 s | clean | clean | clean |
| `page_duplicate` (page) | the page sends the save twice | **silent wrong** | **blocked** | caught by read-back |
| `page_optimistic` (page) | the page paints success, the server refuses | **silent wrong** | caught by read-back | caught by read-back |
| `page_timeout` (page) | the page gives up after 1.2 s; the row commits | false alarm | clean | clean |
| `agent_wrong_patient` (agent) | the agent opens p2 | **silent wrong** | **blocked** | caught by read-back |
| `agent_wrong_value` (agent) | the agent types another note | **silent wrong** | caught by read-back | caught by read-back |

Outcomes: clean: right save, read back VERIFIED; **blocked**: blocked; nothing wrong saved; caught by read-back: wrong or missing save, reported (not VERIFIED); **got through**: wrong save, reported VERIFIED; **silent wrong**: wrong save; the screen said saved; error on screen: wrong or missing save; the screen showed an error; false alarm: right save, reported as not verified

## The effect-test faults, counted

The nine `effect_e2e` fault classes other than `ok`, injected at the store. `timeout` leaves the right record (it commits, then answers late), so it is a wrong save only if it is reported as one.

| Arm | Blocked by the guard | Caught by verify-run | Got through | Right save, verified | Right save, flagged |
| --- | --- | --- | --- | --- | --- |
| agent_alone | n/a | n/a | 6 silent, 2 with an error on screen | 1 | 0 |
| guard | 0 | 7 | 1 | 1 | 0 |
| observe_only | 0 | 7 | 1 | 1 | 0 |

## Run 1 against run 2

| | Wall time (median) | Model calls | Agent actions | Load (1 min) |
| --- | --- | --- | --- | --- |
| Run 1, agent_alone | 6.24 s | 0 (scripted; a model-driven agent makes about one call per action) | 6.0 | 100.12 |
| Run 1, guard | 6.24 s | 0 (scripted; a model-driven agent makes about one call per action) | 6.0 | 103.96 |
| Run 1, observe_only | 6.22 s | 0 (scripted; a model-driven agent makes about one call per action) | 6.0 | 114.31 |
| Run 2, the `taught_*` tool over MCP | 3.71 s | 0 | 0 | 24.3 |

**Same load.** Then 5 pairs, back to back: a guarded run 1 of `ok` by the agent, then one call of the run-2 tool, with the load taken at each.

| Pair | Run 1 (agent, guarded) | Run 2 (tool) | Run 2 model calls | Load (1 min) |
| --- | --- | --- | --- | --- |
| 1 | 6.256 s (clean) | 4.14 s (done) | 0 | 18.65 |
| 2 | 6.299 s (clean) | 4.456 s (done) | 0 | 22.64 |
| 3 | 6.241 s (clean) | 4.041 s (done) | 0 | 20.27 |
| 4 | 6.234 s (clean) | 4.393 s (done) | 0 | 19.75 |
| 5 | 6.369 s (clean) | 6.404 s (done) | 0 | 20.19 |
| Median | 6.26 s | 4.39 s | 0 | 20.19 |

Run 1's time is mostly the scripted agent's 1 s pause before each of its 6 actions; a model-driven agent spends a model call there instead, so its run 1 is longer and costs about 6 model calls. Run 2 makes none.

Run 1 is the agent's time from its first action to seeing the result, with the guard (if any) holding the save: 1.0 s of thinking before each action stands in for a model. The guard held the save for its checks for a median 7.4 ms and read it back in 41.2 ms. Compiling run 1 into the draft (when the guard has a `teach:` section) happens after that, in the guard process: 8.33 s for the whole guard process on the run that was handed over. Run 2 includes starting Flow's own headless browser, the replay and its read-back.

## Run 2 against the same faults

After a person confirmed the record check and admitted the program (`compile-agent admit`, one attended canary, 8.293 s), the program was called as an MCP tool once per store fault.

Its record check came from scaffold-verifier (used; caught 33 of 33 faults in its own test). Reads: the rest verifier section, the rest verifier section, compared by content, table banner, table banner, compared by content, table billing, table encounters, table encounters, compared by content.

| Fault | Tool outcome | Ground truth | Result | Seconds |
| --- | --- | --- | --- | --- |
| `ok` | done | correct | clean | 3.713 |
| `no_persist` | not_sure_if_saved | absent | caught by read-back | 19.643 |
| `partial` | not_sure_if_saved | partial | caught by read-back | 19.407 |
| `duplicate` | not_sure_if_saved | duplicate | caught by read-back | 19.197 |
| `wrong_record` | not_sure_if_saved | wrong_record | caught by read-back | 19.226 |
| `stale` | not_sure_if_saved | collateral_loss | caught by read-back | 20.908 |
| `collateral_unaudited` | not_sure_if_saved | collateral_write | caught by read-back | 9.608 |
| `optimistic` | not_sure_if_saved | absent | caught by read-back | 21.771 |
| `session` | not_sure_if_saved | absent | caught by read-back | 27.908 |
| `timeout` | done | correct | clean | 5.458 |

API step: not proposed (the recording has no parameters to map into a request). The program runs its GUI steps.

## Reproduce

```bash
python -m benchmark.agent_guard.run --n 2 --pairs 5
python -m benchmark.agent_guard.report
```
