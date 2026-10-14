"""Viewer implementations for openadapt-viewer.

Each viewer type is organized as a vertical slice with:
- data.py: Pydantic models and data loading
- generator.py: HTML generation logic
"""

from openadapt_viewer.viewers.benchmark import generate_benchmark_html

__all__ = ["generate_benchmark_html"]
