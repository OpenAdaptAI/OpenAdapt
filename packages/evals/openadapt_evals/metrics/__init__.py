"""Metrics for GUI agent evaluation.

This module provides metrics for evaluating GUI agent performance,
including grounding accuracy and trajectory similarity.

Currently includes re-exports from benchmarks.runner. Future additions:
- Grounding metrics (element localization accuracy)
- Trajectory metrics (action sequence similarity)
"""

from openadapt_evals.benchmarks.runner import compute_domain_metrics, compute_metrics

__all__ = [
    "compute_metrics",
    "compute_domain_metrics",
]
