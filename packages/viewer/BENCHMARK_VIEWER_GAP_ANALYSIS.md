# Benchmark Viewer Gap Analysis & Live Stats Dashboard Implementation

**Date**: January 17, 2026
**Status**: In Progress
**Priority**: High (P0)

## Executive Summary

This document analyzes gaps between current and legacy benchmark viewer implementations and proposes a live stats dashboard for real-time monitoring of benchmark evaluations, training jobs, and cloud resources.

## Phase 1: Review of Current Implementations

### 1.1 Current Implementations Located

| Location | File | Purpose | Status |
|----------|------|---------|--------|
| **openadapt-evals** | `openadapt_evals/benchmarks/viewer.py` | Generates static HTML viewer for benchmark results | ✅ Complete |
| **openadapt-ml** | `openadapt_ml/benchmarks/viewer.py` | Imports from openadapt-ml/training (shared UI) | ✅ Complete |
| **openadapt-ml** | `openadapt_ml/training/benchmark_viewer.py` | Unified training+benchmark viewer (174KB) | ✅ Complete |
| **openadapt-viewer** | `viewers/benchmark/generator.py` | Component-based benchmark viewer | ✅ Complete |
| **openadapt-viewer** | `examples/benchmark_example.py` | Example usage of component library | ✅ Complete |

### 1.2 Existing Live Tracking Infrastructure

| Component | File | Features | Integration Status |
|-----------|------|----------|-------------------|
| **LiveEvaluationTracker** | `openadapt_evals/benchmarks/live_tracker.py` | Tracks real-time evaluation progress, writes to `benchmark_live.json` | ✅ Implemented |
| **API Endpoint** | `openadapt_ml/cloud/local.py:682` | `/api/benchmark-live` - polls `benchmark_live.json` | ✅ Implemented |
| **API Endpoint** | `openadapt_ml/cloud/local.py:667` | `/api/benchmark-progress` - polls `benchmark_progress.json` | ✅ Implemented |
| **API Endpoint** | `openadapt_ml/cloud/local.py:695` | `/api/tasks` - background task status (VM, Docker, benchmarks) | ✅ Implemented |
| **API Endpoint** | `openadapt_ml/cloud/local.py:710` | `/api/azure-jobs` - live Azure ML job status | ✅ Implemented |
| **API Endpoint** | `openadapt_ml/cloud/local.py:749` | `/api/vms` - VM registry with live status | ✅ Implemented |
| **API Endpoint** | `openadapt_ml/cloud/local.py:802` | `/api/tunnels` - SSH tunnel status (VNC/WAA) | ✅ Implemented |
| **API Endpoint** | `openadapt_ml/cloud/local.py:785` | `/api/probe-vm` - WAA server health check | ✅ Implemented |
| **SSE Endpoint** | `openadapt_ml/cloud/local.py:735` | `/api/benchmark-sse` - Server-Sent Events for real-time updates | ✅ Implemented |

### 1.3 Key Findings

**Strengths:**
1. ✅ **Comprehensive API layer** - 10+ endpoints for live monitoring
2. ✅ **Real-time tracking** - LiveEvaluationTracker writes step-by-step progress
3. ✅ **Multiple viewer implementations** - Static HTML (evals), unified dashboard (ml), component library (viewer)
4. ✅ **SSH tunnel management** - Automatic VNC/WAA access via tunnels
5. ✅ **Azure ML integration** - Live job status, logs, metrics
6. ✅ **Shared UI components** - `shared_ui.py` for consistent styling

**Gaps Identified:**
1. ❌ **No unified live stats dashboard** - Viewers are static post-evaluation
2. ❌ **Cost tracking incomplete** - No API endpoint for aggregated costs (Azure VM, API calls, GPU time)
3. ❌ **Performance metrics not aggregated** - No endpoint for success rate over time, domain breakdown trends
4. ❌ **Worker utilization not visualized** - No dashboard showing which VMs/workers are active
5. ❌ **Historical comparison missing** - No side-by-side comparison of multiple runs
6. ❌ **Real-time job monitor UI missing** - API exists but no dashboard component
7. ❌ **ETA and progress not calculated** - LiveEvaluationTracker doesn't estimate time remaining

## Phase 2: Gap Analysis

### 2.1 Live Job Status Tracking

**Current State:**
- ✅ `LiveEvaluationTracker` tracks current task, steps, and results
- ✅ `/api/benchmark-live` polls `benchmark_live.json` (updated every step)
- ✅ `/api/azure-jobs` fetches live Azure ML job status

