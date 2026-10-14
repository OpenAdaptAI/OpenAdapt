"""Evaluate openadapt-flow (the demonstration compiler) on WAA.

Two eval modes, both scored by WAA's own task verifier:

- **demonstrate-then-replay** (:mod:`openadapt_evals.flow.replay_runner`) --
  compile ONE demonstration into a bundle and replay it via
  :class:`openadapt_flow.backends.windows_backend.WindowsBackend` against the
  WAA in-guest server (~0 model calls). The paradigm-correct eval for a
  demonstration compiler.
- **hybrid-as-agent** (:mod:`openadapt_evals.flow.hybrid_agent`) -- a
  ``BenchmarkAgent`` the existing WAA runner can drive: compiled replay first,
  computer-use agent fallback only on a detected halt. Directly comparable to a
  pure agent baseline on the same tasks.

Cost estimation + hard guardrails live in :mod:`openadapt_evals.flow.cost`
(stdlib-only, importable without the ``flow`` extra or a VM).

Submodules are imported lazily (PEP 562) so importing this package never pulls
in ``openadapt_flow`` or any heavy dependency -- light CI imports it freely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    # cost
    "estimate_flow_waa_cost",
    "FlowRunCostEstimate",
    "SpendLedger",
    "CostGuardConfig",
    "MODELS",
    # replay runner
    "FlowTask",
    "PerTaskReplayMetrics",
    "run_demonstrate_then_replay",
    "aggregate_replay_metrics",
    # hybrid agent
    "HybridFlowAgent",
    # parallels local-VM environment ($0)
    "ParallelsConfig",
    "ParallelsSession",
    "ParallelsTask",
    "parallels_enabled",
    "builtin_tasks",
    "run_parallels_replay",
]

_EXPORTS = {
    "estimate_flow_waa_cost": ("openadapt_evals.flow.cost", "estimate_flow_waa_cost"),
    "FlowRunCostEstimate": ("openadapt_evals.flow.cost", "FlowRunCostEstimate"),
    "SpendLedger": ("openadapt_evals.flow.cost", "SpendLedger"),
    "CostGuardConfig": ("openadapt_evals.flow.cost", "CostGuardConfig"),
    "MODELS": ("openadapt_evals.flow.cost", "MODELS"),
    "FlowTask": ("openadapt_evals.flow.replay_runner", "FlowTask"),
    "PerTaskReplayMetrics": ("openadapt_evals.flow.replay_runner", "PerTaskReplayMetrics"),
    "run_demonstrate_then_replay": (
        "openadapt_evals.flow.replay_runner",
        "run_demonstrate_then_replay",
    ),
    "aggregate_replay_metrics": (
        "openadapt_evals.flow.replay_runner",
        "aggregate_replay_metrics",
    ),
    "HybridFlowAgent": ("openadapt_evals.flow.hybrid_agent", "HybridFlowAgent"),
    "ParallelsConfig": ("openadapt_evals.flow.parallels_env", "ParallelsConfig"),
    "ParallelsSession": ("openadapt_evals.flow.parallels_env", "ParallelsSession"),
    "ParallelsTask": ("openadapt_evals.flow.parallels_env", "ParallelsTask"),
    "parallels_enabled": ("openadapt_evals.flow.parallels_env", "parallels_enabled"),
    "builtin_tasks": ("openadapt_evals.flow.parallels_env", "builtin_tasks"),
    "run_parallels_replay": ("openadapt_evals.flow.parallels_env", "run_parallels_replay"),
}

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_evals.flow.cost import (
        MODELS,
        CostGuardConfig,
        FlowRunCostEstimate,
        SpendLedger,
        estimate_flow_waa_cost,
    )
    from openadapt_evals.flow.hybrid_agent import HybridFlowAgent
    from openadapt_evals.flow.parallels_env import (
        ParallelsConfig,
        ParallelsSession,
        ParallelsTask,
        builtin_tasks,
        parallels_enabled,
        run_parallels_replay,
    )
    from openadapt_evals.flow.replay_runner import (
        FlowTask,
        PerTaskReplayMetrics,
        aggregate_replay_metrics,
        run_demonstrate_then_replay,
    )


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    return getattr(module, target[1])


def __dir__() -> list[str]:
    return sorted(__all__)
