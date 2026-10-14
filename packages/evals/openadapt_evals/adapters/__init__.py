"""Benchmark adapters for evaluation infrastructure.

This module provides adapters for integrating various GUI benchmarks
with the evaluation framework:
- WAAAdapter: Windows Agent Arena (requires WAA repo)
- WAAMockAdapter: Mock adapter for testing (no Windows required)
- WAALiveAdapter: HTTP adapter for remote WAA server
- LocalAdapter: Local desktop automation (no VM required)
- ScrubMiddleware: PII scrubbing wrapper for any adapter

Available adapters:
    - BenchmarkAdapter: Abstract base class
    - StaticDatasetAdapter: For static trajectory datasets
    - WAAAdapter: Full WAA integration
    - WAAMockAdapter: Testing adapter
    - WAALiveAdapter: Remote HTTP adapter
    - LocalAdapter: Local desktop adapter
    - ScrubMiddleware: PII scrubbing middleware

Example:
    ```python
    from openadapt_evals.adapters import WAAMockAdapter, WAALiveAdapter
    from openadapt_evals.adapters import LocalAdapter, ScrubMiddleware

    # For local testing (no Windows VM)
    adapter = WAAMockAdapter(num_tasks=10)

    # For remote evaluation
    adapter = WAALiveAdapter(server_url="http://vm-ip:5000")

    # For local desktop with PII scrubbing
    adapter = ScrubMiddleware(LocalAdapter())
    ```
"""

from openadapt_evals.adapters.base import (
    BENCHMARK_ERROR_TYPES,
    BenchmarkAction,
    BenchmarkAdapter,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
    EvaluationUnavailableError,
    StaticDatasetAdapter,
    UIElement,
    normalize_benchmark_result,
)
from openadapt_evals.adapters.local import LocalAdapter
from openadapt_evals.adapters.rl_env import (
    ResetConfig,
    RLEnvironment,
    RolloutStep,
)
from openadapt_evals.adapters.scrub_middleware import ScrubMiddleware
from openadapt_evals.adapters.verl_env import (
    WAADesktopEnv,
    generate_env_spec,
    register_in_vagen,
)
from openadapt_evals.adapters.waa import (
    SyntheticTaskError,
    WAAAdapter,
    WAAConfig,
    WAALiveAdapter,
    WAALiveConfig,
    WAAMockAdapter,
    is_real_waa_task_id,
    is_synthetic_task_id,
)

__all__ = [
    # Base classes
    "BenchmarkAdapter",
    "BenchmarkTask",
    "BenchmarkObservation",
    "BenchmarkAction",
    "BenchmarkResult",
    "BENCHMARK_ERROR_TYPES",
    "normalize_benchmark_result",
    "EvaluationUnavailableError",
    "StaticDatasetAdapter",
    "UIElement",
    # Local adapter
    "LocalAdapter",
    # Scrubbing middleware
    "ScrubMiddleware",
    # RL environment
    "RLEnvironment",
    "ResetConfig",
    "RolloutStep",
    # WAA adapters
    "WAAAdapter",
    "WAAConfig",
    "WAAMockAdapter",
    "WAALiveAdapter",
    "WAALiveConfig",
    # verl-agent / VAGEN integration
    "WAADesktopEnv",
    "register_in_vagen",
    "generate_env_spec",
    # Task ID validation
    "SyntheticTaskError",
    "is_real_waa_task_id",
    "is_synthetic_task_id",
]
