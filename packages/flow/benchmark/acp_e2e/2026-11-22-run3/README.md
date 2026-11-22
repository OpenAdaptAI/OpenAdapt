# Agent control plane, end to end (run 3, 2026-11-22)

This run repeats run 2 (`../2026-11-22/`) on newer PR heads, with the same steps and drivers. Everything ran headless on one Mac against synthetic MockMed on 127.0.0.1. Step 5 is the only step that called a model. It made 12 Claude calls for $0.108 in total.

## Integration tree

The integration commit is `128faaec3`. Its base is origin/main `7a11a8d54`, and these heads were merged in this order. No branch had moved past the SHAs the brief gave.

| PR | Piece | Head | Conflicts |
|---|---|---|---|
| #2493 | P3 flight recorder (run pseudonyms) | `9da11b205` | none |
| #2498 | P1 readiness (contains P3) | `ac0e45550` | none |
| #2478 | P2 ledger | `af6d5432b` | none |
| #2492 | P5 gate card | `40d4e9b14` | `CAPABILITIES.md` (2 blocks), `llms-full.txt` |
| #2494 | P6 exceptions | `2fb05fae1` | none |
| #2491 | P7 fallback | `3b73d093e` | none |

The conflicts were resolved the same way as in run 2:

- In `CAPABILITIES.md`, both blocks kept both sides (`merge/resolve_keep_both.py`). The `guard` row and bullet take P5's wording, and the `flight` row and bullet from P3 stay.
- `llms-full.txt` took HEAD's side and was then regenerated in `128faaec3`. The command map needed no change.

Merging `128faaec3` into this evidence branch conflicted on the same two files. The new tree's versions were taken.

Cloud ran from openadapt-cloud#551 at `7d31899a2`, as a `next dev` server in mock mode on 127.0.0.1.

## Unit tests

The tests ran single process through `~/oa/scratch/cpu-slots/run.sh` (`merge/run_tests.sh`), covering every unit test file the PRs add, plus `test_cli_tasks.py` and `test_capabilities_map.py`.

| Suite | Result |
|---|---|
| Flow, 29 files | 741 passed |
| Types | 38 passed |
| Desktop | 32 passed in a Desktop venv, because the Flow venv lacks `loguru` |
| Launcher | 5 passed |

No test failed.

## Step 1: shadow mode

The full numbers are in `results_step1.json` and `1-shadow/`.

- **20 tasks** under `--egress blocked`: 0 writes reached the app and the store was unchanged.
- **Matched 13 of 20.** The 7 mismatches are 5 planted field errors and 2 wrong records.
- **Scorecard:** `scope_ok` true and $0.00 metered. Median time is 2.02 s for the agent against 0.92 s for the scripted person, so the savings estimate is negative: -$0.15.
- **Instructions:** `readiness import` now names the record in each task's inputs (`record_name`, `record_patient_id`) and in the instruction.
- **Driver fix:** the first attempt matched 0 of 20. The e2e scripted agent now asserts that the task names its record (`true_patient` in its plan), and run 2's driver did not pass that field. The driver was fixed and step 1 was rerun.

The adversarial second-tab agent ran once (route `create_target`). No write landed. The case is `NOT_CHECKED` with reason `extra_target`, the card has `scope_ok: false`, and the scope statement now says what failed. `readiness report` exits 2 on this card.

## Step 2: gated runs

The full numbers are in `results_step2.json`, `results_step2b.json` and the `2-gated/runs/*/guard_ledger_row.json` files.

| Run | Guard verdict | Ledger row |
|---|---|---|
| approve, correct (write adapter) | VERIFIED | VERIFIED |
| approve, wrong value, no adapter | REFUTED after the save | CAUGHT_ERROR |
| wrong value, with adapter | blocked `write_value_not_declared`, no card | CAUGHT_ERROR |
| decline | blocked `declined` | ESCALATED, `approver_kind: person` |
| wrong patient | blocked `write_wrong_record`, no card | CAUGHT_ERROR |
| not saved | RECONCILIATION_REQUIRED | UNKNOWN |
| clean, 2 runs | VERIFIED | VERIFIED |

- **No-adapter card:** it shows "not read" and never says "exactly". The wrong note still lands; the read-back catches it after the save.
- **Decline:** the row now records the person (`approver_kind: person`), fixed in 40d4e9b14. In run 2 it said `none`.

The P6 watch ran against a synthetic one-time-code dialog:

- It raised a `second_factor` card by itself.
- It blocked the agent's save while the card was open: HTTP 409 `exception_open`, with 0 writes.
- After the person's answer, the next save read back VERIFIED, with 1 write.

## Step 3: ledger and dashboard

`ledger add` ran once per run, 36 runs in all.

- **Rows:** 36, with 36 distinct row ids. Buckets: VERIFIED 5, ESCALATED 19, CAUGHT_ERROR 10, FAILED 0, UNKNOWN 2. Model cost $0.1082, unknown for 9 runs.
- **Export and push:** `ledger export --require-scrubber` wrote 36 rows. `ledger push` to the dev server sent 36 of 36 with nothing adapted, and no refusals. The three scorecards were posted as raw bytes and returned 201 each.

