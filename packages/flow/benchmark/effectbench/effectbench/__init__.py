"""EffectBench -- the Silent Wrong-Effect Rate (SWER) benchmark.

A standalone, versioned, independently runnable benchmark for silent
wrong-effect failures: cases where an automation agent reports (or a screen
renders) success while the independent system of record is wrong or empty. Its
headline metric is the **Silent Wrong-Effect Rate (SWER)**, reported jointly
with over-halt, task success, and the success-effect gap.

Install this package alone (pydantic is the only dependency) and run it against
YOUR agent/system by implementing :class:`~effectbench.adapter.SystemUnderTest`.
The full specification is in ``SPEC.md``; the submission format is in
``LEADERBOARD.md``.

Quick start::

    from effectbench import evaluate, summarize
    from effectbench.adapter import ScreenOnlySUT

    episodes = evaluate(ScreenOnlySUT(), trials=10)
    print(summarize(episodes, arm="screen_only").swer.rate)
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from effectbench.adapter import (
    EffectVerifiedSUT,
    EnvHandle,
    ScreenOnlySUT,
    SystemUnderTest,
)
from effectbench.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    EffectVerifier,
    ValueExpr,
    Verdict,
)
from effectbench.metrics import BenchmarkSummary, RateEstimate, summarize
from effectbench.oracle import (
    CompoundSnapshotOracle,
    RecordSnapshotOracle,
    TrueEffectState,
    classify_outcome,
    combine_true_states,
    effect_state,
    score_episode,
)
from effectbench.provider import BenchmarkProvider, EpisodeSetup
from effectbench.runner import (
    MockMedProvider,
    evaluate,
    evaluate_provider,
    run_episode,
    run_setup,
)
from effectbench.schema import (
    AgentReport,
    DivergenceCategory,
    EpisodeRecord,
    OracleSpec,
    OutcomeLabel,
    Substrate,
    SwerVariant,
    TaskSpec,
)


def _read_version() -> str:
    # Source checkouts keep VERSION beside the importable package. Wheels do
    # not carry that repository-level file, so installed distributions must use
    # their authoritative package metadata instead of silently reporting
    # ``0.0.0``.
    candidate = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return candidate.read_text(encoding="utf-8").strip()
    except OSError:
        try:
            return version("effectbench")
        except PackageNotFoundError:  # pragma: no cover - malformed install
            return "0.0.0"


__version__ = _read_version()

__all__ = [
    "__version__",
    # metric + taxonomy
    "summarize",
    "BenchmarkSummary",
    "RateEstimate",
    "OutcomeLabel",
    "SwerVariant",
    "DivergenceCategory",
    "Substrate",
    # schema
    "TaskSpec",
    "OracleSpec",
    "AgentReport",
    "EpisodeRecord",
    # oracle / scorer
    "classify_outcome",
    "score_episode",
    "effect_state",
    "combine_true_states",
    "TrueEffectState",
    "RecordSnapshotOracle",
    "CompoundSnapshotOracle",
    # effect mechanism
    "Effect",
    "EffectKind",
    "EffectState",
    "EffectVerdict",
    "EffectVerifier",
    "ValueExpr",
    "Verdict",
    # adapter + runner
    "SystemUnderTest",
    "EnvHandle",
    "ScreenOnlySUT",
    "EffectVerifiedSUT",
    "evaluate",
    "run_episode",
    # pluggable external system-of-record + oracle
    "BenchmarkProvider",
    "EpisodeSetup",
    "MockMedProvider",
    "evaluate_provider",
    "run_setup",
]