**Gaps:**
- ❌ No UI component displaying current job status in real-time
- ❌ No queue status visualization (pending vs running tasks)
- ❌ No ETA calculation (based on avg task time)
- ❌ No progress bar showing X/Y tasks completed

**Proposed Solution:**
```python
# Add to /api/benchmark/status endpoint
{
  "current_job": {
    "run_id": "waa_eval_20260117_123456",
    "model_id": "anthropic-api",
    "status": "running",
    "total_tasks": 154,
    "completed_tasks": 42,
    "current_task": {
      "task_id": "notepad_15",
      "instruction": "Type hello world",
      "step": 3,
      "total_steps_estimate": 5
    },
    "elapsed_seconds": 1234,
    "eta_seconds": 2000,
    "avg_task_seconds": 29.4
  },
  "queue": [
    {"task_id": "browser_10", "status": "pending"},
    {"task_id": "office_3", "status": "pending"}
  ]
}
```

### 2.2 Cost Tracking

**Current State:**
- ❌ No cost tracking infrastructure
- ⚠️ Cloud cost estimates exist in CLI (`benchmarks/cli.py:estimate`) but not exposed as API
- ⚠️ Training dashboard shows instance type and hourly rate but not running total

**Gaps:**
- ❌ No API endpoint for cost breakdown
- ❌ No tracking of API calls (Anthropic/OpenAI)
- ❌ No GPU time tracking per job
- ❌ No running total during evaluation

**Proposed Solution:**
```python
# Add to /api/benchmark/costs endpoint
{
  "azure_vm": {
    "instance_type": "Standard_D4ds_v5",
    "hourly_rate_usd": 0.192,
    "hours_elapsed": 2.5,
    "cost_usd": 0.48
  },
  "api_calls": {
    "anthropic": {
      "model": "claude-sonnet-4-5-20250929",
      "input_tokens": 125000,
      "output_tokens": 8500,
      "cost_usd": 3.75
    },
    "openai": {
      "model": "gpt-5.1",
      "input_tokens": 0,
      "output_tokens": 0,
      "cost_usd": 0.0
    }
  },
  "gpu_time": {
    "lambda_labs": {
      "instance_type": "gpu_1x_a10",
      "hourly_rate_usd": 0.75,
      "hours_elapsed": 0.0,
      "cost_usd": 0.0
    }
  },
  "total_cost_usd": 4.23
}
```

### 2.3 Performance Metrics Display

**Current State:**
- ✅ Static viewers show success rate, domain breakdown, task list
- ✅ `/api/benchmark-live` includes current task result
- ⚠️ No aggregation over time (trends, moving averages)

**Gaps:**
- ❌ No success rate over time chart
- ❌ No average steps per task trend
- ❌ No domain difficulty ranking (success rate by domain)
- ❌ No comparison with previous runs

**Proposed Solution:**
```python
# Add to /api/benchmark/metrics endpoint
{
  "success_rate_over_time": [
    {"task_idx": 0, "success_rate": 0.0},
    {"task_idx": 10, "success_rate": 0.4},
    {"task_idx": 20, "success_rate": 0.45},
    # ... (rolling window)
  ],
  "avg_steps_per_task": [
    {"task_idx": 0, "avg_steps": 5.2},
    {"task_idx": 10, "avg_steps": 6.8},
    # ...
  ],
  "domain_breakdown": {
    "notepad": {"total": 20, "success": 15, "rate": 0.75, "avg_steps": 4.2},
    "browser": {"total": 30, "success": 12, "rate": 0.40, "avg_steps": 8.1},
    "office": {"total": 15, "success": 10, "rate": 0.67, "avg_steps": 6.5},
    # ...
  },
  "episode_success_metrics": {
    "first_action_accuracy": 0.82,  # % of tasks with correct first action
    "episode_success_rate": 0.45,   # % of tasks fully completed
    "avg_steps_to_success": 5.3,
    "avg_steps_to_failure": 8.7
  }
}
```

### 2.4 Real-Time Updates

**Current State:**
- ✅ SSE endpoint `/api/benchmark-sse` for streaming updates
- ✅ Polling endpoints for `/api/benchmark-live`, `/api/benchmark-progress`
- ⚠️ No UI component using SSE for live updates

**Gaps:**
- ❌ Dashboard uses polling (5s interval) instead of SSE
- ❌ No WebSocket alternative for bidirectional communication
- ❌ No auto-refresh when evaluation completes

