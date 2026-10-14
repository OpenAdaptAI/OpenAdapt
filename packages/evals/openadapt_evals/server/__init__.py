"""WAA server extensions.

This module provides extensions for the Windows Agent Arena Flask server,
including the /evaluate endpoint that calls WAA's native evaluators.

These components are designed to be deployed to the WAA server running
inside the Windows VM.
"""

from openadapt_evals.server.evaluate_endpoint import (
    create_evaluate_blueprint,
    evaluate_task_state,
    get_actual_value,
    get_expected_value,
    run_metric,
)

__all__ = [
    "create_evaluate_blueprint",
    "evaluate_task_state",
    "get_actual_value",
    "get_expected_value",
    "run_metric",
]
