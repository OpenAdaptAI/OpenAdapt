"""OpenAdapt Evals: Evaluation infrastructure for GUI agent benchmarks.

This package provides:
- Benchmark adapters for Windows Agent Arena (WAA), OSWorld, WebArena, etc.
- Agent interfaces for evaluation (including ApiAgent with P0 demo persistence fix)
- Execution trace collection for replay viewers
- Metrics for grounding and trajectory evaluation

Package Structure:
    - openadapt_evals.agents: Agent implementations (BenchmarkAgent, ApiAgent, etc.)
    - openadapt_evals.adapters: Benchmark adapters (WAAAdapter, WAALiveAdapter, etc.)
    - openadapt_evals.benchmarks: Evaluation utilities (runner, metrics, viewer)

Quick Start:
    ```python
    from openadapt_evals import (
        WAAMockAdapter,
        SmartMockAgent,
        evaluate_agent_on_benchmark,
        compute_metrics,
    )

    # Create mock adapter for testing
    adapter = WAAMockAdapter(num_tasks=10)

    # Create agent
    agent = SmartMockAgent()

    # Run evaluation
    results = evaluate_agent_on_benchmark(agent, adapter, max_steps=15)

    # Compute metrics
    metrics = compute_metrics(results)
    print(f"Success rate: {metrics['success_rate']:.1%}")
    ```

For API-backed evaluation:
    ```python
    from openadapt_evals import ApiAgent, WAALiveAdapter

    # Use Claude Sonnet 4.5 with demo (P0 fix: demo persists across all steps)
    agent = ApiAgent(
        provider="anthropic",
        demo="Step 1: Click Start menu..."  # Included at EVERY step
    )

    adapter = WAALiveAdapter(server_url="http://vm-ip:5000")
    results = evaluate_agent_on_benchmark(agent, adapter, max_steps=15)
    ```

For benchmark viewer:
    ```python
    from openadapt_evals import generate_benchmark_viewer
    from pathlib import Path

    # Generate HTML viewer from benchmark results
    generate_benchmark_viewer(
        benchmark_dir=Path("benchmark_results/my_run"),
        output_path=Path("benchmark_results/my_run/viewer.html"),
    )
    ```
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("openadapt-evals")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

# Public API is resolved lazily (PEP 562). Importing this package must stay
# cheap: `oa-vm` (VM lifecycle) and other light entry points import
# `openadapt_evals` transitively, and eagerly pulling in `agents` drags the
# whole ML training stack (transformers/peft) into commands that never touch a
# model — which also made `oa-vm` crash outright under a NumPy 2 / stale
# transformers environment. Each name below imports its submodule only on first
# access, then caches it as a module global so later lookups are free.
_LAZY_IMPORTS = {
    # agents
    "BenchmarkAgent": "openadapt_evals.agents",
    "RandomAgent": "openadapt_evals.agents",
    "ScriptedAgent": "openadapt_evals.agents",
    "SmartMockAgent": "openadapt_evals.agents",
    "ApiAgent": "openadapt_evals.agents",
    "RetrievalAugmentedAgent": "openadapt_evals.agents",
    "DemoGuidedAgent": "openadapt_evals.agents",
    "PolicyAgent": "openadapt_evals.agents",
    "action_to_string": "openadapt_evals.agents",
    "format_accessibility_tree": "openadapt_evals.agents",
    "parse_action_response": "openadapt_evals.agents",
    # demo library
    "DemoLibrary": "openadapt_evals.demo_library",
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
    # benchmarks
    "EvaluationConfig": "openadapt_evals.benchmarks",
    "compute_domain_metrics": "openadapt_evals.benchmarks",
    "compute_metrics": "openadapt_evals.benchmarks",
    "evaluate_agent_on_benchmark": "openadapt_evals.benchmarks",
    "generate_benchmark_viewer": "openadapt_evals.benchmarks",
    "ExecutionTraceCollector": "openadapt_evals.benchmarks",
    "LiveEvaluationTracker": "openadapt_evals.benchmarks",
    "save_execution_trace": "openadapt_evals.benchmarks",
    # evaluation
    "TaskVerifierRegistry": "openadapt_evals.evaluation.verifier_registry",
    "VerificationResult": "openadapt_evals.evaluation.verifier_registry",
    # task config
    "evaluate_milestones_screenshot": "openadapt_evals.task_config",
}

_AZURE_LAZY = ("AzureConfig", "AzureWAAOrchestrator", "AzureMLClient", "estimate_cost")


def __getattr__(name: str):
    """Resolve public names lazily so importing the package stays light."""
    import importlib

    module_path = _LAZY_IMPORTS.get(name)
    if module_path is not None:
        value = getattr(importlib.import_module(module_path), name)
        globals()[name] = value  # cache: subsequent lookups skip __getattr__
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
    # Version
    "__version__",
    # Adapters (base classes)
    "BenchmarkAdapter",
    "BenchmarkTask",
    "BenchmarkObservation",
    "BenchmarkAction",
    "BenchmarkResult",
    "StaticDatasetAdapter",
    "UIElement",
    # Agents
    "BenchmarkAgent",
    "ScriptedAgent",
    "RandomAgent",
    "SmartMockAgent",
    "ApiAgent",
    "PolicyAgent",
    "RetrievalAugmentedAgent",
    "DemoGuidedAgent",
    "DemoLibrary",
    # Evaluation
    "EvaluationConfig",
    "evaluate_agent_on_benchmark",
    "compute_metrics",
    "compute_domain_metrics",
    # Task verification
    "TaskVerifierRegistry",
    "VerificationResult",
    # Milestone evaluation
    "evaluate_milestones_screenshot",
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
    # Viewer
    "generate_benchmark_viewer",
    # Data collection
    "ExecutionTraceCollector",
    "save_execution_trace",
    "LiveEvaluationTracker",
    # Utilities
    "action_to_string",
    "format_accessibility_tree",
    "parse_action_response",
]
