# EffectBench Leaderboard & Submission Format

A leaderboard entry is a single, **fully reproducible** JSON document. It carries
the raw per-episode rows, the headline recomputed from them, and a
reproducibility manifest (benchmark version, task-pack fingerprint, pinned
dependency versions, seeds). Anyone can re-derive every headline from the raw
rows — the verifier does exactly that and **rejects a submission whose claimed
numbers do not match the recomputation**.

## How results are reported

Every submission MUST report SWER **jointly** with over-halt, task-success,
screen-success, and the success–effect gap, decomposed by
`(category × substrate)`. A SWER reported without its over-halt is not a valid
entry (a system reaches SWER 0 by halting on everything). See `SPEC.md` §1.

## Producing a submission

```bash
pip install effectbench

# The shipped baselines (a template + the pinned reference fixture):
python -m effectbench submission --baseline screen_only     --trials 10 > screen_only.json
python -m effectbench submission --baseline effect_verified --trials 10 > effect_verified.json
```

For your own system, implement `effectbench.adapter.SystemUnderTest` (one
method) and build the submission in Python:

```python
from effectbench import evaluate
from effectbench.leaderboard import build_submission

episodes = evaluate(MySUT(), trials=10)          # runs the MockMed anchor
doc = build_submission(
    system_name="my-agent",
    description="short description of the system under test",
    url="https://…",
    episodes=episodes,
    trials=10,
)
# write doc to my-agent.json
```

To score against a system of record **the benchmark authors did not build**,
implement a `effectbench.provider.BenchmarkProvider` (bring your own fixture +
independent oracle) and use `evaluate_provider(sut, provider, trials=…)` instead
of `evaluate`. See `README.md` and `SPEC.md` §5.2.

## Verifying a submission (reproduce it)

```bash
python -m effectbench score my-agent.json   # exit 0 iff the claims reproduce
```

`score` recomputes SWER / over-halt / task-success / screen-success from the raw
`episodes` rows and checks them against the claimed `results`, and checks that
the `pack_fingerprint` matches the benchmark's task pack. Any mismatch fails.

## Submission document shape

```json
{
  "effectbench_submission_version": 1,
  "system": { "name": "my-agent", "description": "…", "url": "…" },
  "reproducibility": {
    "effectbench_version": "1.0.0",
    "pack": "mockmed-anchor",
    "pack_fingerprint": "sha256:…",
    "trials_per_task": 10,
    "seeds": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    "python": "3.12.3",
    "platform": "…",
    "dependencies": { "pydantic": "2.x" }
  },
  "results": {
    "arm": "my-agent",
    "n_episodes": 90,
    "n_tasks": 9,
    "swer": { "numerator": 0, "denominator": 90, "rate": 0.0, "ci": { "lo": …, "hi": … } },
    "swer_wrong_write": { … },
    "swer_phantom": { … },
    "over_halt": { … },
    "task_success": { … },
    "screen_success": { … },
    "success_effect_gap": 0.0,
    "pass_hat_k": { "1": …, "2": …, "4": …, "8": … },
    "outcome_counts": { "success": …, "silent_wrong_effect": …, "over_halt": … },
    "cells": [ { "category": "C1_partial_save", "substrate": "web", "swer": { … }, … } ]
  },
  "episodes": [ { "episode_id": "…", "outcome": "…", "agent": { … }, "oracle": { … }, … } ]
}
```

The `episodes` array is the **single source of truth**: `results` is a
convenience projection the verifier recomputes and discards on mismatch.

## Reproducibility manifest

The `reproducibility` block pins everything needed to re-run:

- `effectbench_version` — the benchmark version (a result under a different
  **major** is not comparable; see `SPEC.md` §8).
- `pack` + `pack_fingerprint` — which task pack and its SHA-256 fingerprint, so a
  changed task set is detected.
- `trials_per_task` + `seeds` — trial `i` seeds its trial-unique payload from
  `i`; the default seed set is `range(trials)`.
- `python`, `platform`, `dependencies` — the environment.

## The reference fixture's pinned expected values

**These are not measured results.** Every row below is a **pinned expected value
of a deterministic synthetic fixture** (MockMed), produced by **OpenAdapt's own
arm** on **OpenAdapt's own fixture**. They exist as a regression anchor: the
fixture is hand-authored and deterministic, so these counts reproduce exactly on
every run, and `tests/test_reference.py` fails if a schema, classifier, judge, or
metrics change moves them. Do not cite them as an empirical finding or a
published headline. **No independent third-party system of record has been scored
yet.** The `BenchmarkProvider` interface exists so a third party *can* bring a
real system of record + its own independent oracle; authoring that oracle is the
real-world cost the benchmark abstracts away on the reference fixture only.

| system | file | SWER (pinned) | over-halt | notes |
|---|---|---|---|---|
| `screen_only` baseline | `results/reference.json` | **50/90 (55.6%)** | 0/90 | trusts the banner — the arm the benchmark indicts |
| `effect_verified` baseline | `results/reference.json` | **0/90 (0.0%)** | 0/90 | gates success on an independent record readback |
| **OpenAdapt compiler** (end-to-end) | `results/openadapt_reference.json` | *pending* | *pending* | the reference governed runtime, scored end-to-end by the sibling measurement (see below) |

### Where the measured end-to-end result lives

OpenAdapt's measured silent-wrong-effect result is
[`benchmark/effect_e2e/`](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/benchmark/effect_e2e/EFFECT_E2E.md)
in the engine repository, where every write goes through the real governed
replay path to an on-disk SQLite system of record, the verifier reads back over a
different HTTP verb/endpoint/connection, and the ground truth is a direct
read-only SQLite connection that bypasses the service and audits every table it
finds in `sqlite_master`. Its measured ladder, 90 runs per arm:

| arm | silent-wrong-effect rate |
|---|---|
| screen-verify (success banner) | **60.0%** (54/90) |
| effect-verify, one out-of-band REST record oracle | **10.0%** (9/90) |
| effect-verify, complete SQL read path | **0.0%** (0/90) |

The middle rung is the number a real deployment ships — one out-of-band record
oracle cuts undetected wrong effects from 75.0% to 12.5%. All nine residual
misses are the single `collateral_unaudited` class: a collateral write to a
surface the oracle's read path does not cover. The `0/90` arm reaches zero only
by widening the read path to every mutable surface.

### The OpenAdapt reference (sibling-agent artifact)

`results/openadapt_reference.json` is a **wired placeholder** for the reference
OpenAdapt result: a real `record → compile → replay` run scored through this
benchmark's oracle. It is produced by the sibling end-to-end measurement in
`OpenAdaptAI/openadapt-evals` (not by the in-benchmark `effect_verified`
baseline, which is the mechanism proxy). When that measurement lands, its
submission JSON replaces the placeholder and is verifiable with
`python -m effectbench score results/openadapt_reference.json`.
