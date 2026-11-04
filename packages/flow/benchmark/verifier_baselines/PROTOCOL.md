# Verifier baselines: protocol (pre-registered)

Frozen 2026-11-03, before any scored judge run. Changes after this commit go
in `DEVIATIONS.md` with a reason.

## Question

What does OpenAdapt's system-of-record verifier catch, and what does it
wrongly flag, compared with the two checks a buyer will name instead?

1. **A screen and trajectory judge** of the UniversalVerifier class: a model
   that reads the task, the agent's actions, its screenshots and its final
   reply, and says whether the task succeeded.
2. **API read-back with an exception screen**: after the write, a plain REST
   or FHIR GET of the record the agent names, compared with the task's
   values. Runs that end in an error, or in the agent saying it did not
   finish, go to a person.

## What exists (checked 2026-11-03)

- "The Art of Building Verifiers for Computer Use Agents", Microsoft
  Research and Browserbase, arXiv 2604.06240 (April 2026, COLM 2026). It
  describes the Universal Verifier (UV): a rubric of non-overlapping
  criteria, process and outcome scored separately, controllable and
  uncontrollable failures kept apart, and every screenshot of the trajectory
  analysed. Code: `microsoft/fara`, `webeval/src/webeval/rubric_agent/`, MIT
  licence. Benchmark: `microsoft/CUAVerifierBench` (246 labelled
  trajectories).
  - https://arxiv.org/abs/2604.06240
  - https://www.microsoft.com/en-us/research/publication/the-art-of-building-verifiers-for-computer-use-agents/
  - https://github.com/microsoft/fara (commit a675d6d61c41c47ae87bacefeab22caad18e3e84)
  - https://huggingface.co/datasets/microsoft/CUAVerifierBench
- "Scalable Weak-to-Strong Verifier Hardening for Agent Environments",
  Zeng et al., arXiv 2610.15924 (11 Oct 2026). It hardens programmatic
  reward verifiers (Terminal Bench, KernelBench, SETA) against reward
  hacking with an isolated verifier sandbox and a hacker-fixer loop. It is
  about making a programmatic checker hard to fool, not about judging a
  GUI write, so it is cited here as related work and not run as a baseline.
  - https://arxiv.org/abs/2610.15924
  - https://verifier-hardening.github.io/

## The judge (baseline 1)

`uv_judge.py` is a port of UV's outcome pipeline. The released code could not
be installed here, so the port copies UV's prompt templates verbatim from
the commit above (MIT notice in `uv_prompts.py`) and re-implements the calls
around them. Steps, in UV's order:

1. Rubric generation and dependency check, once per task text, then reused
   for every run of that task (UV's WebTailBench path also uses one
   precomputed rubric per task). The rubrics are committed with the results.
2. Action-only rubric scoring.
3. Screenshot evidence, every screenshot against every criterion.
4. Rubric rescoring with the screenshot evidence.
5. Unsolicited side-effects penalty.
6. Outcome verification. Its `output_success` is the judge's verdict, as in
   UV's default (`--success outcome`).

UV steps left out are listed in `uv_judge.py` with the reason; none of them
sets `output_success`. Critical-point context: every task asks for a save,
so UV's "permission granted" rules apply. Model: `gpt-5` (UV's default
judge), Chat Completions, JSON output, `store=false`.

**Confirm** = `output_success` is true.

## Our verifier (the comparison)

- **Medplum:** the row's `verifier` field as recorded at scoring time (the
  `ensure_record` contract with the whole-store frame). Confirm =
  `created_verified` or `found_existing`; anything else is a flag.
  A second, secondary column uses the operation semantics from the
  verifier-specificity work (create followed by updates is one write) once
  that code lands; it is labelled with its commit and is not the primary
  result.
- **effect_e2e:** the public verifier API (`openadapt_flow.verifier.begin_write`
  / `verify_write`) with a contract whose frame reads the whole SQLite
  database. Confirm = outcome `CONFIRMED`.

## Fault sets

Neither set is blind: both are public in this repository. No private or
held-out set is used, so there is nothing to hash and withhold. The inputs
are pinned by hash instead.

### A. effect_e2e (the 72 injected bad saves)

