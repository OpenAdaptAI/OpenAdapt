# Weights & Biases Integration for OpenAdapt Evals

## Overview

This document describes the integration of Weights & Biases (wandb) into the OpenAdapt evaluation framework for tracking and visualizing GUI agent benchmark results.

## Goals

1. **Automated tracking**: Log evaluation metrics to wandb after each benchmark run
2. **Reports**: Generate standardized dashboards showing key performance metrics
3. **Comparison**: Enable model comparison across runs
4. **Reproducibility**: Track hyperparameters, model configs, and environment info
5. **Programmatic screenshots**: Support automated report generation for PRs

## Data Structures

### Existing Eval Framework Data

The eval framework produces the following data structures:

#### BenchmarkResult (per task)
```python
@dataclass
class BenchmarkResult:
    task_id: str           # e.g., "notepad_1", "chrome_5"
    success: bool          # Binary success/failure
    score: float           # 0.0 to 1.0 (partial credit)
    num_steps: int         # Steps taken
    total_time_seconds: float
    error: str | None      # Error message if failed
    reason: str | None     # Explanation of result
```

#### Summary (per run)
```json
{
  "benchmark_name": "waa",
  "run_name": "waa_eval_20260122",
  "model_id": "claude-sonnet-4-5",
  "num_tasks": 154,
  "num_success": 30,
  "success_rate": 0.195,
  "avg_score": 0.23,
  "avg_steps": 8.5,
  "avg_time_seconds": 45.2,
  "tasks": [...]
}
```

#### Execution Trace (per task, per step)
```json
{
  "task_id": "notepad_1",
  "steps": [
    {
      "step_idx": 0,
      "screenshot_path": "screenshots/step_000.png",
      "action": {"type": "click", "x": 0.5, "y": 0.3, ...},
      "reasoning": "I should click the Start menu..."
    }
  ]
}
```

### WAA Domains (11 domains, 154 tasks)
- chrome, clock, file_explorer, libreoffice_calc, libreoffice_writer
- microsoft_paint, msedge, notepad, settings, vlc, vs_code, windows_calc

## Wandb Logging Strategy

### Run Configuration
```python
wandb.init(
    project="openadapt-evals",
    config={
        "model_id": "claude-sonnet-4-5",
        "benchmark": "waa",
        "max_steps": 15,
        "provider": "anthropic",
        "demo_enabled": True,
    },
    tags=["waa", "api-agent", "production"],
)
```

### Metrics to Log

#### Per-Run Summary Metrics
```python
wandb.log({
    # Overall performance
    "eval/success_rate": 0.195,
    "eval/avg_score": 0.23,
    "eval/avg_steps": 8.5,
    "eval/avg_time_seconds": 45.2,
    "eval/num_tasks": 154,
    "eval/num_success": 30,

    # By domain (logged as separate metrics for filtering)
    "eval/domain/chrome/success_rate": 0.25,
    "eval/domain/notepad/success_rate": 0.35,
    # ... etc
})
```

#### Per-Task Metrics (for detailed analysis)
```python
# Log as a wandb.Table for querying
task_table = wandb.Table(
    columns=["task_id", "domain", "success", "score", "num_steps", "time_s", "error"],
    data=[
        ["notepad_1", "notepad", True, 1.0, 5, 23.4, None],
        ["chrome_1", "chrome", False, 0.3, 15, 60.0, "timeout"],
        # ...
    ]
)
wandb.log({"eval/task_results": task_table})
```

#### Time Series (for runs over time)
```python
# Track improvement over model iterations
wandb.log({
    "eval/success_rate": success_rate,
    "eval/model_version": 3,
})
```

### Artifacts

#### Screenshots
```python
# Log sample screenshots for failure analysis
artifact = wandb.Artifact("eval_screenshots", type="dataset")
artifact.add_dir(f"benchmark_results/{run_name}/tasks/")
wandb.log_artifact(artifact)
```

#### Execution Traces
```python
# Log full execution traces for replay
artifact = wandb.Artifact("execution_traces", type="dataset")
artifact.add_file(f"benchmark_results/{run_name}/summary.json")
wandb.log_artifact(artifact)
```

## Reports Design

### 1. Success Rate Over Time
- **Type**: Line chart
- **X-axis**: Run timestamp or model version
- **Y-axis**: Success rate (0-100%)
- **Grouping**: By model_id, benchmark
- **Use case**: Track improvement over training iterations

