<!-- DRAFT. Not for publication until the founder approves the text.
     Report findings to the AgentDojo and AppWorld maintainers first. -->

# When the grader says pass

Agent benchmarks report a success rate, and the success rate is only as
good as the grader behind it. Earlier audits found problems: OSWorld
checkers reject about 14% of valid solutions, and in an audit of SWE-bench
Verified failures, 59.4% involved flawed tests.

We asked a narrower question of two benchmarks that run locally: when the
grader says pass, did the intended change happen, and nothing else?

## Method

For each task we took the benchmark's own reference solution and made
mechanical edits to it: send a write to the wrong record, change a field,
add a write to an unrelated record, drop a write, send a write twice, or
make the last write after claiming success. We also made valid variants
that leave the same end state, such as extra reads or reordered writes.
We replayed each edit from a fresh start, read the end state directly from
the benchmark's database, and then asked the benchmark's grader. No model
was involved. The protocol was committed before the first run.

## Results

AgentDojo (60 tasks that write state, 4 suites): the grader passed 59 of
199 wrong end states, 29.6% (95% interval 23.7–36.3%). 33 of 60 tasks
(55%, 42.5–66.9%) had at least one wrong outcome that passed. Most checks
confirm that an expected record exists, but not that nothing else changed.
An extra payment to an unrelated account passed in 20 of 29 cases. In two
banking tasks the check is already true before the agent does anything.

AppWorld (48 tasks): the grader passed 58 of 196 wrong end states, 29.6%
(23.6–36.3%). 48 of those were the same issue. The tests read only the
final database, so a write made after the agent claimed success always
passes. Leaving that family out, 10 of 148 wrong end states passed, 6.8%
(3.7–12.0%), mostly duplicates and a payment note with one extra character.
AppWorld's tests caught every missing write and every write to an unrelated
record we made.

Neither grader rejected any of 280 valid variants (0%, 0.0–1.4%). Our
variants all leave the same end state as the reference, so this does not
cover valid solutions that reach a different but acceptable end state,
which is where most published false rejects come from.

## What it means

A pass from these graders means "the expected record is there". It does not
mean "only the expected change happened" or "the work was done when the
agent said it was". An agent trained or ranked on these rewards can get
credit for extra writes, duplicates and early claims.

The full tables, counterexamples, protocol and code are in the OpenAdapt
repository under `packages/flow/benchmark/grader_soundness/`.