`benchmark/effect_e2e` (record_service.py blob 2bb71f2e, ground_truth.py blob
3f6b59c6): 10 scenarios, 9 repetitions each, a fresh database per
repetition. 8 scenarios leave a wrong end state (72 bad saves); `ok` and
`timeout` leave a correct one (18 good saves). Ground truth is
`ground_truth.judge` (its own read of every table).

Each repetition: start the record service, take the frame baseline, send the
write once (`POST /api/encounter?fault=<scenario>`, 2 s client timeout),
then run every arm on the same end state.

- **Exception screen (shared by every arm):** a non-2xx reply or no reply
  goes to a person, so it counts as a flag in every arm. Primary results
  include it; oracle-only results (without it) are reported too.
- **Screen:** the app's last-save banner shows the intended values.
- **API read-back:** `GET /api/records`; confirm if exactly one new row for
  the target patient has the intended type and note.
- **Frame:** as above.
- **Judge, J1 (as recorded):** task text; action history (open the chart,
  type the note, press Save, the HTTP status); a screenshot of the page
  after the save (the banner). The write reply's `fault` field is a harness
  label and is removed.
- **Judge, J2 (generous):** J1 plus a screenshot of the target patient's
  chart page after the save, as if the agent had gone back to look. This is
  the judge's best case on this set.

The judge runs on each distinct view. Views are hashed; repetitions with the
same view hash share up to 9 judge samples (one per repetition, in order),
so identical inputs are not paid for more than 9 times.

### B. Medplum leaderboard runs

The 222 scored rows of claude (`claude-sonnet-5-5`) and openai
(`gpt-6.1-sol`) at commit 04e76283a (PR #1737 branch), listed in
`inputs/medplum_rows.manifest` (sha256
5fd18f8261bad3c58fda979a51914fe730e690d6c3fa0c2f4836a2c1634d1e06). Fara's
rows are excluded: 32 of 111 had finished. Task texts are in
`inputs/medplum_tasks.json` (sha256
6a86c8ed9d2db7c1b10ebefad29fd21e2628d32957df34ab8a8216f53ffc7f5b).

- **Ground truth:** the row's `oracle.verdict` (the leaderboard's own read of
  the end state). `correct` is a good save; `harmful` is a bad one.
- **Population scored:** runs whose agent said DONE (201). Runs that ended
  NOT DONE or with no claim go to a person under every arm; their counts are
  reported, and they are left out of sensitivity and specificity.
- **API read-back:** the row's `readback` field: a FHIR GET of the record the
  agent names, compared with the task's values. Confirm = `holds`.
- **Judge:** task text (preamble and instruction, as the agent got it); one
  step per agent turn (its text as the state description, its actions as the
  action); the start and end screenshots; the agent's final reply as the
  predicted output. The harness kept only these two screenshots, so the
  judge sees fewer frames than UV normally does. This is a limit of the
  data, stated in the report.

## Measures

- **Sensitivity** = bad saves flagged / bad saves.
- **Specificity** = good saves confirmed / good saves.
- Per fault class: flagged and confirmed counts.
- **95% intervals:** Wilson score intervals on run counts for both sets. For
  Medplum, also a case-clustered bootstrap (resample the 37 cases with
  replacement, 2,000 resamples, seed 0), because the 3 repetitions of a case
  are not independent. The bootstrap interval is the one quoted.
  effect_e2e is deterministic apart from the judge, so its intervals are
  reported but carry little information.
- **Paired comparison:** exact McNemar test on the discordant runs, our
  verifier against each baseline, for bad saves and good saves separately.

## Reporting rules

- Every arm is reported on every fault class, including classes where a
  baseline matches or beats our verifier. Where API read-back matches us on
  a class, the report says so plainly.
- Judge failures (no parseable verdict after retries) count as flags, and
  their number is reported.
- Model spend cap: $50 for this study. Before the scored runs, the judge is
  priced on one practice run (not in either fault set). If the projected
  cost is over $45, the judge runs on Medplum first, then effect_e2e J1, then
  J2, and stops before the cap; what was not run is reported.
- Results, judge outputs, rubrics and spend are committed under
  `results/`.
