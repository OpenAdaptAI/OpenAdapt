"""Weights & Biases Report Generation for OpenAdapt Evals.

This module provides programmatic report generation via the wandb Reports API,
enabling auto-configured dashboards for benchmark evaluation results.

Example:
    from openadapt_evals.integrations import WandbReportGenerator

    generator = WandbReportGenerator(
        project="openadapt-evals",
        entity="your-team",
    )

    # Generate standard benchmark report
    report_url = generator.create_benchmark_report(
        run_ids=["run1", "run2"],
        title="WAA Evaluation - Jan 2026",
    )

    # Or generate from specific scenarios
    report_url = generator.create_comparison_report(
        model_ids=["claude-sonnet-4-5", "gpt-5.1"],
    )

Environment Variables:
    WANDB_API_KEY: Your wandb API key (required)
    WANDB_PROJECT: Default project name (optional)
    WANDB_ENTITY: Team/organization name (optional)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from dotenv import load_dotenv

# Load .env file
load_dotenv()

logger = logging.getLogger(__name__)

# Try to import wandb, fail gracefully if not installed
try:
    import wandb
    from wandb.apis.reports import BarPlot, LinePlot, PanelGrid, Report, RunSet, ScatterPlot

    WANDB_REPORTS_AVAILABLE = True
except ImportError:
    WANDB_REPORTS_AVAILABLE = False
    wandb = None
    Report = None


class WandbReportGenerator:
    """Generator for wandb reports with standard benchmark visualizations.

    This class creates pre-configured wandb reports for benchmark evaluation
    results, including:
    - Success rate over time
    - Action accuracy by domain
    - Episode length distribution
    - Failure mode breakdown
    - Model comparison charts

    Args:
        project: Wandb project name.
        entity: Wandb entity (team/org) name.

    Raises:
        ImportError: If wandb is not installed or reports API unavailable.
        ValueError: If WANDB_API_KEY is not set.
    """

    def __init__(
        self,
        project: str = "openadapt-evals",
        entity: str | None = None,
    ):
        if not WANDB_REPORTS_AVAILABLE:
            raise ImportError(
                "wandb reports API not available. Install with: pip install wandb>=0.16.0 "
                "or: pip install openadapt-evals[wandb]"
            )

        api_key = os.environ.get("WANDB_API_KEY")
        if not api_key:
            raise ValueError(
                "WANDB_API_KEY not set. Set it in your environment or .env file."
            )

        self.project = project
        self.entity = entity or os.environ.get("WANDB_ENTITY")
        self.api = wandb.Api()

    def create_benchmark_report(
        self,
        run_ids: list[str] | None = None,
        title: str | None = None,
        description: str | None = None,
        include_charts: list[str] | None = None,
    ) -> str:
        """Create a comprehensive benchmark evaluation report.

        Args:
            run_ids: Specific run IDs to include (None = all recent runs).
            title: Report title.
            description: Report description.
            include_charts: List of chart types to include. Options:
                - "success_rate": Success rate over time
                - "domain_breakdown": Action accuracy by domain
                - "step_distribution": Episode length distribution
                - "error_breakdown": Failure mode breakdown
                - "cost_performance": Cost vs performance scatter
                Defaults to all charts.

        Returns:
            URL of the created report.
        """
        if include_charts is None:
            include_charts = [
                "success_rate",
                "domain_breakdown",
                "step_distribution",
                "error_breakdown",
            ]

        # Generate title if not provided
        if title is None:
            timestamp = datetime.now().strftime("%Y-%m-%d")
            title = f"Benchmark Evaluation Report - {timestamp}"

        # Create report
        report = Report(
            project=self.project,
            entity=self.entity,
            title=title,
            description=description or "Auto-generated benchmark evaluation report",
        )

        # Build panel grid with requested charts
        panels = []

        if "success_rate" in include_charts:
            panels.append(self._create_success_rate_panel())

        if "domain_breakdown" in include_charts:
            panels.append(self._create_domain_breakdown_panel())

        if "step_distribution" in include_charts:
            panels.append(self._create_step_distribution_panel())

        if "error_breakdown" in include_charts:
            panels.append(self._create_error_breakdown_panel())

        if "cost_performance" in include_charts:
            panels.append(self._create_cost_performance_panel())

        # Configure run set
        runset_config = {"project": self.project}
        if self.entity:
            runset_config["entity"] = self.entity
        if run_ids:
            runset_config["filters"] = {"$or": [{"name": rid} for rid in run_ids]}

        panel_grid = PanelGrid(
            runsets=[RunSet(**runset_config)],
            panels=panels,
        )

        report.blocks = [panel_grid]

        # Save report
        report.save()

        logger.info(f"Created report: {report.url}")
        return report.url

    def create_comparison_report(
        self,
        model_ids: list[str],
        title: str | None = None,
    ) -> str:
        """Create a model comparison report.

        Args:
            model_ids: List of model IDs to compare.
            title: Report title.

        Returns:
            URL of the created report.
        """
        if title is None:
            title = f"Model Comparison: {', '.join(model_ids[:3])}"
            if len(model_ids) > 3:
                title += f" (+{len(model_ids) - 3} more)"

        # Filter runs by model_id in config
        filters = {"$or": [{"config.model_id": mid} for mid in model_ids]}

        report = Report(
            project=self.project,
            entity=self.entity,
            title=title,
            description=f"Comparing {len(model_ids)} models on benchmark evaluation",
        )

        # Create comparison panels
        panels = [
            # Success rate comparison bar chart
            BarPlot(
                title="Success Rate by Model",
                metrics=["eval/success_rate"],
                groupby="config.model_id",
            ),
            # Domain breakdown grouped by model
            BarPlot(
                title="Domain Performance by Model",
                metrics=[
                    "eval/domain/notepad/success_rate",
                    "eval/domain/chrome/success_rate",
                    "eval/domain/file_explorer/success_rate",
                ],
                groupby="config.model_id",
            ),
            # Average steps comparison
            BarPlot(
                title="Average Steps by Model",
                metrics=["eval/avg_steps"],
                groupby="config.model_id",
            ),
        ]

        runset_config = {
            "project": self.project,
            "filters": filters,
        }
        if self.entity:
            runset_config["entity"] = self.entity

        panel_grid = PanelGrid(
            runsets=[RunSet(**runset_config)],
            panels=panels,
        )

        report.blocks = [panel_grid]
        report.save()

        logger.info(f"Created comparison report: {report.url}")
        return report.url

    def create_scenario_report(
        self,
        scenarios: list[str] | None = None,
        title: str = "Synthetic Scenarios Comparison",
    ) -> str:
        """Create a report comparing synthetic data scenarios.

        Args:
            scenarios: List of scenario names (noise, best, worst, median).
                Defaults to all scenarios.
            title: Report title.

        Returns:
            URL of the created report.
        """
        if scenarios is None:
            scenarios = ["noise", "best", "worst", "median"]

        # Filter by scenario tag
        filters = {"$or": [{"config.scenario": s} for s in scenarios]}

        report = Report(
            project=self.project,
            entity=self.entity,
            title=title,
            description="Comparison of synthetic benchmark scenarios",
        )

        panels = [
            BarPlot(
                title="Success Rate by Scenario",
                metrics=["eval/success_rate"],
                groupby="config.scenario",
            ),
            BarPlot(
                title="Average Steps by Scenario",
                metrics=["eval/avg_steps"],
                groupby="config.scenario",
            ),
            LinePlot(
                title="Expected vs Actual Success Rate",
                x="config.expected_success_rate",
                y=["eval/success_rate"],
            ),
        ]

        runset_config = {
            "project": self.project,
            "filters": filters,
        }
        if self.entity:
            runset_config["entity"] = self.entity

        panel_grid = PanelGrid(
            runsets=[RunSet(**runset_config)],
            panels=panels,
        )

        report.blocks = [panel_grid]
        report.save()

        logger.info(f"Created scenario report: {report.url}")
        return report.url

    def _create_success_rate_panel(self) -> LinePlot:
        """Create success rate over time line chart."""
        return LinePlot(
            title="Success Rate Over Time",
            x="_timestamp",
            y=["eval/success_rate"],
            smoothing_factor=0.0,
        )

    def _create_domain_breakdown_panel(self) -> BarPlot:
        """Create domain breakdown bar chart."""
        return BarPlot(
            title="Success Rate by Domain",
            metrics=[
                "eval/domain/notepad/success_rate",
                "eval/domain/chrome/success_rate",
                "eval/domain/file_explorer/success_rate",
                "eval/domain/settings/success_rate",
                "eval/domain/vlc/success_rate",
                "eval/domain/vs_code/success_rate",
            ],
            groupby="config.model_id",
        )

    def _create_step_distribution_panel(self) -> BarPlot:
        """Create step distribution panel."""
        return BarPlot(
            title="Step Count Distribution",
            metrics=[
                "eval/steps/success_mean",
                "eval/steps/fail_mean",
            ],
            groupby="config.model_id",
        )

    def _create_error_breakdown_panel(self) -> BarPlot:
        """Create error breakdown panel."""
        return BarPlot(
            title="Error Type Breakdown",
            metrics=[
                "eval/errors/timeout",
                "eval/errors/wrong_action",
                "eval/errors/navigation_error",
            ],
            groupby="config.model_id",
        )

    def _create_cost_performance_panel(self) -> ScatterPlot:
        """Create cost vs performance scatter plot."""
        return ScatterPlot(
            title="Cost vs Performance",
            x="eval/avg_time_seconds",
            y="eval/success_rate",
        )


def generate_standard_report(
    project: str = "openadapt-evals",
    entity: str | None = None,
    title: str | None = None,
) -> str:
    """Convenience function to generate a standard benchmark report.

    Args:
        project: Wandb project name.
        entity: Wandb entity (team/org).
        title: Report title.

    Returns:
        URL of the created report.
    """
    generator = WandbReportGenerator(project=project, entity=entity)
    return generator.create_benchmark_report(title=title)


def generate_demo_report(
    project: str = "openadapt-evals-demo",
    entity: str | None = None,
) -> str:
    """Generate a report for the demo synthetic data.

    This creates a report showing all synthetic scenarios side-by-side.

    Args:
        project: Wandb project name.
        entity: Wandb entity (team/org).

    Returns:
        URL of the created report.
    """
    generator = WandbReportGenerator(project=project, entity=entity)
    return generator.create_scenario_report(
        title="OpenAdapt Evals Demo - Synthetic Scenarios"
    )
