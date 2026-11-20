# Protocol: grader soundness on public agent benchmarks

Written and committed before any cell below was run. Changes after the first
scored cell go in `DEVIATIONS.md`, dated, never in place.

## Question

When a benchmark's own grader says PASS, did the intended change happen, and
nothing else? When it says FAIL, was the outcome actually wrong?

For each benchmark we measure two error rates of the grader, against an
independent read of the end state:

- **False accept**: the grader passes an end state that is wrong in a named
  way.
- **False reject**: the grader fails an end state that matches the correct
  one.

## Benchmarks and licenses

| Benchmark | Version | Code license | Data terms | How it runs |
|---|---|---|---|---|
| AgentDojo | `agentdojo` 0.1.35 from PyPI, suites `v1.2.2` | MIT | MIT, synthetic | In process, from a separate virtual environment. No AgentDojo file is copied into this repository. |
| AppWorld | `appworld` 0.1.3.post1, data `0.1.0` | Apache-2.0 | Protected data: Apache-2.0 plus a rule that public redistribution of it or its derivatives must be encrypted | Out of process: the kit's `appworld_worker.py` in AppWorld's own interpreter, from an external install. Results are written with `--redact`. |

A third benchmark is added only if it is permissively licensed and runs
locally without a model. Candidate: tau-bench (MIT, Sierra). Its grader
replays the task's ground-truth actions and compares a hash of the whole
database, plus a substring check of required outputs. If it is added, it is
added before its first scored cell, with its own adapter written here, and
logged in `DEVIATIONS.md`. Nothing GPL, AGPL, LGPL or SSPL is vendored.

## Task sample

- **AgentDojo**: every user task of the four `v1.2.2` suites (banking 16,
  slack 21, travel 20, workspace 40; 97 tasks). No sampling.
