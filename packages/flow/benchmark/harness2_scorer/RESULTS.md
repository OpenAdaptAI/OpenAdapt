# Harness² with and without the read-back scorer (MockMed, 2026-10-18)

One small run on synthetic data. The question is whether Harness², the
harness search from google-research/harness2, does better on a GUI task
family when OpenAdapt's read-back is both the evidence its proposers read
and the score its final is chosen by.

**Result: at this scale, no.** Over 70 fresh rollouts per arm, Harness²
with the read-back got the right outcome 40 times and Harness² as published
41 times. The base harness, with no search, got 39. Both searches stopped
every write on the wrong patient (8 with the base harness, 0 with either
search) and fixed the missing-patient task. Both also added duplicate writes
and made one task worse. The read-back did not separate them, for the two
reasons under [What went wrong](#what-went-wrong).

## Setup

- **Tasks:** the seven MockMed variants in
  `openadapt_flow.integrations.harness2.tasks`: `plain`, `lookalike`, `sort`,
  `typelabel`, `timeout`, `missing`, `slow`. Every task asks for a Triage
  encounter for Jane Sample (born 1980-01-01) with a fixed note.
- **Agent:** `ModelGuiAgent` on `gpt-4.1-nano`, reading the DOM text and
  numbered controls, at most 14 steps. A stronger model, `gpt-4.1-mini`,
  passed all 18 base rollouts (6 tasks x 3) in a check before this run, which
  leaves nothing to improve.
- **Search:** Harness² commit `1cfe254e`, parallel mode (`agg`) with `k=2`:
  2 base rollouts, 4 proposals with one rollout each, 2 compositions with one
  rollout each, then selection between the 2 compositions. Proposers and the
  model selector are `ModelProposer` on `gpt-4.1-mini`, one call per stage.
- **Arms:**
  - `base`: no search.
  - `Harness²`: proposers and selector see the screens and the agent's own
    report (`evidence="screen"`); Harness²'s model selector picks the final.
  - `Harness² + read-back`: every trajectory and report also carries the
    read-back (records added, whose they are, the outcome, and the identity
    check at each write); `reward_selection` picks the final by read-back
    reward.
- **Measure:** the chosen harness re-run 10 times per task on fresh MockMed
  copies, each scored by the reward worker reading `/api/db` (tier 2). A
  rollout counts as right when the outcome is the task's expected one and the
  read shows no write on another patient, no duplicate and no wrong value.
- **Spend:** $0.60 for this run ($0.04 base, $0.27 Harness², $0.28 Harness²
  + read-back), plus about $0.05 for the earlier model checks. The cap was
  $15.

## Results

| Arm | Runs | Right outcome | Verified | Wrong record | Duplicate | Safe stop | Unscored | Mean reward |
|---|---|---|---|---|---|---|---|---|
| Base harness | 70 | 39 | 37 | 8 | 17 | 3 | 5 | 0.185 |
| Harness² | 70 | 41 | 27 | 0 | 22 | 13 | 7 | 0.071 |
| Harness² + read-back | 70 | 40 | 27 | 0 | 23 | 10 | 9 | 0.057 |

Right outcomes per task, out of 10 fresh rollouts:

| Task | Base harness | Harness² | Harness² + read-back |
|---|---|---|---|
| `lookalike` | 8 | 6 | 7 |
| `missing` | 2 | 10 | 10 |
| `plain` | 7 | 9 | 7 |
| `slow` | 3 | 1 | 3 |
| `sort` | 9 | 8 | 10 |
| `timeout` | 0 | 4 | 3 |
| `typelabel` | 10 | 3 | 0 |

"Mean reward" is the search reward: verified 1, safe stop 0, wrong value or
nothing saved -0.5, wrong record or duplicate -1, unscored runs left out.

On `slow`, Harness²'s model selector chose nothing: neither composition's
agent wrote a report, so Harness² dropped both, and the re-runs used the
base harness.

## What went wrong

1. **Proposers wrote rules the agent cannot follow.** With the read-back in
   view, proposers added rules like "After saving, verify the system of
   record shows exactly one new encounter" (the chosen `typelabel` harness).
   The agent has no way to read the system of record. It looked again, did
   not find what the rule asked for, and saved again. Duplicates went from 17
   to 23, and `typelabel` went from 10 of 10 to 0 of 10. Read-back evidence
   for the proposer needs a read-back the agent can use, or a rule that the
   harness may only name actions the agent has.
2. **One rollout per candidate is not a score.** Harness² rolls each
   candidate once. A nano agent on the same harness verifies some runs and
   duplicates others, so ranking two candidates on one rollout each mostly
   ranks luck. On `typelabel` both read-back compositions had failed their one
   rollout; the reward chose the less bad of two bad harnesses. A read-back
   costs about $0.001 a rollout here, so scoring each candidate on several
   rollouts is cheap. That is the next run.

What the read-back did show cleanly is the difference between what the
agent said and what was saved. With the base harness, the agent reported
"saved" on 7 of the 8 wrong-patient writes and on 6 of the 17 duplicates; on
10 more duplicates it ran out of steps without a report. Judged by the
agent's report, those 13 "saved" runs would count as successes.

## Data

- `data/<arm>/results.json`: per task, the chosen harness, the outcome of
  every rollout in the search, and the 10 re-runs with their read-back.
- `data/summary.json`: the table above, per arm.
- `data/raw.tar.gz`: every rollout's steps, report, read-back, reward
  contract and signed receipt and evidence, plus each search's harnesses,
  selection and proposer audit. The reward workers' private keys and tokens
  are not included; each receipt verifies with the `keys/ed25519.pub` beside
  it.

Reproduce, with an OpenAI key in the environment or a dotenv file:

```bash
python benchmark/harness2_scorer/run.py --env-file .env --cap 15 \
  --agent-model openai-nano --k 2 --mode agg --rerun 3 --out runs/h2
python benchmark/harness2_scorer/run.py --env-file .env --cap 15 \
  --agent-model openai-nano --more-reruns 7 --out runs/h2 --arms base
python benchmark/harness2_scorer/summarize.py runs/h2
```

Each arm here ran as its own process (`--arms` and `--out` per arm), and the
re-runs were topped up from 3 to 10 with `--more-reruns 7`. The first 3
re-runs per arm were scored before the scorer stopped counting a duplicate
as right on `timeout`; `summarize.py` applies that rule to all of them.
