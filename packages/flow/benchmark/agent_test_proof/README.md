# `openadapt test` proof runs (2026-11-21)

These runs check three things about `openadapt test` and its GitHub Action:

- that every shipped scenario plants its fault on a real app;
- that the gate catches a real regression;
- that the grader agrees with a person's reading of the database.

All data is synthetic. The receipts are unsigned (digests only).

**Where it ran.** The apps ran in Docker on the lab laptop (WSL2): Dolibarr 20.0.2, OpenEMR 8.0.0.3 and EspoCRM 8.4, each with MariaDB, all pulled by image reference. The harness, the headless browser and every model call ran on the Mac. The Action ran under `act` (`-P ubuntu-latest=-self-hosted`, [`lab-gate.yml`](lab-gate.yml)), with `start-app: "false"`; it reached the lab's stack over SSH (`--docker`) and the app's port. A second, hosted run of the Action on GitHub's `ubuntu-latest` started the app on the runner itself (`.github/workflows/agent-test-action.yml`).

The raw runs are in `runs/<name>/`: `results.json`, `report.html`, `receipt.json`, `gate.json`, and per-scenario evidence (the transcript and the table before and after, raw). The screenshots were left out to keep the repository small. Machine paths are replaced with `lab/` and `packages/flow/`. Model prices are not published here; each run's `results.json` keeps its model call count.

## Every scenario, with the model-free agents

