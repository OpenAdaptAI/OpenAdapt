# What OpenAdapt's verifier adds over a screen judge and an API read-back

Internal report, 2026-11-03. Protocol: [PROTOCOL.md](PROTOCOL.md), committed
before any scored judge run (c7b2b09a4). Deviations: [DEVIATIONS.md](DEVIATIONS.md).
Every number below is in [results/SCORE.md](results/SCORE.md) and
[results/score.json](results/score.json), computed by `score.py`.
Per-run rows, judge outputs, the replayed verifier verdicts and the spend
ledger are run-level evidence and are kept in the private corpus
(`openadapt-corpus`, `paid_agent_evidence/2026-11-03-verifier-baselines/`).

## Summary

- **Against a screen or trajectory judge, the verifier's lead is large on
  both fault sets.** On effect_e2e's 72 bad saves, a port of Microsoft's
  Universal Verifier flagged 18 when it saw what the run saw, and 44 when it
  was also shown the patient's chart page. Our whole-database verifier flagged
  all 72. On the Medplum agent runs the judge flagged 22 of 61 bad saves and
  ours 55; the two confirmed good saves at similar rates (97 and 103 of 140).
- **API read-back catches wrong values as well as we do, and with fewer
  false stops.** On Medplum's wrong-value saves, read-back flagged 10 of 10
  bad ones and confirmed all 37 good ones; our verifier also flagged 10 of 10
  but stopped 11 good ones. Read-back misses everything off the record it
  reads: writes to other records, duplicates, a record that landed despite an
  error, and wrong patient (1 of 45 such bad saves on Medplum, against our
  45; 0 of 9 on effect_e2e).
- **Our verifier stops 37 of 140 good Medplum saves (74% confirmed); read-back
  stops none.** That is the price of the off-record catches. Most of the 37
  are wrong-value, wrong-patient, duplicate and lost-response cases where the
  end state was acceptable, plus 6 clean runs. The exploratory "either"
  operations rule from the verifier-specificity study confirms 121 of 140
  with the same 55 catches.

## The three checks

1. **UV-class judge.** A port of the Universal Verifier from "The Art of
   Building Verifiers for Computer Use Agents" (Microsoft Research and
   Browserbase, arXiv 2604.06240; code in `microsoft/fara`, MIT). Its prompt
   templates are copied verbatim with the MIT notice (`uv_prompts.py`); the
   pipeline around them is re-implemented (`uv_judge.py`), because installing
   the released package was not permitted here. gpt-5, as in UV's default.
   The verdict is UV's `output_success`. o4-mini, UV's action-only scorer, is
   retired by OpenAI, so gpt-5 does that step too.
   The second paper the outside review cites, "Scalable Weak-to-Strong
   Verifier Hardening" (arXiv 2610.15924), hardens programmatic reward
   checkers against reward hacking; it is not a judge of GUI writes and is not
   run here.
2. **API read-back with an exception screen.** A REST or FHIR GET of the
   record the run names, compared with the task's values. A run that ends in
   an error, or with the agent saying it did not finish, goes to a person.
3. **OpenAdapt's verifier.** On effect_e2e, the public `verify_write` API
   with a frame over the whole SQLite database. On Medplum, the
   `ensure_record` contract with the whole-store frame, replayed offline on
   each run's frozen server state (verifier-specificity commit debdfe351,
   PR #1795), because 163 of the 222 recorded verdicts were "unreadable:
   HTTP 401" from an expired read token (DEVIATIONS.md 3 and 6).

## effect_e2e: 72 bad saves, 18 good

8 kinds of injected bad save, 9 repetitions each, plus 18 good saves (a clean
save and a save that committed but timed out). Deterministic apart from the
judge.

| Check | Bad saves flagged | Good saves confirmed |
|---|---|---|
| OpenAdapt whole-database verifier | **72/72** | 9/18 |
| API read-back | 63/72 | 9/18 |
| Screen banner | 18/72 | 9/18 |
| UV-class judge, J1 (what the run saw) | 18/72 | 9/18 |
| UV-class judge, J2 (plus the chart page) | 44/72 | 9/18 |