What the dashboard showed (`3-dashboard/*.png`):

- **Ledger page, production runs:** 12 cases. Verified 5, Escalated 3, Error caught 3, Failed 0, Unknown 1. Minutes and dollars saved: unknown. Model cost "$0.00 for 3 runs, 9 unknown". Human baseline "1 s per case".
- **Ledger page, shadow runs:** 24, shown apart from the outcome counts. Matched 16, field mismatch 5, wrong record 2, unguarded extra target 1.
- **Readiness page:**
  - vendor-agent-standin: 13 of 20, 2 risks.
  - adversarial-second-tab: "Scope check failed", and the card says the scope claim was not met.
  - claude-sonnet-5-5-computer-use: 3 of 3 matched, $0.1082 metered, 12 calls, scrubbed.

## Step 4: compiled run with a forced drift

The full numbers are in `results_step4.json`. The path is the same as run 2: `dispatch_cli.run` with the real CLI parser and the gate the CLI builds for item approval. There was no terminal. The task setup and a scripted fallback agent were injected, for the reasons given in run 2.

| Run | Path | Outcome | Saved |
|---|---|---|---|
| no drift | program | VERIFIED, 0 model calls | 1 |
| drift, approved | agent fallback | VERIFIED, 1 signed approval, repair opened | 1 |
| drift, declined | agent fallback | ESCALATED (`declined`) | 0 |

## Step 5: three real Claude computer-use tasks

The full numbers are in `results_step5.json`. The adapter was `claude_cu_agent.py` (claude-sonnet-5-5), run with `--egress scrubbed` and pseudonyms on, the proxy's default. The caps were `--spend-cap 0.60` per task and `OPENADAPT_CU_MAX_USD=0.60`, so the 3 tasks together stayed under $2. Tasks 0 to 2 were re-recorded by the scripted person on a fresh app so the episodes match its origin. The instructions are the ones `readiness import` wrote; none was edited by hand.

| Task | Status | Matched | Identity | Model calls | Metered cost |
|---|---|---|---|---|---|
| 0000 | HELD | MATCHED (type, note) | verified | 4 | $0.035427 |
| 0001 | HELD | MATCHED (type, note) | verified | 4 | $0.036320 |
| 0002 | HELD | MATCHED (type, note) | verified | 4 | $0.036476 |

The total spend was $0.108223, with 0 writes. The agent matched the `[PERSON_n]` tokens: the record's name arrived as `[PERSON_1]` in the instruction, and the scrubbed screenshots show the same token in the patient list.

The run's token map was checked locally; it is not committed. It also turned the app name, a body-part word from a referral reason, two field labels and the screen size into tokens. They are not quoted here because the grep gate treats every value in the map as personal data. These are over-detections, and they did not stop the agent.

## Evidence copy and leak checks

- **Allowlist copy:** files were copied only by `scripts/copy_evidence.py` (`copy_report.json` lists what was copied and what was left).
  - Readiness folders go through Flow's own `readiness.export.export_task` with `include_values=False`.
  - Guard rows keep only their known keys.
  - Ledger summary, export and push payloads, the drivers, a fixed list of PNGs, and value-free `results_*.json`.
  - Left behind: `pseudonyms.json`, `case.json`, `proposed_write.json`, held requests, the agent's screenshots, logs, approver keys, `*.private.json`, recordings and databases.
- **Planted-file check:** `scripts/check_no_leak.py` plants `pseudonyms.json`, `egress/pseudonyms.json` and two stray files holding a canary value in a real run folder. It shows that neither the ledger's push payload nor the evidence copy contains the canary or the files (`results_leak_check.json`, `ok: true`).
- **PHI grep gate:** before committing, the gate searched every copied text file for 55 synthetic values, plus the API key prefix. The values were MockMed's patient names, MRNs and DOBs, every note the person, agents and fallback typed, and every value in the 3 token maps. **Result: 0 hits.**
  - One value is excluded by name: the app's product name "MockMed". The scrubber put it in the token map, and it appears in every task id.
  - The PNGs were not grepped. They are only the two dashboard pages and the two rendered scorecards, which show no record values.
- **Secret scans:** gitleaks found no leaks, and grepping the staged tree for the full API key found nothing.

## Breaks and open questions

1. Without a write adapter, a person can approve a save whose values were never read, and the wrong value lands. The card says so; the read-back catches it after the save.
2. The scrubber over-detects. The app name, a body-part word, field labels and the screen size become tokens. The agent still worked here.
3. Shadow rows list `fields_written` (note, patient_id, type) although a shadow run writes nothing; these are the fields it proposed.
4. Start times are filled now, except for the P7 dispatch rows, the adversarial row and the two guard rows blocked before a card.
5. The approved fallback row still shows intervention "unknown" and fields written "none", despite a signed approval and a verified write.
6. The P6 run read back VERIFIED but is in the ESCALATED bucket (`second_factor`).
7. The decline row's duration is 60.1 s on the dashboard. The approval was decided about 3 s after it opened.
8. The merged tree has no `wrap-dispatch` run with a scripted agent from the CLI alone.
9. The e2e scripted agent now needs a `true_patient` plan field. Run 2's driver, reused as is, gave 0 of 20.