`scripted` fills the form and believes the page. `scripted-readback` then reads the app back (Dolibarr: the contact list, then the card; OpenEMR: the patient's edit form; EspoCRM: the account list).

| App (scenarios) | Agent | Completed correctly | Silent corruption | Collateral edits | False success | Safe stops |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Dolibarr (10) | scripted | 3 | 4 | 2 | 7 | 0 |
| Dolibarr (10) | scripted-readback | 3 | 1 | 2 | 3 | 4 |
| OpenEMR (8) | scripted | 1 | 4 | 2 | 7 | 0 |
| OpenEMR (8) | scripted-readback | 1 | 2 | 2 | 4 | 3 |
| EspoCRM (6) | scripted | 1 | 3 | 1 | 5 | 0 |
| EspoCRM (6) | scripted-readback | 1 | 3 | 1 | 5 | 0 |

The app accepted every planted fault: all 20 faulted scenarios (plus the 4 clean ones) in each pass, none left out.

The read-back reference catches a blank, a wrong value and a record that is not saved, on Dolibarr and OpenEMR, and a duplicate on Dolibarr. It misses:

- collateral edits;
- a late revert, which lands after it looked;
- a duplicate on OpenEMR, because the edit form shows one chart.

On EspoCRM it checks only that the name appears, so it catches nothing beyond the clean case.

## The gate catches a real regression (Dolibarr, through the Action)

**Baseline.** Claude computer use (`claude-cu`, `claude-sonnet-5-5`) with one added instruction, [`verify-prompt.md`](verify-prompt.md): "open the list ... confirm that exactly one record has every value ... check that none of [the others] changed". It ran 2 repetitions of the 10 scenarios.

**Candidates.** Both ran through the same Action, against [`baselines/dolibarr-claude-verify.json`](baselines/dolibarr-claude-verify.json), with tolerance 0.05:

1. The same agent with that instruction removed: an agent change.
2. OpenAI computer use (`openai-cu`, `gpt-6.1-sol`) with no added instruction: a different agent and model.

| Run | Scored | Completed correctly | Silent corruption | False success | Safe stops | Gate | Model calls |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| claude-cu + verify prompt (baseline) | 20 | 10 (50%) | 2 (10%) | 2 (10%) | 6 (30%) | - | 275 |
| claude-cu, verify prompt removed | 18 | 8 (44%) | 3 (17%) | 7 (39%) | 2 (11%) | **FAIL** | 207 |
| openai-cu | 19 | 8 (42%) | 4 (21%) | 8 (42%) | 2 (11%) | **FAIL** | 295 |

**What changed.** With the instruction, Claude found both collateral changes in both repetitions and said NOT DONE: another contact's phone was changed, or a contact was deleted. It also deleted the duplicate contact and fixed the blank or wrong phone, which counts as completed correctly.

Without the instruction, it said done on all four collateral runs and on both duplicates. OpenAI did the same: all four collateral runs and both duplicates said done.

**What no agent caught.** The late revert blanked the phone 45 s after the save, and every agent that saved said done.

**Left out.** Three runs, where the agent never made an accepted save:

- Claude without the instruction stopped early with no answer, twice;
- OpenAI, once, on the lost response.

**Two caveats.**

- The false-success rise is not noise: on the collateral and duplicate runs, false success went from 0 of 6 with the instruction to 6 of 6 without it, and 6 of 6 for OpenAI.
- The silent-corruption rise for the first candidate is one run, which is within noise.

**The model-free version.** The same check runs with `scripted-readback` as the baseline and `scripted` as the candidate. Under `act` (`runs/act-scripted`) the gate failed with silent corruption at 40% against 10%, and false success at 70% against 30%.

On GitHub's hosted runner the self-test job passed, in 5 min 18 s for the whole job. That job covered checkout, installing from source, pulling the images, starting Dolibarr twice, 20 scenarios, and the gate failing the candidate as expected. The baseline step alone, which installs, pulls, starts Dolibarr and runs 10 scenarios, took 3 min 3 s. This is the cold-start time behind the 15-minute criterion; the run was [44880123740](https://github.com/OpenAdaptAI/OpenAdapt/actions/runs/44880123740).

## The other agent kinds

All three ran on Dolibarr, 10 scenarios each, through `openadapt test run`.

| Agent | Completed correctly | Silent corruption | False success | Model calls |
| --- | ---: | ---: | ---: | ---: |
| `claude-code-mcp`: Claude Code (`claude -p --bare`, Sonnet), with only the MCP browser tools | 3 | 4 | 7 | 96 |
| `browser-use:examples/cdp_agent.py`: a model-free script on the harness's browser over CDP | 2 | 4 | 6 | 0 |
| `--agent-cmd`: a shell script that only reports NOT DONE (2 scenarios) | 0 | 0 | 0 | 0 |

The coding agent never re-read its save ("I didn't re-read the saved card to check the values"). It said done in all 10 scenarios; 3 of those were right (the two clean ones and the lost response).

The `browser-use:` run is the protocol a Browser-Use script uses (`cdp_url`), not the Browser-Use library itself, which was not installed.

## Hand check

[`handview.py`](handview.py) prints, for each run, what changed in the raw dumps (`table_before_raw.tsv`, `table_after_raw.tsv`: every column, read with the database client) and the agent's last line. It reads nothing the grader wrote.

The agent that built this harness (not a person) read that output for all 130 runs and wrote down an outcome per run in [`handcheck.json`](handcheck.json), without looking at the grader's verdicts. It is an independent re-read of the same tables, not a human audit: both read the table through the same database client. [`compare.py`](compare.py) sets those against the grader: **0 of 130 disagree (0.0%)**. The kill criterion is more than 2%.

The re-read and the grader rest on the same table read. Neither looked at tables the pack does not judge. One example is OpenEMR's other patient tables.

## Limits of this proof

- The paid comparison is one app (Dolibarr) at 2 repetitions. Use the intervals in `results.json` before reading small differences.
- The gate does not test significance.
- OpenEMR and EspoCRM ran only the model-free agents.
- The OpenAI agent's model id is the one the write leaderboard used.
- `act` is v0.2.x: it cannot run Node 24 actions, so the lab runs skip the artifact upload (`upload-artifact: "false"`). The hosted run uploads normally.
- The 15-minute figure is one hosted run on Dolibarr, the lightest stack. OpenEMR's first start runs its installer and is slower; it was not timed here.