All checks stop the 9 timed-out good saves, because the exception screen
sends an unanswered save to a person; without the exception screen, every
check except J1 confirms all 18 good saves.

- **J1 sees nothing to judge.** For the 6 kinds of bad save the app accepts
  with HTTP 200, the run's actions and its after-save page are identical to a
  good save's, so the judge confirmed them all, 9 of 9 times. It caught only
  the two kinds the app rejected with an error (409, 401), which the
  exception screen already catches.
- **J2, given the patient's chart page, caught missing, wrong-patient and
  (8 of 9) duplicate saves.** It still confirmed every partial save, where
  the note was dropped. The chart showed a Triage row with an empty note;
  the judge's reasoning reads the blank as "the chart list view does not
  display the note text in its summary column" and trusts the green banner.
  It also confirmed every stale save, which deleted another clinician's
  urgent encounter, and every save with a stray billing row, because neither
  shows on the patient's chart.
- **Read-back missed only the stray billing row** (9 of 9), which is on a
  table it does not read. This reproduces the original study.

## Medplum: 201 agent runs that said DONE

claude-sonnet-5-5 and gpt-6.1-sol, 37 public cases, 3 runs each (leaderboard
PR #1737, commit 04e76283a). 61 runs ended with a bad record, 140 with a
correct one. 21 runs that ended NOT DONE go to a person under every check
and are not scored. Intervals are case-clustered bootstrap, 95%.

| Check | Bad saves flagged | Good saves confirmed |
|---|---|---|
| OpenAdapt verifier (contracts main, replayed) | **55/61** = 90% (69–100) | 103/140 = 74% (60–85) |
| API read-back | 11/61 = 18% (5–38) | **140/140** = 100% |
| UV-class judge | 22/61 = 36% (23–49) | 97/140 = 69% (59–79) |

Other columns of our verifier, all with the same 55/61 catches:
operations contracts as pre-registered by the verifier-specificity study,
97/140 confirmed; its exploratory "either" rule, 121/140. The replay as
first registered, before that study's replay fixes (DEVIATIONS.md 6), gave
60/61 and 47/140; the recorded field, broken by the expired token, 60/61 and
28/140.

All 6 of our misses are late_collateral: a stray write that the harness
makes after the check finishes, which a single check cannot see by design.
The first-registered replay "caught" 5 of them only because its window ran
past the harness's next step.

Paired, exact McNemar: on bad saves our verifier was right where read-back
was wrong 44 times and never the other way (p < 1e-6); against the judge,
36 to 3 (p < 1e-6). On good saves read-back was right where we were wrong
37 times (p < 1e-6). Against the judge on good saves, 34 to 28 our way
(p = 0.53): no detectable difference.

### By fault class

| Class | Bad / runs | Ours flagged | Read-back flagged | Judge flagged |
|---|---|---:|---:|---:|
| collateral (another record changed) | 24/24 | 24 | 0 | 9 |
| duplicate | 11/18 | 17 | 0 | 4 |
| lost_response (landed despite an error) | 8/12 | 12 | 0 | 2 |
| late_collateral (stray write after the check) | 6/6 | 0 | 0 | 3 |
| wrong_patient | 2/11 | 11 | 1 | 5 |
| wrong_value | 10/47 | 21 | 10 | 15 |
| clean | 0/60 | 6 | 0 | 17 |
| other_user (another user's legitimate edit) | 0/17 | 0 | 0 | 10 |
| refused_write | 0/6 | 1 | 0 | 0 |

"Flagged" counts every run flagged in the class, good and bad; in a class
with no bad runs every flag is a false stop.

- **Where read-back matches us: wrong values.** Both flag all 10 bad
  wrong-value saves. Read-back stops none of the 37 good ones; we stop 11.
  If the buyer's risk is "the record says the wrong thing", read-back is as
  sensitive as our verifier here and more specific.
- **Where only we catch it: harm off the named record that has landed by the
  check.** Writes to another record, duplicates, saves that landed after an
  error, and the wrong patient: read-back flagged 1 of these 45 bad saves,
  the judge 15, and we flagged all 45.
- **Late writes need a second look.** No check here caught a write that lands
  after it; the judge flagged 3 of 6 only from the agents' own replies.
- **Off the record, the judge catches some harm and read-back almost none.**
  It flagged 9 of 24 collateral saves. It can also ignore a plain warning: in
  claude r1 P09 the agent said another patient's "Last Updated" time had
  changed and it could not explain why; the judge's reasoning does not
  mention it, and it confirmed the run (rubric 0.93, no side-effect
  penalty).
- **Our 37 false stops:** wrong_value 11, wrong_patient 9, duplicate 6,
  clean 6, lost_response 4, refused_write 1. Outside the clean runs they have
  two causes. In 22, the agent noticed the fault and corrected its record
  afterwards, so the end state is right but a version in between was not;
  the contract refuses any wrong intermediate version. In 9, the agent
  created a second record (the duplicate or lost-response fault) and the
  oracle accepts the end state. Whether a corrected or deduplicated record
  should pass is a policy choice, not a reading error. The judge's 43 false
  stops are clean 17, wrong_value 11, other_user 10 and wrong_patient 5.

## What this means for the product

1. **The defensible claim is "off-record harm".** Against read-back the gain
   is concentrated in writes off the named record: collateral, duplicates,
   writes after an error, wrong patient. On the record's own values,
   read-back is enough.
2. **Against screen and trajectory judges the gain is broad.** A judge
   cannot see what the screen does not show; given the chart page it still
   misread a blank field and could not see deletions or other tables.
3. **Specificity is the cost to keep shrinking.** On Medplum we confirm 74%
   of good saves, against read-back's 100%: a quarter of good agent saves
   would still reach a person. The exploratory "either" operations rule
   reaches 86% with no lost catches; effect_e2e's 100% (oracle only) shows
   the frame can be fully specific when the app writes one row per save.
4. **A single check misses late writes.** All our Medplum misses are writes
   after the check; a scheduled re-read closes that.

## Limits

- Neither fault set is blind: both are public in this repository.
- effect_e2e is deterministic and hand-authored; its rates are coverage of
  its taxonomy, not field incidence.
- The Medplum harness kept only the first and last screenshot of each run,
  so the judge saw 2 frames, not every step as UV normally does. Each step's
  text and actions were given.
- The judge is a port, not the released code; skipped UV steps are listed in
  `uv_judge.py` and none sets the verdict. The CP-profile wording is ours.
- J2 is a constructed best case for the judge on effect_e2e: the run never
  opened the chart.
- Medplum's ground truth is the leaderboard's own oracle, which reads the
  end state through the same FHIR server.

## Cost

Model spend $59.28, under the $70 cap the coordinating session approved
(DEVIATIONS.md 4): Medplum judge $44.60, effect_e2e J1 $3.73, J2 $9.89,
rubrics $0.71, the port's smoke test $0.35. The ledger is `spend.jsonl` in
the corpus folder.

## Reproduce

From `packages/flow`, with `OPENAI_API_KEY` set, Playwright installed and
`VERIFIER_BASELINES_DATA` pointing at the corpus folder above:

```bash
python -m benchmark.verifier_baselines.effect_e2e_arms --n 9
python -m benchmark.verifier_baselines.run_judge medplum
python -m benchmark.verifier_baselines.run_judge effect_e2e
python -m benchmark.verifier_baselines.score
```

The Medplum verifier column comes from
`benchmark.fhir_medplum.leaderboard_rescore` at debdfe351 (PR #1795):
its `results/rescore-*.json`, copied to `medplum/verifier_rescore_*.json` in
the corpus folder.