### 2. Action Accuracy by Domain
- **Type**: Grouped bar chart
- **X-axis**: Domain (notepad, chrome, etc.)
- **Y-axis**: Success rate per domain
- **Grouping**: By model_id
- **Use case**: Identify weak domains for targeted improvement

### 3. Episode Length Distribution
- **Type**: Histogram / Box plot
- **X-axis**: Number of steps
- **Y-axis**: Count / Frequency
- **Filtering**: By success/failure
- **Use case**: Understand if failures are due to getting stuck vs wrong actions

### 4. Failure Mode Breakdown
- **Type**: Pie chart / Stacked bar
- **Categories**:
  - timeout (max steps reached)
  - wrong_action (clicked wrong element)
  - navigation_error (got lost in UI)
  - api_error (model API failure)
  - evaluation_error (evaluator issue)
- **Use case**: Prioritize failure categories for improvement

### 5. Model Comparison Chart
- **Type**: Radar chart / Table
- **Metrics**: Success rate, avg steps, avg time, by domain
- **Models**: Multiple model_ids side by side
- **Use case**: Compare model versions or different architectures

### 6. Cost vs Performance
- **Type**: Scatter plot
- **X-axis**: API cost ($) or tokens used
- **Y-axis**: Success rate
- **Points**: Individual runs
- **Use case**: Optimize cost-performance tradeoff

## Synthetic Data Fixtures

For testing and demo purposes, provide parametrizable fixtures:

### Scenario Types

#### 1. Pure Noise (Random Baseline)
```python
def generate_noise_data(num_tasks=154, domains=WAA_DOMAINS):
    """Random actions with ~10% success rate."""
    results = []
    for i in range(num_tasks):
        domain = random.choice(domains)
        success = random.random() < 0.10  # 10% random success
        results.append(BenchmarkResult(
            task_id=f"{domain}_{i}",
            success=success,
            score=random.uniform(0, 0.3),
            num_steps=random.randint(10, 15),  # Usually max out
            total_time_seconds=random.uniform(50, 120),
        ))
    return results
```

#### 2. Best Case (Strong Model)
```python
def generate_best_case_data(num_tasks=154, domains=WAA_DOMAINS):
    """High performance with ~85% success rate."""
    results = []
    for i in range(num_tasks):
        domain = random.choice(domains)
        success = random.random() < 0.85
        results.append(BenchmarkResult(
            task_id=f"{domain}_{i}",
            success=success,
            score=random.uniform(0.7, 1.0) if success else random.uniform(0.2, 0.5),
            num_steps=random.randint(3, 8) if success else random.randint(8, 15),
            total_time_seconds=random.uniform(15, 45),
        ))
    return results
```

#### 3. Worst Case (Failing Model)
```python
def generate_worst_case_data(num_tasks=154, domains=WAA_DOMAINS):
    """Consistently failing with ~5% success rate."""
    results = []
    for i in range(num_tasks):
        domain = random.choice(domains)
        success = random.random() < 0.05
        error = random.choice(["timeout", "wrong_action", "navigation_error"])
        results.append(BenchmarkResult(
            task_id=f"{domain}_{i}",
            success=success,
            score=random.uniform(0, 0.2),
            num_steps=15,  # Always max out
            total_time_seconds=random.uniform(90, 180),
            error=None if success else error,
        ))
    return results
```

#### 4. Median Case (Typical Performance)
```python
def generate_median_case_data(num_tasks=154, domains=WAA_DOMAINS):
    """SOTA-like performance with ~20% success rate, domain variation."""
    domain_rates = {
        "notepad": 0.35, "file_explorer": 0.30, "settings": 0.25,
        "chrome": 0.20, "vlc": 0.15, "vs_code": 0.10,
        # ... etc
    }
    results = []
    for i in range(num_tasks):
        domain = random.choice(domains)
        rate = domain_rates.get(domain, 0.20)
        success = random.random() < rate
        results.append(BenchmarkResult(
            task_id=f"{domain}_{i}",
            success=success,
            score=random.uniform(0.6, 1.0) if success else random.uniform(0.1, 0.4),
            num_steps=random.randint(4, 10) if success else random.randint(10, 15),
            total_time_seconds=random.uniform(30, 90),
        ))
    return results
```

## Implementation Plan

### Phase 1: Core Integration
1. Add `wandb` to optional dependencies in `pyproject.toml`
2. Create `openadapt_evals/integrations/wandb_logger.py`
3. Add WANDB_API_KEY to `.env.example`
4. Integrate with `evaluate_agent_on_benchmark()` runner