- **AppWorld**: the 48 tasks whose reference trajectories were recorded for
  the verifier-audit kit (#1783): 24 from `train`, 24 from `dev`, 16 task
  scenarios with 3 variants each. Each reference is the API-call log of the
  task's shipped solution. The references are AppWorld derivatives, so they
  stay outside this repository; only task ids and redacted cells are
  committed.

A task enters the analysis only if (the kit's existing rules):

1. the grader accepts the unedited reference,
2. two replays of the reference from a fresh reset give the same effect, and
3. the reference changes the state at all (answer-only tasks are out, since
   this study is about writes).

Excluded tasks are counted and listed with the reason.

## Solutions: three classes

Every solution is the reference trajectory edited mechanically. No model
writes any solution. No edit is chosen by looking at a grader verdict: the
runner first replays each candidate edit **without scoring** (the census),
decides from the effect alone whether the candidate is the named class, and
only then scores it.

1. **Correct**: the unedited reference. This is the control, and it is the
   exclusion rule above.
2. **Wrong** (harmful families, from
   `openadapt_flow.verifier_audit.families`, unchanged):

   | Family | Requested name | Edit |
   |---|---|---|
   | `wrong_record` | wrong record | One task write lands on a different record of the same type |
   | `wrong_value` | wrong field | One task write carries a changed non-identity value |
   | `unauthorized_side_effect` | collateral write | All task writes, plus a copy of one aimed at a record the task never touches |
   | `partial_write` | missing write | A multi-write task sends only its first write, or drops its last |
   | `optimistic_success` | missing write (all) | Reads and the success claim, no writes |
   | `duplicate` | duplicate write | One task write is sent twice |
   | `late_write` | write after claim | The last write is sent after the success claim |

   A wrong cell counts only when the census shows the edited call was
   accepted by the environment and the end state differs from the
   reference. Otherwise it is listed as degenerate or refused.

3. **Valid alternative** (legitimate families, new in
   `grader_soundness/alternatives.py`): a different trajectory whose end
   state the census shows is **identical** to the reference end state.

   | Family | Edit |
   |---|---|
   | `repeated_reads` | The reference's reads, repeated before the claim (the kit's `incidental_change` edit) |
   | `foreign_reads` | Read-only calls taken from other tasks of the same suite, with tools this task does not use, inserted before the claim. AgentDojo only: AppWorld reads need per-task tokens |
   | `reordered_writes` | Two consecutive task writes sent in the other order |
   | `refused_attempt` | Before a task write, a copy of it aimed at a record id that does not exist; kept only if the environment refused that call |
   | `self_corrected_write` | A task write first sent with a changed value, then sent as in the reference |

   A valid-alternative cell counts only when the census end state equals the
   reference end state exactly (same multiset of effect rows) and the scored
   replay reproduces it. Every other candidate is listed as not equivalent
   and never counted. Equality of the end state is the definition of
   "valid" here; the study does not judge alternatives whose end state
   differs.

Up to 6 candidates per family per task, in the order the family lists them.
The first candidate that passes the census is scored.

## Independent read of the end state

The ground truth is the target adapter's effect read, which never calls the
grader:

- AgentDojo: a field-level diff of the environment's `model_dump` before and
  after, with access logs (`web.web_requests`) and wall-clock stamps
  normalized.
- AppWorld: a row-level multiset diff of a full SQLite dump against the
  task's seed dump, read with `sqlite3`.

## Grader under test

- AgentDojo: `TaskSuite._check_task_result` (the suite's `utility`, or
  `utility_from_traces` where a task defines it), with the reference's
  ground-truth output as the final answer.
- AppWorld: `AppWorld.evaluate` (the task's shipped tests).

## Metrics

Per benchmark and per family, never pooled across benchmarks:

- False-accept rate = FALSE_ACCEPT / (FALSE_ACCEPT + CAUGHT), over counted
  wrong cells.
- False-reject rate = FALSE_REJECT / (FALSE_REJECT + ACCEPTED), over counted
  valid-alternative cells.
- Share of tasks with at least one false accept.
- Wilson 95% score intervals on every rate, with n.

**Unspecified-value rule for `wrong_value`.** A changed value is only wrong
for certain when the task instruction names the original value. For
`wrong_value` cells the kit records whether the original value appears as a
whole word in the instruction (`original_value_in_instruction`: true, false,
or unknown when the value is under 3 characters). The primary `wrong_value`
rate uses only cells where it is **true**. Cells where it is false or
unknown are reported in a separate row as "unspecified value", counted
neither as false accept nor as caught.

**Headline** per benchmark: one pooled false-accept rate over all counted
wrong cells (with `wrong_value` restricted as above) and one pooled
false-reject rate over all counted valid-alternative cells, each with its
Wilson interval and n. Cells from the same task are not independent, so
the pooled intervals are narrower than they should be; the per-family rows
and the task-level share are reported next to them, and the pooled rate is
labeled as descriptive.

**Counterexamples.** Every false accept and false reject is listed with the
task id, the edit, the effect diff and the grader's verdict (AppWorld:
redacted digests plus call names). At least one concrete counterexample per
family with any hit is described in plain words in `RESULTS.md`.

## Stop rule

- One pass over the full sample per benchmark. No task is added or removed
  after the first scored cell.
- A rerun of the same ledger is allowed only to retry cells that ended in
  `HARNESS_ERROR` or `SCORER_ERROR` (the kit's resume rule), at most twice.
- If a harness bug changes any classification, the fix is logged in
  `DEVIATIONS.md` and the whole benchmark is rerun into a fresh ledger;
  both ledgers are kept.
- Wall-clock cap: 8 hours per benchmark. Whatever is unfinished at the cap
  is reported as not run.

## Rules

- Synthetic, benchmark-provided data only. No model calls (graders are
  deterministic; no step needs a model).
- Tests and runs go through the workspace CPU-slot wrapper. Disk stays at
  or above 30 GiB free.
- Prior exposure: the author saw exploratory, non-preregistered results of
  the #1783 kit on both benchmarks before writing this protocol. Those runs
  are not reused as data; every number in `RESULTS.md` comes from ledgers
  written after this file was committed.

## Outputs

- `results/<benchmark>/<suite>/` per run: `cells.jsonl`, `ledger.json`,
  `report.json`, `REPORT.md` from the kit, plus `alternatives.jsonl`.
- `results.json`: the combined summary.
- `RESULTS.md`: tables, counterexamples, and what could not be run.
