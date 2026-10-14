# Minimal Benchmark Viewer

A simple, focused benchmark viewer built for iterative development.

## Quick Start

### Option 1: HTTP Serving (Recommended)

```bash
# From openadapt-ml directory
cd /path/to/openadapt-ml

# Copy viewer to served directory
cp /path/to/openadapt-viewer/viewers/benchmark/minimal_viewer.html \
   training_output/current/minimal_benchmark.html

# Start server
uv run python -m openadapt_ml.cloud.local serve --port 8765

# Open in browser
open http://localhost:8765/minimal_benchmark.html
```

The viewer will automatically load data from `/api/benchmark/runs`.

### Option 2: Standalone HTML with Embedded Data

```bash
# Generate standalone HTML file
cd /path/to/openadapt-viewer

python viewers/benchmark/generator.py \
  --results-dir /path/to/openadapt-ml/benchmark_results \
  --run-name waa_eval_20251217_test_real \
  --output /tmp/benchmark_viewer.html

# Open directly in browser (no server needed!)
open /tmp/benchmark_viewer.html
```

### Option 3: Using from Python

```python
from pathlib import Path
from viewers.benchmark.generator import generate_from_benchmark_results

# Generate viewer
output = generate_from_benchmark_results(
    results_dir="benchmark_results",
    run_name="waa_eval_20251217_test_real",
    output_path="viewer.html"
)

print(f"Generated: {output}")
```

## Features

### MVP Features (Current)
- ✅ List of completed benchmark runs
- ✅ Task-level results (pass/fail status)
- ✅ Step-by-step execution trace
- ✅ Screenshot display for each step
- ✅ Basic metrics (success rate, avg steps, avg time)
- ✅ Domain extraction from task IDs
- ✅ Click to view task details
- ✅ Click screenshots to open full size

### Architecture
- **Single HTML file**: 589 lines, easy to share and deploy
- **Pure Alpine.js**: No build step, works in any browser
- **Progressive enhancement**: Works offline with embedded data, or loads from API
- **Minimal dependencies**: Just Alpine.js from CDN

## Data Flow

### With API (Dynamic)
```
Browser → /api/benchmark/runs → List of runs
Browser → Select run → Show metrics and tasks
Browser → Click task → /api/benchmark/tasks/{run}/{task} → Execution details
Browser → View screenshot → /api/benchmark/screenshots/{run}/{task}/screenshots/{file}
```

### With Embedded Data (Static)
```
Generator → Read summary.json → Embed in HTML → window.BENCHMARK_DATA
Browser → Load HTML → Parse embedded data → Display
```

## API Endpoints

Implemented in `openadapt-ml/openadapt_ml/cloud/local.py`:

### GET `/api/benchmark/runs`
Returns array of benchmark run summaries.

**Response:**
```json
[
  {
    "run_name": "waa_eval_20251217_test_real",
    "benchmark_name": "waa",
    "model_id": "openai-api",
    "num_tasks": 10,
    "num_success": 0,
    "success_rate": 0.0,
    "avg_steps": 3.5,
    "avg_time_seconds": 9.87,
    "tasks": [
      {
        "task_id": "browser_1",
        "success": false,
        "score": 0.0,
        "num_steps": 3,
        "error": null
      }
    ]
  }
]
```

### GET `/api/benchmark/tasks/{run_name}/{task_id}`
Returns detailed execution trace for a specific task.

**Response:**
```json
{
  "task_id": "browser_1",
  "success": false,
  "steps": [
    {
      "step_idx": 0,
      "screenshot_path": "screenshots/step_000.png",
      "action": {
        "type": "click",
        "target_node_id": "2",
        "raw_action": {
          "response": "ACTION: CLICK(2)"
        }
      },
      "reasoning": null,
      "timestamp": 1765919453.086115
    }
  ]
}
```

### GET `/api/benchmark/screenshots/{run_name}/{task_id}/screenshots/{filename}`
Serves screenshot PNG files.

## Testing

```bash
# Run tests
cd /path/to/openadapt-viewer
python test_minimal_viewer.py
```

Tests verify:
- ✅ HTML structure is valid
- ✅ Line count is under 650 lines
- ✅ Generator works with real data
- ✅ Data structure matches expected format

