# Benchmarks: method, numbers, and caveats

![OpenEMR: compiled replay vs computer-use agent, latency and cost](../benchmark/openemr/latency_cost.png)

## OpenEMR public demo (the lead result)

The lead result is on a real third-party app: the official OpenEMR public
demo (fake patients only, resets daily). We ran an 18-step add-patient-note
workflow both ways (log in, find a patient, scroll a dense dashboard, add
a note) with a distinct note value each run and the same OCR success
check on both arms: 20 compiled replays against 10 runs of a
claude-sonnet-5 computer-use agent, measured on 2026-07-08 from a
pre-v0.2.0 source checkout declaring openadapt-flow 0.1.0. Compiled went
19/20 at 39.2s (p50)
with zero model calls; the agent went 10/10 at 70.4s (p50), about $0.55
per run at list price ($5.52 total for the 10 runs, with prompt caching
and hard cost caps enforced in the harness). The corrected OCR check requires
the note in a saved Patient Messages row; it rejects one compiled run where the
note remained in the unsaved entry form. This is screen-row evidence, not a
system-of-record read. It is a shared public demo
that other users mutate and that resets daily, so it is not CI-reproducible,
and the sample is small. Correctness alone (no agent arm, 5/5 fresh browsers,
zero model calls, closed-loop scrolling) is in
[`showcase-openemr/FINDINGS.md`](showcase-openemr/FINDINGS.md).
Full numbers, methodology, and caveats:
[`../benchmark/openemr/BENCHMARK.md`](../benchmark/openemr/BENCHMARK.md).

## MockMed bundled fixture (the methodology anchor)

For a controlled, CI-reproducible comparison (the methodology anchor) we
ran the bundled MockMed task both ways on 2026-07-08, on the same
openadapt-flow 0.1.0 pre-v0.2.0 source build, with the same OCR
success check: 100 compiled replays against 20 runs of the same agent.
Both arms went 100 for 100 and 20 for 20, so on an app this simple
success rate does not separate them. Cost and latency do: a compiled
replay finishes in 4.9s (p50; 5.1s p95) with zero model calls, while the
agent takes 37.5s (p50; 43.4s p95). The measured agent sample cost about $0.27 per run at the model's
then-current list price; repeat-run figures are projections and exclude
authoring, maintenance, and infrastructure. Full
numbers, methodology, and caveats:
[`../benchmark/BENCHMARK.md`](../benchmark/BENCHMARK.md).

## 29-application public-web corpus (the breadth number)

The two comparisons above are one task each, chosen and rehearsed. The corpus
is the opposite: 29 public, no-authentication web applications, recorded once,
compiled, and replayed once against an unchanged UI with `grounder=None` and
zero model calls. All 29 recordings compiled. **17 replays reached a verified
success (58.6%), 10 halted safely, and 2 reported success while the external
oracle disagreed.** Nine of the ten halts were conservative identity refusals
and one was a failed postcondition. The two wrong actions were vacuous
successes after an external page blocked the demonstrated interaction, so
neither was a confirmed bad write, but the runtime reported success and the
independent oracle did not, which is the definition we hold ourselves to.

One observation per target is failure discovery, not a reliability rate. The
corpus covers public browser applications only: no authenticated enterprise
apps, no native desktop, no RDP or Citrix, no UI drift, no repeatability over
time. We picked the targets and we built the mechanism, so it is not
independent validation. Full method, failure taxonomy, and caveats:
[`../benchmark/reliability/RELIABILITY.md`](../benchmark/reliability/RELIABILITY.md);
machine-readable aggregate:
[`../benchmark/reliability/summary.json`](../benchmark/reliability/summary.json).

## Frappe lending reference environment

The stack also ships a pinned, containerized lending reference environment,
[`../benchmark/frappe_lending/README.md`](../benchmark/frappe_lending/README.md), with pinned
containers, a lockfile, and independent REST, SQL, and exact table-delta
verification of every write. In the model-free engineering matrix (compiled
and direct-API arms, baseline plus cosmetic drift, measured 2026-07-16 on
openadapt-flow 1.9.0), it delivered **12/12
correct rows with zero silent wrong writes, zero over-halts, and $0 model
cost**. A separate paid-agent run on 2026-07-21 on openadapt-flow 1.19.0 completed
6/6 correct writes (5/6 clean;
one post-write cost-cap over-halt) with zero silent incorrect successes. That
small-N run used a separately provisioned baseline, so it is engineering
evidence rather than a matched comparison or publication result. See the
[aggregate agent-arm report](../benchmark/agent_arm_verticals/README.md).

## EffectBench: the standalone SWER benchmark

The silent-wrong-effect result is also packaged as a standalone, versioned,
independently runnable benchmark — **EffectBench** — that a third party can
`pip install` and run against their own agent with pydantic as the only
dependency (no OpenAdapt codebase). It defines the Silent Wrong-Effect Rate
(SWER) metric, the fault taxonomy, the oracle contract, and a leaderboard /
submission format, and ships the public synthetic MockMed sample plus the
reference scorer. Spec: [`../benchmark/effectbench/SPEC.md`](../benchmark/effectbench/SPEC.md);
submission format: [`../benchmark/effectbench/LEADERBOARD.md`](../benchmark/effectbench/LEADERBOARD.md).
