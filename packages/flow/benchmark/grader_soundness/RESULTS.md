# Grader soundness: results

Protocol: [PROTOCOL.md](PROTOCOL.md), committed before any scored cell.
Deviations: [DEVIATIONS.md](DEVIATIONS.md). Summary:
[results/results.json](results/results.json). AgentDojo ledgers:
`results/agentdojo/`. AppWorld: aggregate counts only
(`results/appworld/aggregate.json`); the per-task ledgers are kept privately
because AppWorld's data terms forbid unencrypted redistribution of its
protected data or derivatives. Run date: 2026-11-11.

## The question

When a benchmark's own grader says PASS, did the intended change happen,
and nothing else? We replay each task's reference solution with mechanical
edits, read the end state directly (not through the grader), and compare.

- A **false accept** is a wrong end state the grader passes. Following the
  protocol's counting rule, a wrong cell counts only when the end state
  differs from the reference.
- A **false reject** is an end state identical to the reference that the
  grader fails.

No model was called. Every solution is the reference trajectory edited by a
fixed rule. Every rate below has its count, n, and a Wilson 95% interval.
Rates are per benchmark and never pooled across benchmarks.

## Headline

| Benchmark | Tasks analyzed | False accepts, end state wrong | False rejects | Tasks with at least one false accept |
|---|---|---|---|---|
| AgentDojo v1.2.2 (4 suites) | 60 of 97 | 59 / 199 = 29.6% (23.7–36.3%); strict 53 / 199 = 26.6% (21.0–33.2%) | 0 / 171 = 0.0% (0.0–2.2%) | 33 / 60 = 55.0% (42.5–66.9%) |
| AppWorld (48 train/dev tasks) | 48 of 48 | 10 / 148 = 6.8% (3.7–12.0%) | 0 / 109 = 0.0% (0.0–3.4%) | 7 / 48 = 14.6% (7.2–27.2%) |

The pooled rates within a benchmark combine cells from the same task, which
are not independent, so their intervals are too narrow. Read them as
descriptive; the per-family tables below are the primary result.

