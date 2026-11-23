## Measured results

- **OpenEMR referral, 62 timed runs each, warm.** OpenAdapt took a median of
  29.7 s and a computer-use agent (Claude) 36.5 s. Both saved 62 of 63
  referrals correctly. OpenAdapt made no model calls; the agent cost about
  $0.08 per referral. Raw data and
  figures: [benchmark/openemr_referral_timing](packages/flow/benchmark/openemr_referral_timing/).
- **Guard an agent you already run, MockMed.** A scripted Playwright agent
  in its own browser, 90 runs at a high shared load (median load average 29).
  Of the 8 `effect_e2e` faults that leave a wrong record, the agent alone
  reported 6 as saved; with the guard, the read-back caught 7 and 1 (a stray
  write to a table the REST read doesn't cover) got through; the compiled
  program caught all 8. The guard blocked the saves it can see: a save on the
  wrong patient and a second submit. Same-load pairs: the guarded agent took
  6.3 s (6 actions, each a model call for a real agent), the compiled tool
  4.4 s with no model calls:
  [GUARD_AN_AGENT.md](packages/flow/docs/GUARD_AN_AGENT.md#measured-on-mockmed).
- **Fault test.** We injected 8 kinds of bad save, 9 runs each, alongside
  clean runs: 72 runs left a wrong record behind. A check that trusts the screen's "Saved" banner reported 54 of those
  as success; reading the record back over the app's REST API missed 9; the
  complete database read path caught all 72. The test app's database is
  SQLite with two record tables, so "every table" there is two tables.
  Separately, 9 of the 18 correct saves stopped in every arm: the server
  committed the save but its answer timed out, so the write step halted before
  any read-back ran. The count is of end-to-end run outcomes. The fault set and method are in
  [EFFECT_E2E.md](packages/flow/benchmark/effect_e2e/EFFECT_E2E.md).
- **OpenEMR referral: agent, screen program and API step.** Median wall time
  per referral, from a fresh browser through sign-in, the entry and the read-back:
  agent alone 36.5 s (62 of 63 correct, the 14 to 15 Oct study); compiled program
  on the screen 22.5 s (18 of 18); the same program with the API step 3.3 s
  (18 of 18). The program rows ran on 19 Oct, interleaved on one copy, with
  the approval gate off; the benchmark harness admitted the API step:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#openemr-agent-screen-program-and-api-step).
- **An agent's claims versus the read-back, on OpenEMR.** With faults injected
  in the fixture's database (partial, wrong value, duplicate, not persisted,
  another chart deleted, another chart edited; 5 items each), Claude Sonnet 5.5
  computer use claimed success on all 30 bad saves. `verify-run` caught 25 and
  confirmed 5 of 5 clean saves with no false alarms:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#the-agents-claims-versus-the-read-back).
- **An agent's claims versus the read-back.** Claude Sonnet 5.5 computer use,
  working alone on a test app whose server broke the save in 6 ways (5 items
  each), reported all 30 bad saves as done. `verify-run` flagged all 30 and
  confirmed all 10 clean saves, with no false alarms. The app shows "saved"
  before the server answers, so this measures what the screen can't show, not
  the agent's care:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#the-agents-claims-versus-the-read-back).
- **openIMIS claims.** On a test copy of openIMIS, the open-source insurance
  and claims system that runs Nepal's national health insurance scheme, the
  compiled program entered 149 synthetic claims with no model calls: 148 read
  back correct and none were saved wrong. Each saved claim was read back by
  SQL through a read-only database role on the test copy; the 149th stopped
  before saving:
  [SCOREBOARD.md](packages/flow/benchmark/scoreboard/SCOREBOARD.md#openimis).

The systems each result ran on are listed in
[LIMITS.md](packages/flow/docs/LIMITS.md#validated-on).
