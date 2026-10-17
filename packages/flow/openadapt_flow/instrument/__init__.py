"""Competitor-drift instrument: point our EffectVerifier at ANY external agent.

The self-directed silent-wrong-action benchmark
(``openadapt_flow.benchmark.silent_wrong_action``, #67) measures how often our
OWN runtime lets a wrong / absent / duplicate business effect land while
reporting success. This package generalizes that measurement to an *arbitrary
external computer-use agent*: given any agent behind the
:class:`ExternalAgentAdapter` seam, drive it through the MockMed transactional
fault suite against ``mockmed.fault_server``, read the resulting system of
record with the #63 :class:`~openadapt_flow.runtime.effects.RestRecordVerifier`,
and compute that agent's **silent-wrong-action rate** — output anonymized by
architecture class (``Tool A`` / ``Tool B`` / ...), never a vendor name.

This module is the HARNESS only. It ships no concrete adapter for any real
external product, makes no paid API / model calls, and names no vendor. The
only bundled adapter is a deterministic, $0, offline STUB that proves the
harness measures the rate correctly end to end. Wiring a real (cost-capped)
adapter and running it against a real competitor is a separate, explicit,
user-gated step.
"""

from openadapt_flow.instrument.competitor_drift import (  # noqa: F401
    AgentRunResult,
    ArchitectureClassError,
    CostGuard,
    DriftTask,
    ExternalAgentAdapter,
    InstrumentReport,
    InstrumentRunRow,
    StubExternalAgentAdapter,
    assert_anonymized,
    default_tasks,
    ensure_architecture_class,
    run_instrument,
)

__all__ = [
    "AgentRunResult",
    "ArchitectureClassError",
    "CostGuard",
    "DriftTask",
    "ExternalAgentAdapter",
    "InstrumentReport",
    "InstrumentRunRow",
    "StubExternalAgentAdapter",
    "assert_anonymized",
    "default_tasks",
    "ensure_architecture_class",
    "run_instrument",
]