## Extending the Viewer

### Adding Live Progress (Iteration 2)

Add polling to the Alpine.js component:

```javascript
init() {
    this.loadRuns();
    // Poll every 5 seconds if a run is in progress
    setInterval(() => this.pollIfRunning(), 5000);
},

async pollIfRunning() {
    const response = await fetch('/api/benchmark/status');
    const status = await response.json();
    if (status.running) {
        this.progress = status.tasks_completed / status.tasks_total;
        // Reload runs when complete
        if (status.tasks_completed === status.tasks_total) {
            await this.loadRuns();
        }
    }
}
```

Add progress UI:

```html
<div x-show="running" class="progress-section">
    <h3>Running Evaluation</h3>
    <div class="progress-bar">
        <div class="progress-fill" :style="`width: ${progress * 100}%`"></div>
    </div>
    <div x-text="`${tasksCompleted} / ${tasksTotal} tasks`"></div>
</div>
```

### Adding Domain Filtering (Iteration 3)

Add filter state:

```javascript
data() {
    return {
        // ... existing state
        selectedDomain: 'all',
        domains: []
    }
},

selectRun() {
    this.selectedRun = this.runs.find(r => r.run_name === this.selectedRunId);
    // Extract unique domains
    this.domains = [...new Set(
        this.selectedRun.tasks.map(t => this.getDomain(t.task_id))
    )];
},

get filteredTasks() {
    if (this.selectedDomain === 'all') {
        return this.selectedRun?.tasks || [];
    }
    return (this.selectedRun?.tasks || []).filter(
        t => this.getDomain(t.task_id) === this.selectedDomain
    );
}
```

Update template to use `filteredTasks`:

```html
<select x-model="selectedDomain">
    <option value="all">All Domains</option>
    <template x-for="domain in domains" :key="domain">
        <option :value="domain" x-text="domain"></option>
    </template>
</select>

<template x-for="task in filteredTasks" :key="task.task_id">
    <!-- task item -->
</template>
```

### Adding Charts (Iteration 4)

Include Chart.js:

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
```

Add chart component:

```javascript
mounted() {
    this.$nextTick(() => this.renderChart());
},

renderChart() {
    const ctx = this.$refs.successChart;
    new Chart(ctx, {
        type: 'line',
        data: {
            labels: this.runs.map(r => r.run_name),
            datasets: [{
                label: 'Success Rate',
                data: this.runs.map(r => r.success_rate * 100),
                borderColor: '#60a5fa',
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            scales: {
                y: {
                    beginAtZero: true,
                    max: 100
                }
            }
        }
    });
}
```

Add canvas element:

```html
<div class="chart-section">
    <h3>Success Rate Trend</h3>
    <canvas x-ref="successChart" width="400" height="200"></canvas>
</div>
```

## Design Decisions

### Why Single File?
- Easy to deploy (just copy one file)
- Easy to share (send one HTML file)
- Works offline (with embedded data)
- Forces simplicity (can't hide complexity in modules)

### Why Alpine.js?
- No build step required
- Simple reactivity model
- Small footprint (~15KB)
- Already used in OpenAdapt ecosystem

### Why No Frameworks?
- Faster iteration (no compile step)
- Easier to debug (view source = actual code)
- Lower barrier to entry (plain HTML/JS/CSS)
- Smaller attack surface

### Why Progressive Enhancement?
- Works immediately (embedded data)
- Enhances when server available (API calls)
- Degrades gracefully (fallback to static)
- Clear upgrade path

## File Structure

```
viewers/benchmark/
├── minimal_viewer.html  # Single-file viewer (589 lines)
├── generator.py         # Python generator for embedding data
└── README.md           # This file
```

## Success Criteria

✅ Single HTML file under 650 lines (589 lines)
✅ Works with file:// protocol (embedded data mode)
✅ Works with HTTP API (dynamic mode)
✅ Shows real benchmark data
✅ Easy to understand code
✅ Clear extension points
✅ Tested with actual benchmark runs

## Next Steps

See [MINIMAL_BENCHMARK_VIEWER.md](../../MINIMAL_BENCHMARK_VIEWER.md) for:
- Detailed iteration plan
- Feature roadmap
- Testing strategy
- Integration examples
