# EffectBench

**The Silent Wrong-Effect Rate (SWER) benchmark for automation agents.**

EffectBench measures how often an agent **reports or renders success while the
independent system of record is wrong or empty** — the failure that aggregate
task-success benchmarks miss because they score the screen or the agent's own
self-report, exactly the witness a partial save, a duplicate, an optimistic-UI
reject, a lost update, or a wrong-record write all satisfy.

- **Headline metric:** SWER = `silent-wrong-effect episodes / N`, reported
  jointly with over-halt, task success, and the success–effect gap.
- **Standalone:** installs with **pydantic as its only dependency** — no
  OpenAdapt codebase required.
- **Versioned:** the contract is `SPEC.md` + `VERSION`; the submission format is
  `LEADERBOARD.md`.

This is the standalone, independently runnable packaging of the SWER result. The
full specification is in [`SPEC.md`](SPEC.md).

## Install

```bash
pip install effectbench          # from a release, or:
pip install ./benchmark/effectbench   # from this repository subtree
```

## Run the reference (no agent needed)

```bash
python -m effectbench reference
```

```
screen_only     SWER : 50/90 = 55.6%  (wrong-write 40, phantom 10)
effect_verified SWER :  0/90 =  0.0%
transactional silently mishandled by screen-only: 5/7
```

The two shipped baselines — `screen_only` (trusts the banner) and
`effect_verified` (gates success on an independent record readback) — reproduce
the published result and are a template for your own integration.

## Run it against YOUR system

Implement one method — `effectbench.adapter.SystemUnderTest.run_task` — that
attempts each task's goal against the reference (synthetic MockMed) system of
record and returns your system's own (untrusted) self-report. EffectBench reads
the true effect through an independent oracle you cannot reach, and computes your
SWER.

```python
from typing import Mapping
from effectbench import evaluate, summarize
from effectbench.adapter import EnvHandle
from effectbench.schema import AgentReport, TaskSpec


class MySUT:
    name = "my-agent"

    def run_task(self, task: TaskSpec, env: EnvHandle, *,
                 params: Mapping[str, str]) -> AgentReport:
        # 1. Drive the reference system toward task.goal (intent, not steps).
        obs = env.attempt_intended_action(params)
        # 2. Decide success HOWEVER your system does. If you have your own
        #    record-readback capability, use it (env.product_effect_verifier());
        #    if you only trust the screen, you'll surface a SWER.
        return AgentReport(reported_success=obs.banner_saved)


episodes = evaluate(MySUT(), trials=10)
summary = summarize(episodes, arm="my-agent")
print(summary.swer.rate, summary.over_halt.rate)   # report BOTH, always
```

## The adapter contract

```python
@runtime_checkable
class SystemUnderTest(Protocol):
    name: str
    def run_task(self, task: TaskSpec, env: EnvHandle,
                 *, params: Mapping[str, str]) -> AgentReport: ...
```

- `task.goal` is the natural-language intent, **never a step list** (fairness).
- `env` drives the reference system and reads back only the **screen banner** —
  the oracle's read path is never exposed to your system.
- `env.product_effect_verifier()` is your system's OWN optional record-readback
  capability (a distinct object from the harness oracle) — the honest way to
  refuse to trust the screen.
- The return `AgentReport.reported_success` is the untrusted witness; the oracle,
  not this value, decides the outcome.

## Bring your OWN system of record + oracle

The section above scores YOUR AGENT against OUR reference fixture (synthetic
MockMed). MockMed ships its ground-truth oracle as a **freebie** - the benchmark
authored a cheap, correct, independent record-readback oracle for its own
synthetic app. On a **real** legacy system of record, authoring that oracle is
the whole problem (SPEC §2.2, §5.2); the benchmark does not do it for you.

To score EffectBench against a system of record **the benchmark authors did not
build**, implement a `effectbench.provider.BenchmarkProvider`. It supplies (a)
your environment/fixture + tasks and (b) your OWN independent ground-truth
oracle, and runs through the same runner and the same two reference baselines:

```python
from effectbench import EpisodeSetup, evaluate_provider
from effectbench.adapter import EffectVerifiedSUT, ScreenOnlySUT

class MyProvider:
    name = "my-system"
    def tasks(self):                      # opaque task handles
        ...
    def provision(self, task, trial) -> EpisodeSetup:
        # build a FRESH system-of-record state, an arm-facing env, and an
        # INDEPENDENT oracle the SUT can never reach; return an EpisodeSetup.
        ...

episodes = evaluate_provider(ScreenOnlySUT(), MyProvider(), trials=10)
```

If your provider does **not** author a product verifier for the SUT
(`EnvHandle.product_effect_verifier()` returns `None`), `EffectVerifiedSUT`
**fails safe** - it halts rather than getting a correct oracle for free. That is
the point: the freebie only exists on the built-in synthetic fixture.

> **Honest status.** The public reusable surface currently ships **one** synthetic
> reference fixture (MockMed). **No independent third-party system of record has
> been scored yet** - the two baselines (`screen_only`, `effect_verified`) are
> OpenAdapt's own arms on OpenAdapt's own fixture. This interface exists so a
> third party *can* bring a real system of record + oracle; authoring that oracle
> is the real-world cost the benchmark abstracts away on the reference fixture
> only.

## Submit a result

```bash
python -m effectbench submission --baseline effect_verified --trials 10 > sub.json
python -m effectbench score sub.json     # verify it reproduces (exit 0)
```

See [`LEADERBOARD.md`](LEADERBOARD.md) for the submission format and the
reproducibility manifest.

## What's in the package

| module | responsibility |
|---|---|
| `effectbench.schema` | the episode data contract + the outcome/fault taxonomy |
| `effectbench.effect` | the typed `Effect` contract mechanism (system-of-record kinds) |
| `effectbench.judge` | the substrate-independent effect judge |
| `effectbench.oracle` | the classifier + `score_episode` + the snapshot oracle |
| `effectbench.metrics` | `summarize` → SWER + co-metrics with Wilson / bootstrap CIs |
| `effectbench.adapter` | the `SystemUnderTest` interface + two reference baselines |
| `effectbench.provider` | the `BenchmarkProvider` seam - bring your OWN system of record + oracle |
| `effectbench.fixtures.mockmed` | the public **synthetic**, **reference-only** MockMed system of record |
| `effectbench.tasks.mockmed` | the public synthetic task pack (the anchor suite) |
| `effectbench.runner` | `evaluate(sut, trials=…)` and `evaluate_provider(sut, provider, …)` → episode rows |
| `effectbench.reference` | the reference result (regression anchor) |
| `effectbench.leaderboard` | submission build + reproducibility manifest + verifier |

## Source-availability boundary

This package ships the **mechanism + metric + a synthetic sample + the reference
scorer** and nothing else. It contains none of the crown-jewel artifacts (grown
hardening corpus, tuned adversary params, deployment-derived thresholds,
per-system-of-record oracle recipes, real-EMR datasets). `tests/test_boundary.py`
enforces that. See `SPEC.md` §7.

## Run the tests

```bash
pip install ./benchmark/effectbench[test]
python -m pytest benchmark/effectbench/tests -q
```

## License

MIT. See `LICENSE`. All fixture data is synthetic.
