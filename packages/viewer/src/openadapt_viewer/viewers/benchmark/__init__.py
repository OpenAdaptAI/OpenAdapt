"""Benchmark viewer for displaying evaluation results.

This module generates standalone HTML files for visualizing
benchmark evaluation results (WAA, WebArena, OSWorld, etc.).
"""

from openadapt_viewer.viewers.benchmark.data import (
    create_sample_data,
    load_benchmark_data,
)
from openadapt_viewer.viewers.benchmark.generator import generate_benchmark_html

__all__ = [
    "create_sample_data",
    "generate_benchmark_html",
    "load_benchmark_data",
]
