"""Integrations with external services.

This module provides integrations with:
- Weights & Biases (wandb) for experiment tracking and report generation
- W&B callback functions for the standalone GRPO trainer
- Weave (W&B) for LLM/agent execution tracing
- TRL TrainerCallback for telemetry during GRPO training
"""

from openadapt_evals.integrations.fixtures import (
    Scenario,
    generate_best_case_data,
    generate_median_case_data,
    generate_noise_data,
    generate_worst_case_data,
)
from openadapt_evals.integrations.trl_callbacks import TelemetryCallback
from openadapt_evals.integrations.wandb_callbacks import (
    wandb_model_loaded,
    wandb_rollout_logger,
    wandb_step_logger,
)
from openadapt_evals.integrations.wandb_logger import WandbLogger

# Import report generator (may fail if wandb reports API not available)
try:
    from openadapt_evals.integrations.wandb_reports import (
        WandbReportGenerator,
        generate_demo_report,
        generate_standard_report,
    )
    _REPORTS_AVAILABLE = True
except ImportError:
    WandbReportGenerator = None
    generate_standard_report = None
    generate_demo_report = None
    _REPORTS_AVAILABLE = False

# Weave tracing (optional — no-op when weave not installed)
try:
    from openadapt_evals.integrations.weave_integration import weave_init, weave_op
except ImportError:
    weave_init = None
    weave_op = None

__all__ = [
    "TelemetryCallback",
    "WandbLogger",
    "WandbReportGenerator",
    "weave_init",
    "weave_op",
    "wandb_model_loaded",
    "wandb_rollout_logger",
    "wandb_step_logger",
    "generate_noise_data",
    "generate_best_case_data",
    "generate_worst_case_data",
    "generate_median_case_data",
    "generate_standard_report",
    "generate_demo_report",
    "Scenario",
]