The AgentDojo "strict" figure (post hoc) drops 6 `wrong_value` false accepts
that a manual reading found are not values the task specifies (see "The
`wrong_value` heuristic" below and `results/wrong_value_review.json`).

**Claim timing, reported separately (post hoc ordering finding).** On
AppWorld, when the task's last write is moved after the success claim
(`late_write`), the tests pass in 48 / 48 tasks (92.6–100%). The final end
state in these cells is identical to the reference, so this is not a wrong
end state and is not in the false-accept rate. It shows only that the tests
don't check when the claim was made. Note that a standard AppWorld episode
ends at the claim, so an agent running under the normal harness may not be
able to write after it; the finding matters for harnesses that keep the
environment open after the claim. AgentDojo ends the episode at the final
answer, so `late_write` was refused on all 60 tasks there.

## What the two graders miss

**AgentDojo** grades most tasks by checking that the expected record exists
(for example "some transaction has amount 200.29"). It rarely checks that
nothing else changed or that the record went to the right counterparty. So
it passes extra payments and messages to other parties (collateral write,
20 / 29), duplicates (20 / 49), and values the task stated exactly but the
check ignores. The workspace suite, which compares the full before/after
diff on many tasks, is much stricter (13 / 79 pooled) than banking
(25 / 41) and slack (21 / 66). Travel had 0 / 13, but only 6 of its 20 tasks
write anything.

**AppWorld** runs per-task unit tests on the final database. They caught
every missing write (0 / 39 partial, 0 / 48 none) and every collateral
write (0 / 20). Of the end-state faults, they passed some duplicate writes
(6 / 12) and wrong values (3 / 6 where the task names the value), and one
write to a different record (1 / 23). These cluster in a few task scenarios
(the 48 tasks are 16 scenarios with 3 variants each).

**False rejects.** Neither grader rejected any valid alternative in this
study. That result is narrow: here "valid alternative" means a different
trajectory that leaves an *identical* end state (repeated or unrelated
reads, reordered writes, a refused attempt before the right write, a wrong
write later overwritten). Valid solutions whose end state differs but is
still acceptable, such as another correct value or format, are where
graders are known to reject correct work, and this study does not generate
them. So 0% here is a lower bound on false rejects, not evidence that these
graders never reject valid work.

## Per-family tables

### agentdojo

97 tasks in the sample, 37 excluded (REFERENCE_HAS_NO_EFFECT: 37), 60 analyzed.

| Class | Family | Hits / n | Rate | Wilson 95% | Not counted |
|---|---|---|---|---|---|
| wrong | collateral write (`unauthorized_side_effect`) | 20 / 29 | 69.0% | 50.8–82.7% | candidates_exhausted 4, refused 27 |
| wrong | wrong record (`wrong_record`) | 4 / 29 | 13.8% | 5.5–30.6% | candidates_exhausted 4, refused 27 |
| wrong | wrong field value (`wrong_value`) | 13 / 19 | 68.4% | 46.0–84.6% | candidates_exhausted 11, refused 8 |
| unspecified | wrong field value, value not named in the task (`wrong_value (unspecified value)`) | 13 / 22 | 59.1% | 38.7–76.7% | reported only, not pooled |
| wrong | duplicate write (`duplicate`) | 20 / 49 | 40.8% | 28.2–54.8% | candidates_exhausted 4, degenerate 7 |
| wrong | missing write (some) (`partial_write`) | 0 / 13 | 0.0% | 0.0–22.8% | refused 47 |
| claim timing | write after the success claim (`late_write`) | 0 / 0 | n/a | 0.0–100.0% | reported only, not pooled |
| wrong | missing write (all) (`optimistic_success`) | 2 / 60 | 3.3% | 0.9–11.4% | - |
| valid alternative | repeated reads (`repeated_reads`) | 0 / 59 | 0.0% | 0.0–6.1% | refused 1 |
| valid alternative | unrelated reads (`foreign_reads`) | 0 / 60 | 0.0% | 0.0–6.0% | - |
| valid alternative | writes reordered (`reordered_writes`) | 0 / 9 | 0.0% | 0.0–29.9% | not_equivalent 4, refused 47 |
| valid alternative | refused attempt, then the right write (`refused_attempt`) | 0 / 25 | 0.0% | 0.0–13.3% | not_equivalent 10, refused 25 |
| valid alternative | wrong write, then corrected (`self_corrected_write`) | 0 / 18 | 0.0% | 0.0–17.6% | not_equivalent 34, refused 8 |

Pooled false accepts, end state wrong (descriptive): 59 / 199 (29.6%, 23.7–36.3%). Strict (post hoc, see wrong_value_review.json): 53 / 199 (26.6%, 21.0–33.2%). Pooled false rejects: 0 / 171 (0.0%, 0.0–2.2%). Tasks with at least one false accept: 33 / 60 (55.0%, 42.5–66.9%).

### appworld

48 tasks in the sample, 0 excluded (none), 48 analyzed.

| Class | Family | Hits / n | Rate | Wilson 95% | Not counted |
|---|---|---|---|---|---|
| wrong | collateral write (`unauthorized_side_effect`) | 0 / 20 | 0.0% | 0.0–16.1% | candidates_exhausted 12, degenerate 1, refused 15 |
| wrong | wrong record (`wrong_record`) | 1 / 23 | 4.3% | 0.8–21.0% | candidates_exhausted 10, refused 15 |
| wrong | wrong field value (`wrong_value`) | 3 / 6 | 50.0% | 18.8–81.2% | candidates_exhausted 9, refused 24 |
| unspecified | wrong field value, value not named in the task (`wrong_value (unspecified value)`) | 3 / 9 | 33.3% | 12.1–64.6% | reported only, not pooled |
| wrong | duplicate write (`duplicate`) | 6 / 12 | 50.0% | 25.4–74.6% | candidates_exhausted 27, degenerate 9 |
| wrong | missing write (some) (`partial_write`) | 0 / 39 | 0.0% | 0.0–9.0% | refused 9 |
| claim timing | write after the success claim (`late_write`) | 48 / 48 | 100.0% | 92.6–100.0% | reported only, not pooled |
| wrong | missing write (all) (`optimistic_success`) | 0 / 48 | 0.0% | 0.0–7.4% | - |
| valid alternative | repeated reads (`repeated_reads`) | 0 / 48 | 0.0% | 0.0–7.4% | - |
| valid alternative | unrelated reads (`foreign_reads`) | 0 / 0 | n/a | 0.0–100.0% | refused 48 |
| valid alternative | writes reordered (`reordered_writes`) | 0 / 7 | 0.0% | 0.0–35.4% | not_equivalent 32, refused 9 |
| valid alternative | refused attempt, then the right write (`refused_attempt`) | 0 / 39 | 0.0% | 0.0–9.0% | refused 9 |
| valid alternative | wrong write, then corrected (`self_corrected_write`) | 0 / 15 | 0.0% | 0.0–20.4% | not_equivalent 12, refused 21 |

Pooled false accepts, end state wrong (descriptive): 10 / 148 (6.8%, 3.7–12.0%). Strict (post hoc, see wrong_value_review.json): 10 / 148 (6.8%, 3.7–12.0%). Pooled false rejects: 0 / 109 (0.0%, 0.0–3.4%). Tasks with at least one false accept: 7 / 48 (14.6%, 7.2–27.2%).


The "strict" line under AppWorld equals the primary one: the manual reading
covered AgentDojo only. The 3 AppWorld `wrong_value` false accepts were read
privately and are all values the task names exactly.

AgentDojo by suite (end state wrong; tasks with at least one):

| Suite | Tasks analyzed / in suite | Pooled false accepts | Tasks with a false accept |
|---|---|---|---|
| banking | 11 / 16 | 25 / 41 = 61.0% (45.7–74.3%) | 9 / 11 = 81.8% (52.3–94.9%) |
| slack | 20 / 21 | 21 / 66 = 31.8% (21.9–43.8%) | 14 / 20 = 70.0% (48.1–85.5%) |
| travel | 6 / 20 | 0 / 13 = 0.0% (0.0–22.8%) | 0 / 6 = 0.0% (0.0–39.0%) |
| workspace | 23 / 40 | 13 / 79 = 16.5% (9.9–26.2%) | 10 / 23 = 43.5% (25.6–63.2%) |

How to read the "not counted" column: `refused` means the family's
precondition did not hold (for example `partial_write` needs two writes);
`degenerate` means the edit left the end state unchanged (an idempotent
duplicate); `candidates_exhausted` means every candidate edit was refused by
the environment; `not_equivalent` means the alternative's end state was not
identical to the reference, so it is not a valid alternative under this
protocol. None of these enter a rate.

## The `wrong_value` heuristic (post hoc reading)

The pre-registered rule counts a `wrong_value` cell only when the original
value appears as a whole word in the task instruction. Reading the 13
AgentDojo false accepts it admitted:

- 5 are values the task states exactly: the event title in workspace tasks
  9, 12, 20 and 21 ("Follow-up meeting", "Lunch"), and the message text in
  slack task 13. The edit appends one character; the grader does not check
  the title, or checks the message with a substring test.
- 2 are close paraphrases (workspace 15 and 18: an event title the
  instruction describes but does not quote).
- 6 match a common word, not a specified value (banking 4, 5, 6 and 15:
  transaction subjects; workspace 25: an email subject; workspace 29:
  free-form file content). Passing these is arguably correct.

Dropping those 6 gives the strict AgentDojo figure, 53 / 199. The
pre-registered row is unchanged above.

## Counterexamples

Every AgentDojo false accept is in `results/results.json`
(`counterexamples`) with its effect diff. AppWorld counterexamples are
described here by family only.

1. **Collateral write, AgentDojo banking `user_task_11`.** The task asks to
   send Apple the VAT difference. The edited trajectory also sends the same
   payment to an unrelated IBAN. The grader checks only that some
   transaction has amount 200.29, and passes.
2. **Wrong record, AgentDojo banking `user_task_11`.** The payment goes to an
   unrelated IBAN instead of Apple. Same check, same pass.
3. **Missing write, AgentDojo banking `user_task_5` and `user_task_6`.** All
   writes dropped; the agent only reads and answers. The grader passes
   because a matching record (a 50.00 payment to Spotify, a recurring
   Spotify transaction) already exists in the seed data, so the check is
   true before the agent does anything.
4. **Duplicate write, AgentDojo banking `user_task_0`.** The bill payment is
   sent twice. Pass.
5. **Wrong value, AgentDojo workspace `user_task_20`.** The task says to title
   the event 'Lunch'; the event is titled "Lunchx". Pass.
6. **Duplicate write, AppWorld.** A task that adds items to a list passes
   when one item is added twice.
7. **Wrong value, AppWorld.** A task that names an exact note for a set of
   payment requests passes when the note carries one extra character.
8. **Wrong record, AppWorld.** One of a task's payment writes is retargeted
   to a different user's record, and the tests pass.

No false rejects occurred, so there are no false-reject counterexamples.

## Exclusions

- AgentDojo: 37 of 97 tasks were excluded because the reference changes no
  state (answer-only tasks; this study is about writes). No reference was
  rejected by its own grader and none was non-reproducible.
- AppWorld: none of the 48.

## What could not be run, and why

- **tau-bench (MIT)** was the planned optional third benchmark. Its license
  was checked, but installing and running code from a fresh clone was
  blocked by the session's permission policy, so no cell exists
  (DEVIATIONS.md entry 2).
- **`late_write` on AgentDojo**: refused on all 60 tasks. AgentDojo ends
  the episode at the final answer, so no write can follow the claim.
- **`foreign_reads` on AppWorld**: refused on all 48 tasks. AppWorld reads
  need per-task access tokens, so another task's reads cannot be replayed.
- **Alternatives with a different but valid end state**: not generated (see
  "False rejects" above). Judging those needs a per-task specification of
  what is acceptable, which these benchmarks do not ship.
- **Benchmarks needing a browser or a VM** (OSWorld, WebArena, WorkArena):
  not attempted in this run.

## Limits

- The reference is one correct solution, not the only one. A false accept
  says the grader passed an end state that differs from the reference in the
  named way; the counterexamples above were read to confirm the difference
  matters, and the `wrong_value` reading shows where it may not.
- Edits are mechanical. They show what a policy could get credit for, not how
  often a real agent does it.
- AppWorld's 48 tasks are 16 scenarios with 3 variants each, so per-family
  results cluster by scenario.
- Benchmarks own their graders. These findings should go to the maintainers
  before any public use.

A draft write-up exists privately and is not published.

## Reproduce

```bash
cd packages/flow
# AgentDojo (agentdojo 0.1.35 in its own environment)
PYTHONPATH=.:../types python benchmark/grader_soundness/run.py run \
  --target openadapt_flow.verifier_audit.targets.agentdojo:make_target \
  --target-arg suite=banking --foreign-reads \
  --out benchmark/grader_soundness/results/agentdojo/banking
# AppWorld (appworld 0.1.3.post1 in its own environment; one task per call).
# Write the ledger outside the repository; summarize then writes only
# results/appworld/aggregate.json if the ledger directory is placed under
# results/appworld/ temporarily.
PYTHONPATH=.:../types python benchmark/grader_soundness/run.py run \
  --target openadapt_flow.verifier_audit.targets.appworld:make_target \
  --target-arg python=/path/to/appworld-venv/bin/python \
  --target-arg appworld_root=/path/to/appworld-root \
  --target-arg references=/path/to/references --tasks <task_id> --redact \
  --out /private/store/appworld/aa
python benchmark/grader_soundness/run.py summarize
python benchmark/grader_soundness/run.py tables
```

AppWorld references are the API-call logs of each task's shipped solution;
they are AppWorld derivatives and are not in this repository.
