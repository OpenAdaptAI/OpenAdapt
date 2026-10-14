"""Trace analysis utilities for OpenAdapt evaluation runs.

Provides aggregate statistics, failure mode classification, step-by-step
analysis, run comparison, and HTML report generation for traces produced by
:class:`~openadapt_evals.training.trajectory_logger.PlannerTrajectoryLogger`,
``run_full_eval.py``, or the benchmark viewer directory format.

Quick start::

    from openadapt_evals.analysis import TraceAnalyzer

    analyzer = TraceAnalyzer("path/to/traces/")
    print(analyzer.summary())
    analyzer.generate_report("report.html")

CLI usage::

    python -m openadapt_evals.analysis path/to/traces/
    python -m openadapt_evals.analysis path/to/traces/ --report report.html
    python -m openadapt_evals.analysis path/to/traces/ --compare other/ --report diff.html
"""

from openadapt_evals.analysis.report_generator import generate_report
from openadapt_evals.analysis.trace_analyzer import TraceAnalyzer

__all__ = [
    "TraceAnalyzer",
    "generate_report",
]
