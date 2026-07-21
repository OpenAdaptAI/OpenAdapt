# Compile-reliability study — bounded aggregate

This public evidence summary reports one record → compile → unchanged-UI replay
observation for each of 29 public, no-authentication web applications. Replay
used the deterministic runtime with `grounder=None`, so the study made no model
calls. The generic study harness remains public; the curated target recipes and
raw per-target results are evaluation data and are maintained privately.

## Aggregate result

- Demonstrations recorded: **29/29**
- Bundles compiled: **29/29**
- Independently verified replay successes: **17/29** (**58.6%**)
- Safe halts: **10/29**
- Wrong actions reported as success: **2/29**
- False halts: **0/29**
- Crashes: **0/29**
- Model calls: **0**

The two wrong actions were vacuous successes after an external page prevented
the demonstrated interaction; neither was an independently confirmed write.
They still count as wrong actions because the runtime reported success while
the independent oracle disagreed. The dominant failure class was conservative
identity refusal (9 runs); one further run failed a postcondition.

## Scope and caveats

This is a small, single-observation-per-target study, not a production
reliability estimate. It covers public browser applications only. It does not
measure authenticated enterprise applications, native desktop applications,
RDP/Citrix sessions, UI drift, or repeatability across time. The targets were
selected and the mechanism was developed by the same team, so the evidence is
not independent validation.

The machine-readable aggregate is in [`summary.json`](summary.json). Product
claims must retain the task, environment, run count, oracle, failure taxonomy,
and caveats above; the aggregate must not be presented as an SLA or as evidence
for an arbitrary workflow.

The repository-wide [source and evaluation boundary](../../docs/SOURCE_BOUNDARY.md)
explains why the generic harness and this bounded aggregate remain public while
successor target recipes and raw per-target evaluation rows remain private.
