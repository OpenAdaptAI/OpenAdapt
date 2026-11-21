"""OpenAdapt Transactional Commit Layer (tx) 50k-row / 20-worker ACID benchmark suite."""

from __future__ import annotations

from .baselines import (
    ArmExecutionResult,
    BenchmarkArm,
    LegacyWholeTableContractArm,
    NaiveAgent2xxArm,
    ReferenceTxArm,
)
from .runner import (
    AppBenchmarkReport,
    ArmMetricsSummary,
    BenchmarkSuiteReport,
    build_tx_arm,
    render_markdown_report,
    run_tx_acid_benchmark,
)
from .servers import (
    ALL_BENCHMARK_FAULTS,
    ALL_WRITE_FAULTS,
    STATE_BIJECTION_MAP,
    BackgroundTrafficGenerator,
    BaseSorServer,
    ClaimsLedgerServer,
    ClinicalChartServer,
    FaultConfig,
    FaultInjectingProxy,
    FaultKind,
    ReplaceTaskServer,
    normalize_iso_date_utc,
    normalize_phone_e164,
    normalize_state,
    normalize_title_words,
    normalize_upper,
)

__all__ = [
    "ALL_BENCHMARK_FAULTS",
    "ALL_WRITE_FAULTS",
    "AppBenchmarkReport",
    "ArmExecutionResult",
    "ArmMetricsSummary",
    "BackgroundTrafficGenerator",
    "BaseSorServer",
    "BenchmarkArm",
    "BenchmarkSuiteReport",
    "ClaimsLedgerServer",
    "ClinicalChartServer",
    "FaultConfig",
    "FaultInjectingProxy",
    "FaultKind",
    "LegacyWholeTableContractArm",
    "NaiveAgent2xxArm",
    "ReferenceTxArm",
    "ReplaceTaskServer",
    "STATE_BIJECTION_MAP",
    "build_tx_arm",
    "normalize_iso_date_utc",
    "normalize_phone_e164",
    "normalize_state",
    "normalize_title_words",
    "normalize_upper",
    "render_markdown_report",
    "run_tx_acid_benchmark",
]