**Proposed Solution:**
Use existing `/api/benchmark-sse` endpoint with EventSource in dashboard:
```javascript
const eventSource = new EventSource('/api/benchmark-sse?interval=2');
eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  updateDashboard(data);  // Update UI without polling
};
```

### 2.5 Historical Comparison

**Current State:**
- ✅ Benchmark results saved to `benchmark_results/{run_name}/`
- ✅ Each run has `summary.json`, `metadata.json`, task executions
- ❌ No UI for comparing multiple runs side-by-side

**Gaps:**
- ❌ No API endpoint listing available runs
- ❌ No multi-run comparison view
- ❌ No regression detection (success rate dropping over time)

**Proposed Solution:**
```python
# Add to /api/benchmark/runs endpoint (list available runs)
{
  "runs": [
    {
      "run_id": "waa_eval_20260117_123456",
      "model_id": "anthropic-api",
      "created_at": "2026-01-17T12:34:56Z",
      "total_tasks": 154,
      "success_rate": 0.45,
      "avg_steps": 6.2
    },
    {
      "run_id": "waa_eval_20260116_090123",
      "model_id": "gpt-5.1",
      "created_at": "2026-01-16T09:01:23Z",
      "total_tasks": 154,
      "success_rate": 0.38,
      "avg_steps": 7.8
    }
  ]
}

# Add to /api/benchmark/compare?runs=A,B endpoint
{
  "comparison": {
    "run_a": {...},  # Full metrics for run A
    "run_b": {...},  # Full metrics for run B
    "delta": {
      "success_rate": +0.07,
      "avg_steps": -1.6,
      "domain_deltas": {
        "notepad": {"success_rate": +0.15},
        # ...
      }
    }
  }
}
```

### 2.6 Worker Utilization

**Current State:**
- ✅ `/api/vms` returns VM registry with status
- ✅ `/api/azure-jobs` returns running Azure ML jobs
- ⚠️ No visualization of which workers are active

**Gaps:**
- ❌ No worker utilization chart (X/Y workers active)
- ❌ No per-worker task assignment tracking
- ❌ No idle time calculation

**Proposed Solution:**
```python
# Add to /api/benchmark/workers endpoint
{
  "total_workers": 10,
  "active_workers": 7,
  "idle_workers": 3,
  "workers": [
    {
      "worker_id": "vm-001",
      "status": "running",
      "current_task": "notepad_5",
      "tasks_completed": 12,
      "uptime_seconds": 3600,
      "idle_time_seconds": 120
    },
    {
      "worker_id": "vm-002",
      "status": "idle",
      "current_task": null,
      "tasks_completed": 8,
      "uptime_seconds": 2400,
      "idle_time_seconds": 600
    }
  ]
}
```

## Phase 3: Implementation Plan

### 3.1 Backend API Endpoints (openadapt-ml/cloud/local.py)

**Priority: P0 (Required for live dashboard)**

Add new API endpoints to `StopHandler.do_GET()`:

1. `/api/benchmark/status` - Current job status, queue, ETA
2. `/api/benchmark/costs` - Cost breakdown (Azure, API, GPU)
3. `/api/benchmark/metrics` - Performance metrics (success rate, domain breakdown)
4. `/api/benchmark/workers` - Worker status and utilization
5. `/api/benchmark/runs` - List available benchmark runs
6. `/api/benchmark/compare?runs=A,B` - Compare multiple runs

### 3.2 Live Stats Dashboard UI

**Priority: P0 (Required for monitoring)**

Create unified dashboard in `openadapt_ml/training/benchmark_viewer.py`:

```html
<!-- Live Stats Dashboard Structure -->
<div class="unified-dashboard">
  <!-- Header with tabs -->
  <div class="unified-header">
    <div class="nav-tabs">
      <a href="dashboard.html">Training</a>
      <a href="viewer.html">Viewer</a>
      <a href="benchmark.html" class="active">Benchmarks</a>
      <a href="live-stats.html">Live Stats</a>  <!-- NEW TAB -->
    </div>
  </div>

  <!-- Live Stats Panels -->
  <div class="dashboard-grid">
    <!-- Panel 1: Current Job Status -->
    <div class="panel job-status">
      <h3>Current Job</h3>
      <div class="job-info">
        <span class="run-id">waa_eval_20260117_123456</span>
        <span class="model-id">anthropic-api</span>
      </div>
      <div class="progress-bar">
        <div class="progress" style="width: 27%"></div>
      </div>
      <div class="stats">
        <span>42 / 154 tasks</span>
        <span>ETA: 33 min</span>
      </div>
      <div class="current-task">
        <strong>notepad_15</strong> - Step 3/5
      </div>
    </div>

    <!-- Panel 2: Cost Tracker -->
    <div class="panel cost-tracker">
      <h3>Running Costs</h3>
      <div class="cost-breakdown">
        <div class="cost-item">
          <span>Azure VM</span>
          <span class="cost">$0.48</span>
        </div>
        <div class="cost-item">
          <span>API Calls (Claude)</span>
          <span class="cost">$3.75</span>
        </div>
        <div class="cost-item">
          <span>GPU Time</span>
          <span class="cost">$0.00</span>
        </div>
        <div class="cost-total">
          <strong>Total</strong>
          <strong class="cost">$4.23</strong>
        </div>
      </div>
    </div>

    <!-- Panel 3: Performance Metrics -->
    <div class="panel performance-metrics">
      <h3>Performance</h3>
      <div class="metric-cards">
        <div class="metric-card">
          <div class="metric-value success">45%</div>
          <div class="metric-label">Success Rate</div>
        </div>
        <div class="metric-card">
          <div class="metric-value">6.2</div>
          <div class="metric-label">Avg Steps</div>
        </div>
        <div class="metric-card">
          <div class="metric-value success">82%</div>
          <div class="metric-label">First Action</div>
        </div>
      </div>
      <!-- Chart: Success rate over time -->
      <canvas id="successRateChart"></canvas>
    </div>

    <!-- Panel 4: Worker Utilization -->
    <div class="panel worker-utilization">
      <h3>Workers</h3>
      <div class="worker-stats">
        <span>7 / 10 active</span>
        <span>3 idle</span>
      </div>
      <div class="worker-list">
        <div class="worker-item active">
          <span class="worker-id">vm-001</span>
          <span class="worker-task">notepad_5</span>
          <span class="worker-count">12 tasks</span>
        </div>
        <div class="worker-item idle">
          <span class="worker-id">vm-002</span>
          <span class="worker-task">idle</span>
          <span class="worker-count">8 tasks</span>
        </div>
        <!-- ... -->
      </div>
    </div>

    <!-- Panel 5: Domain Breakdown -->
    <div class="panel domain-breakdown">
      <h3>Results by Domain</h3>
      <div class="domain-stats">
        <div class="domain-item">
          <span class="domain-name">notepad</span>
          <div class="domain-bar">
            <div class="bar-fill success" style="width: 75%"></div>
          </div>
          <span class="domain-rate">75%</span>
        </div>
        <div class="domain-item">
          <span class="domain-name">browser</span>
          <div class="domain-bar">
            <div class="bar-fill error" style="width: 40%"></div>
          </div>
          <span class="domain-rate">40%</span>
        </div>
        <!-- ... -->
      </div>
    </div>

    <!-- Panel 6: Historical Comparison -->
    <div class="panel historical-comparison">
      <h3>Recent Runs</h3>
      <div class="run-comparison">
        <div class="run-item">
          <span class="run-date">Jan 17, 2026</span>
          <span class="run-model">anthropic-api</span>
          <span class="run-rate success">45%</span>
        </div>
        <div class="run-item">
          <span class="run-date">Jan 16, 2026</span>
          <span class="run-model">gpt-5.1</span>
          <span class="run-rate">38%</span>
        </div>
        <!-- ... -->
      </div>
    </div>
  </div>
</div>
```

### 3.3 Real-Time Update Mechanism

Use EventSource (Server-Sent Events) for live updates:

```javascript
// Dashboard JavaScript
function initLiveStatsDashboard() {
  // Connect to SSE endpoint
  const eventSource = new EventSource('/api/benchmark-sse?interval=2');

  eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);

    // Update all dashboard panels
    updateJobStatus(data.current_job);
    updateCosts(data.costs);
    updateMetrics(data.metrics);
    updateWorkers(data.workers);
    updateDomainBreakdown(data.domain_breakdown);
  };

  eventSource.onerror = () => {
    console.error('SSE connection lost, falling back to polling');
    startPolling();  // Fallback to 5s polling if SSE fails
  };
}

function startPolling() {
  setInterval(async () => {
    const [status, costs, metrics, workers] = await Promise.all([
      fetch('/api/benchmark/status').then(r => r.json()),
      fetch('/api/benchmark/costs').then(r => r.json()),
      fetch('/api/benchmark/metrics').then(r => r.json()),
      fetch('/api/benchmark/workers').then(r => r.json())
    ]);

    updateJobStatus(status);
    updateCosts(costs);
    updateMetrics(metrics);
    updateWorkers(workers);
  }, 5000);
}
```