### Phase 2: Report Generation
1. Create wandb report templates via API
2. Implement `generate_wandb_report()` function
3. Add CLI command: `openadapt-evals wandb-report --run-name X`

### Phase 3: Programmatic Screenshots
1. Use wandb's export API to get chart images
2. Create `export_report_screenshots()` for PR automation
3. Document usage in CI/CD

### Phase 4: Fixtures & Testing
1. Implement synthetic data generators
2. Create `demo_wandb_integration.py` script
3. Add unit tests for wandb logger

## Usage Examples

### Basic Logging
```python
from openadapt_evals import evaluate_agent_on_benchmark, WAAMockAdapter
from openadapt_evals.integrations import WandbLogger

adapter = WAAMockAdapter()
agent = ApiAgent(provider="anthropic")

# Enable wandb logging
logger = WandbLogger(project="openadapt-evals")
results = evaluate_agent_on_benchmark(
    agent, adapter,
    callbacks=[logger.on_task_complete]
)
logger.finish(results)
```

### CLI Usage
```bash
# Run evaluation with wandb logging
openadapt-evals mock --tasks 20 --wandb

# Generate report from existing run
openadapt-evals wandb-report --run-name waa_eval_20260122

# Export screenshots for PR
openadapt-evals wandb-export --run-name waa_eval_20260122 --output pr_screenshots/
```

### Demo with Synthetic Data
```bash
# Populate wandb with all fixture types for demo
python -m openadapt_evals.integrations.demo_wandb \
    --project openadapt-evals-demo \
    --scenarios noise best worst median
```

## Environment Variables

```bash
# Required
WANDB_API_KEY=your_api_key_here

# Optional
WANDB_PROJECT=openadapt-evals  # Default project name
WANDB_ENTITY=openadaptai       # Team/organization name
WANDB_MODE=online              # online, offline, disabled
```

## File Structure

```
openadapt_evals/
├── integrations/
│   ├── __init__.py
│   ├── wandb_logger.py       # Core logging class
│   ├── wandb_reports.py      # Report generation via Reports API
│   └── fixtures.py           # Synthetic data generators
└── benchmarks/
    └── cli.py                # CLI commands for wandb
```

## Reports API Details

### WandbReportGenerator

The `WandbReportGenerator` class provides programmatic report creation:

```python
from openadapt_evals.integrations import WandbReportGenerator

generator = WandbReportGenerator(
    project="openadapt-evals",
    entity="openadaptai",
)

# Create standard benchmark report
report_url = generator.create_benchmark_report(
    run_ids=["run1", "run2"],  # Optional: filter to specific runs
    title="WAA Evaluation - Jan 2026",
    include_charts=["success_rate", "domain_breakdown", "error_breakdown"],
)

# Create model comparison report
report_url = generator.create_comparison_report(
    model_ids=["claude-sonnet-4-5", "gpt-5.1", "openadapt-v3"],
)

# Create scenario comparison for synthetic demos
report_url = generator.create_scenario_report(
    scenarios=["noise", "best", "worst", "median"],
)
```

### Convenience Functions

```python
from openadapt_evals.integrations.wandb_reports import (
    generate_standard_report,
    generate_demo_report,
)

# Quick standard report
url = generate_standard_report(project="openadapt-evals")

# Demo report for synthetic scenarios
url = generate_demo_report(project="openadapt-evals-demo")
```

### CLI Commands

```bash
# Generate report from existing runs
openadapt-evals wandb-report --run-name waa_eval_20260122

# Generate comparison report
openadapt-evals wandb-report --compare --model-ids claude-sonnet-4-5,gpt-5.1

# Generate demo report from synthetic scenarios
openadapt-evals wandb-report --demo
```

### Available Chart Types

| Chart | Metric Keys | Description |
|-------|-------------|-------------|
| `success_rate` | `eval/success_rate` | Line chart of success rate over time |
| `domain_breakdown` | `eval/domain/*/success_rate` | Bar chart of per-domain success rates |
| `step_distribution` | `eval/steps/*_mean` | Success vs failure step counts |
| `error_breakdown` | `eval/errors/*` | Bar chart of error type counts |
| `cost_performance` | `eval/avg_time_seconds` vs `eval/success_rate` | Scatter plot |

## References

- [Wandb Python SDK](https://docs.wandb.ai/ref/python)
- [Wandb Reports API](https://docs.wandb.ai/guides/reports)
- [Wandb Tables](https://docs.wandb.ai/guides/tables)
- [Wandb Artifacts](https://docs.wandb.ai/guides/artifacts)
