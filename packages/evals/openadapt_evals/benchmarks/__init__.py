"""Benchmark integration for openadapt-evals.

This module provides interfaces and utilities for evaluating GUI agents
on standardized benchmarks like Windows Agent Arena (WAA), OSWorld,
WebArena, and others.

NOTE: The canonical locations for agents and adapters are now:
    - openadapt_evals.agents (BenchmarkAgent, ApiAgent, etc.)
    - openadapt_evals.adapters (BenchmarkAdapter, WAAAdapter, etc.)

This module re-exports from those locations for backward compatibility.

Core classes:
    - BenchmarkAdapter: Abstract interface for benchmark integration
    - BenchmarkAgent: Abstract interface for agents to be evaluated
    - BenchmarkTask, BenchmarkObservation, BenchmarkAction: Data classes

Agent implementations:
    - ScriptedAgent: Follows predefined action sequence
    - RandomAgent: Takes random actions (baseline)
    - SmartMockAgent: Designed to pass mock adapter tests
    - ApiAgent: Uses Claude/GPT APIs directly

Evaluation:
    - evaluate_agent_on_benchmark: Run agent on benchmark tasks
    - compute_metrics: Compute aggregate metrics from results

Example:
    ```python
    from openadapt_evals.benchmarks import (
        BenchmarkAdapter,
        BenchmarkAgent,
        WAAMockAdapter,
        SmartMockAgent,
        evaluate_agent_on_benchmark,
        compute_metrics,
    )

    # Create adapter for specific benchmark (mock for testing)
    adapter = WAAMockAdapter(num_tasks=10)

    # Create agent
    agent = SmartMockAgent()

    # Run evaluation
    results = evaluate_agent_on_benchmark(agent, adapter, max_steps=15)

    # Compute metrics
    metrics = compute_metrics(results)
    print(f"Success rate: {metrics['success_rate']:.1%}")
    ```
"""

# Re-exports are resolved lazily (PEP 562). `openadapt_evals.benchmarks.vm_cli`
# is the `oa-vm` entry point, so importing this package must not eagerly pull in
# `agents` (which drags the transformers/peft training stack — and crashes
# outright under NumPy 2 with a stale transformers). Each name maps to the
# submodule it lives in and is imported + cached on first access.
_LAZY_IMPORTS = {
    # agents
    "BenchmarkAgent": "openadapt_evals.agents",
    "RandomAgent": "openadapt_evals.agents",
    "ScriptedAgent": "openadapt_evals.agents",
    "SmartMockAgent": "openadapt_evals.agents",
    "ApiAgent": "openadapt_evals.agents",
    "PolicyAgent": "openadapt_evals.agents",
    "action_to_string": "openadapt_evals.agents",
    "format_accessibility_tree": "openadapt_evals.agents",
    "parse_action_response": "openadapt_evals.agents",
    # adapters
    "BenchmarkAction": "openadapt_evals.adapters",
    "BenchmarkAdapter": "openadapt_evals.adapters",
    "BenchmarkObservation": "openadapt_evals.adapters",
    "BenchmarkResult": "openadapt_evals.adapters",
    "BenchmarkTask": "openadapt_evals.adapters",
    "StaticDatasetAdapter": "openadapt_evals.adapters",
    "UIElement": "openadapt_evals.adapters",
    "WAAAdapter": "openadapt_evals.adapters",
    "WAAConfig": "openadapt_evals.adapters",
    "WAAMockAdapter": "openadapt_evals.adapters",
    "WAALiveAdapter": "openadapt_evals.adapters",
    "WAALiveConfig": "openadapt_evals.adapters",
    # evaluation runner
    "EvaluationConfig": "openadapt_evals.benchmarks.runner",
    "compute_domain_metrics": "openadapt_evals.benchmarks.runner",
    "compute_metrics": "openadapt_evals.benchmarks.runner",
    "evaluate_agent_on_benchmark": "openadapt_evals.benchmarks.runner",
    # data collection
    "ExecutionTraceCollector": "openadapt_evals.benchmarks.data_collection",
    "save_execution_trace": "openadapt_evals.benchmarks.data_collection",
    "LiveEvaluationTracker": "openadapt_evals.benchmarks.live_tracker",
    # viewers
    "generate_benchmark_viewer": "openadapt_evals.benchmarks.viewer",
    "generate_comparison_viewer": "openadapt_evals.benchmarks.comparison_viewer",
}

_AZURE_LAZY = ("AzureConfig", "AzureWAAOrchestrator", "AzureMLClient", "estimate_cost")


def __getattr__(name: str):
    """Resolve re-exports lazily so importing this package stays light."""
    import importlib

    module_path = _LAZY_IMPORTS.get(name)
    if module_path is not None:
        value = getattr(importlib.import_module(module_path), name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    if name in _AZURE_LAZY:
        from openadapt_evals.benchmarks import azure
        value = getattr(azure, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)

__all__ = [
    # Base classes (from adapters)
    "BenchmarkAdapter",
    "BenchmarkTask",
    "BenchmarkObservation",
    "BenchmarkAction",
    "BenchmarkResult",
    "StaticDatasetAdapter",
    "UIElement",
    # Agents (from agents)
    "BenchmarkAgent",
    "ScriptedAgent",
    "RandomAgent",
    "SmartMockAgent",
    "ApiAgent",
    "PolicyAgent",
    # Evaluation
    "EvaluationConfig",
    "evaluate_agent_on_benchmark",
    "compute_metrics",
    "compute_domain_metrics",
    # WAA adapters
    "WAAAdapter",
    "WAAConfig",
    "WAAMockAdapter",
    "WAALiveAdapter",
    "WAALiveConfig",
    # Azure (lazy imports)
    "AzureConfig",
    "AzureWAAOrchestrator",
    "AzureMLClient",
    "estimate_cost",
    # Viewers
    "generate_benchmark_viewer",
    "generate_comparison_viewer",
    # Data collection
    "ExecutionTraceCollector",
    "save_execution_trace",
    "LiveEvaluationTracker",
    # Utilities
    "action_to_string",
    "format_accessibility_tree",
    "parse_action_response",
]