### 3.4 Cost Tracking Implementation

**Add to benchmark runner** (`openadapt_evals/benchmarks/runner.py`):

```python
from dataclasses import dataclass
from typing import Dict

@dataclass
class CostTracker:
    """Tracks costs during benchmark evaluation."""

    azure_vm_hourly_rate: float = 0.192  # Standard_D4ds_v5
    api_costs: Dict[str, float] = None  # {provider: cost_usd}
    gpu_hourly_rate: float = 0.0
    start_time: float = 0.0

    def __post_init__(self):
        if self.api_costs is None:
            self.api_costs = {"anthropic": 0.0, "openai": 0.0}
        self.start_time = time.time()

    def add_api_call(self, provider: str, input_tokens: int, output_tokens: int):
        """Add cost for an API call."""
        # Pricing (as of Jan 2026)
        rates = {
            "anthropic": {  # Claude Sonnet 4.5
                "input": 3.00 / 1_000_000,   # $3 per million input tokens
                "output": 15.00 / 1_000_000  # $15 per million output tokens
            },
            "openai": {  # GPT-5.1
                "input": 5.00 / 1_000_000,
                "output": 15.00 / 1_000_000
            }
        }

        if provider in rates:
            cost = (input_tokens * rates[provider]["input"] +
                   output_tokens * rates[provider]["output"])
            self.api_costs[provider] += cost

    def get_total_cost(self) -> float:
        """Calculate total cost so far."""
        elapsed_hours = (time.time() - self.start_time) / 3600
        azure_cost = self.azure_vm_hourly_rate * elapsed_hours
        api_cost = sum(self.api_costs.values())
        gpu_cost = self.gpu_hourly_rate * elapsed_hours
        return azure_cost + api_cost + gpu_cost

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        elapsed_hours = (time.time() - self.start_time) / 3600
        return {
            "azure_vm": {
                "hourly_rate_usd": self.azure_vm_hourly_rate,
                "hours_elapsed": elapsed_hours,
                "cost_usd": self.azure_vm_hourly_rate * elapsed_hours
            },
            "api_calls": {
                provider: {"cost_usd": cost}
                for provider, cost in self.api_costs.items()
            },
            "gpu_time": {
                "hourly_rate_usd": self.gpu_hourly_rate,
                "hours_elapsed": elapsed_hours,
                "cost_usd": self.gpu_hourly_rate * elapsed_hours
            },
            "total_cost_usd": self.get_total_cost()
        }
```

## Phase 4: Testing Strategy

### 4.1 API Endpoint Testing

```bash
# Test /api/benchmark/status
curl http://localhost:8080/api/benchmark/status | jq

# Test /api/benchmark/costs
curl http://localhost:8080/api/benchmark/costs | jq

# Test /api/benchmark/metrics
curl http://localhost:8080/api/benchmark/metrics | jq

# Test /api/benchmark/workers
curl http://localhost:8080/api/benchmark/workers | jq

# Test SSE streaming
curl -N http://localhost:8080/api/benchmark-sse?interval=2
```

### 4.2 UI Testing

```bash
# Start dashboard with live stats
uv run python -m openadapt_ml.cloud.local serve --open --start-page live-stats.html

# Run mock evaluation in parallel
uv run python -m openadapt_evals.benchmarks.cli mock --tasks 20 &

# Observe live updates in browser
```

## Next Steps

1. ✅ Complete this gap analysis document
2. ⏳ Implement backend API endpoints (3.1)
3. ⏳ Implement CostTracker in runner (3.4)
4. ⏳ Create live stats dashboard UI (3.2)
5. ⏳ Add real-time update mechanism (3.3)
6. ⏳ Test with mock evaluation (4.2)
7. ⏳ Test with live WAA evaluation
8. ⏳ Document API endpoints in openadapt-evals/CLAUDE.md

## Estimated Time

- **API Endpoints**: 1-2 hours (4 endpoints)
- **CostTracker**: 30 minutes
- **Dashboard UI**: 2-3 hours (6 panels)
- **Real-time Updates**: 30 minutes (SSE integration)
- **Testing**: 1 hour
- **Total**: 5-7 hours

## References

- LiveEvaluationTracker: `openadapt_evals/benchmarks/live_tracker.py`
- Existing API endpoints: `openadapt_ml/cloud/local.py` lines 500-850
- Shared UI components: `openadapt_ml/training/shared_ui.py`
- Benchmark viewer: `openadapt_evals/benchmarks/viewer.py`
