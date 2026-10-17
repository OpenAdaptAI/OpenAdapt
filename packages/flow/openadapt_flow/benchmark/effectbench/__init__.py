"""EffectBench — the Silent Wrong-Effect benchmark foundation.

This package is the **stable contract** two downstream efforts build on:

- the *multi-baseline runner adapter* (drives each agent arm through a common
  backend and records an :class:`EpisodeRecord` per trial), and
- the *task pack* (authors :class:`TaskSpec` + :class:`OracleSpec` per task).

It generalizes the ``benchmark/fault_model`` DB-state oracle (a MockMed-only
``GET /api/db`` ground-truth read + ``classify()``) and the
``benchmark/silent_wrong_action`` proto-SWER rate (55.6% wrong-by-screen ->
0.0% wrong-by-effect over 90 runs) into a substrate-agnostic, statistically
reportable benchmark whose headline metric is the **Silent Wrong-Effect Rate
(SWER)**: the fraction of episodes where the agent reports/renders success
while an *independent system-of-record oracle* disagrees.

Three pieces, each a submodule:

- :mod:`.schema` — the episode schema (pydantic): :class:`TaskSpec`,
  :class:`OracleSpec`, :class:`EpisodeRecord`, :class:`ModelCall`,
  :class:`AgentReport`, and the :class:`OutcomeLabel` taxonomy.
- :mod:`.oracle` — the substrate-agnostic effect-oracle harness:
  :func:`classify_outcome` (the taxonomy decision, generalized from
  ``fault_model.classify``), :func:`score_episode`, :class:`RecordSnapshotOracle`,
  and the four concrete oracles re-exported from
  :mod:`openadapt_flow.runtime.effects` (SQL / REST / FHIR / file). Every
  oracle reads the TRUE effect independently of the screen and is authored to
  be non-gameable (pre/post SoR state, never the agent's self-report).
- :mod:`.metrics` — SWER (+ wrong-write / phantom split), over-halt, task
  success, the **success-effect gap**, cost/latency, ``pass^k``, and Wilson /
  bootstrap confidence intervals, all decomposed by category x substrate.

The design doc is ``.private/benchmark_design_2026_07_20.md``; the operator
contract is ``openadapt_flow/benchmark/effectbench/README.md``.
"""

from openadapt_flow.benchmark.effectbench.metrics import (  # noqa: F401
    BenchmarkSummary,
    CellSummary,
    Interval,
    RateEstimate,
    bootstrap_ci,
    pass_hat_k,
    summarize,
    wilson_interval,
)
from openadapt_flow.benchmark.effectbench.oracle import (  # noqa: F401
    RecordSnapshotOracle,
    TrueEffectState,
    classify_outcome,
    combine_true_states,
    effect_state,
    oracle_verdict_of,
    score_episode,
)
from openadapt_flow.benchmark.effectbench.schema import (  # noqa: F401
    AgentReport,
    DivergenceCategory,
    EpisodeRecord,
    ModelCall,
    OracleSpec,
    OracleVerdict,
    OutcomeLabel,
    Substrate,
    SwerVariant,
    TaskSpec,
)

# The four concrete oracle substrates are the runtime effect verifiers — an
# EffectBench oracle IS an ``EffectVerifier`` (structural: ``substrate`` +
# ``capture_pre_state`` + ``verify``). Re-exported here so a task pack imports
# the whole oracle surface from one place.
from openadapt_flow.runtime.effects import (  # noqa: F401
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    EffectVerifier,
    FhirEffectVerifier,
    FileArrivalVerifier,
    RestRecordVerifier,
    SqlRecordVerifier,
    ValueExpr,
    Verdict,
)

__all__ = [
    # schema
    "TaskSpec",
    "OracleSpec",
    "EpisodeRecord",
    "ModelCall",
    "AgentReport",
    "OracleVerdict",
    "OutcomeLabel",
    "SwerVariant",
    "Substrate",
    "DivergenceCategory",
    # oracle harness
    "classify_outcome",
    "combine_true_states",
    "effect_state",
    "oracle_verdict_of",
    "score_episode",
    "RecordSnapshotOracle",
    "TrueEffectState",
    # metrics
    "summarize",
    "BenchmarkSummary",
    "CellSummary",
    "RateEstimate",
    "Interval",
    "wilson_interval",
    "bootstrap_ci",
    "pass_hat_k",
    # re-exported effect contract + concrete oracle substrates
    "Effect",
    "EffectKind",
    "EffectState",
    "EffectVerdict",
    "EffectVerifier",
    "ValueExpr",
    "Verdict",
    "RestRecordVerifier",
    "SqlRecordVerifier",
    "FhirEffectVerifier",
    "FileArrivalVerifier",
]
