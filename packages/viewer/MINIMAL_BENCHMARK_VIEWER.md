# Minimal Benchmark Viewer

A simple, focused benchmark viewer built for iterative development.

## Philosophy

Build incrementally:
1. **Start minimal** - Core functionality only
2. **Works immediately** - No broken features
3. **Iterates cleanly** - Easy to add features one at a time
4. **Tests well** - Each feature can be unit tested
5. **Stays simple** - Resist feature creep

## Architecture

Progressive enhancement approach:

### Level 1: Static HTML (MVP - DONE)
- Single HTML file
- Embedded JSON data or API calls
- Pure Alpine.js for reactivity
- Shows completed benchmark runs

**What works:**
- List of benchmark runs
- Task-level results (pass/fail)
- Step-by-step execution trace
- Screenshot display
- Basic metrics (success rate, avg steps)

### Level 2: Live Updates (Future)
- Poll `/api/benchmark/status` every 5s
- Show progress bar for running eval
- Update metrics in real-time
- ETA calculation

### Level 3: Advanced Features (Future)
- Cost tracking per run
- Worker status and utilization
- Performance charts (success rate over time)
- Domain filtering
- Failure clustering

### Level 4: Analysis (Future)
- Regression detection
- Task difficulty ranking
- Model comparison side-by-side

## Files

```
openadapt-viewer/
├── viewers/
│   └── benchmark/
│       ├── minimal_viewer.html  # Single-file viewer (492 lines)
│       └── generator.py         # Python generator for embedding data
```

**Single file design:**
- Easy to open (no server needed for basic use)
- Easy to test (just open in browser)
- Easy to share (copy one file)
- Can still serve via HTTP for API calls

## Data Format

The viewer consumes benchmark run data in this format:

```json
{
  "runs": [
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
}
```

Task execution details (from `execution.json`):

```json
{
  "task_id": "browser_1",
  "status": "completed",
  "steps": [
    {
      "step_number": 1,
      "timestamp": "2025-12-16T16:10:49.444888",
      "action": "CLICK(x=100, y=200)",
      "reasoning": "Click on the browser icon",
      "screenshot_path": "screenshots/step_001.png"
    }
  ]
}
```

## API Endpoints

The viewer uses these endpoints (implemented in `openadapt-ml/openadapt_ml/cloud/local.py`):

### GET `/api/benchmark/runs`
Returns list of all benchmark runs.

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
    "tasks": [...]
  }
]
```

### GET `/api/benchmark/tasks/{run_name}/{task_id}`
Returns execution details for a specific task.

**Response:**
```json
{
  "task_id": "browser_1",
  "status": "completed",
  "steps": [
    {
      "step_number": 1,
      "action": "CLICK(...)",
      "reasoning": "...",
      "screenshot_path": "screenshots/step_001.png"
    }
  ]
}
```

### GET `/api/benchmark/screenshots/{run_name}/{task_id}/screenshots/{filename}`
Serves screenshot PNG files.

## Usage

### Option 1: Serve via HTTP (recommended)

```bash
# From openadapt-ml directory
cd /path/to/openadapt-ml

# Serve with API endpoints
uv run python -m openadapt_ml.cloud.local serve --port 8765

# Open minimal viewer
open http://localhost:8765/minimal_benchmark.html
```

### Option 2: Generate standalone HTML

```bash
# From openadapt-viewer directory
cd /path/to/openadapt-viewer

# Generate viewer with embedded data
python viewers/benchmark/generator.py \
  --results-dir /path/to/openadapt-ml/benchmark_results \
  --run-name waa_eval_20251217_test_real \
  --output minimal_benchmark.html

# Open in browser (no server needed)
open minimal_benchmark.html
```

### Option 3: Direct file access

Copy viewer to a served directory:

```bash
# Copy to openadapt-ml training_output for serving
cp /path/to/openadapt-viewer/viewers/benchmark/minimal_viewer.html \
   /path/to/openadapt-ml/training_output/current/minimal_benchmark.html

# Open in browser
open http://localhost:8765/minimal_benchmark.html
```

## Extending the Viewer

The viewer is designed for easy iteration. Here's how to add features:

### Adding a new metric

1. **Update summary.json** to include the new metric
2. **Add metric card** to the HTML:

```html
<div class="metric-card">
    <div class="metric-value" x-text="selectedRun?.new_metric || 0"></div>
    <div class="metric-label">New Metric</div>
</div>
```

### Adding live progress

1. **Add polling** to Alpine.js data:

```javascript
init() {
    this.loadRuns();
    setInterval(() => this.pollProgress(), 5000);
},

async pollProgress() {
    const response = await fetch('/api/benchmark/status');
    const status = await response.json();
    if (status.running) {
        // Update progress bar
        this.progress = status.tasks_completed / status.tasks_total;
    }
}
```

2. **Add progress UI**:

```html
<div class="progress-bar" x-show="running">
    <div class="progress-fill" :style="`width: ${progress * 100}%`"></div>
</div>
```

### Adding charts

1. **Include Chart.js** from CDN:

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
```

2. **Add chart component**:

```html
<canvas x-ref="successChart"></canvas>
```

3. **Initialize in Alpine.js**:

```javascript
init() {
    this.loadRuns();
    this.$nextTick(() => this.createChart());
},

createChart() {
    new Chart(this.$refs.successChart, {
        type: 'line',
        data: {
            labels: this.runs.map(r => r.run_name),
            datasets: [{
                label: 'Success Rate',
                data: this.runs.map(r => r.success_rate * 100)
            }]
        }
    });
}
```

## Testing

### Unit testing

The viewer can be tested without a server:

```bash
# Create test data
cat > test_data.json <<EOF
{
  "runs": [
    {
      "run_name": "test_run",
      "num_tasks": 5,
      "success_rate": 0.6
    }
  ]
}
EOF

# Embed test data in viewer
python viewers/benchmark/generator.py \
  --run-name test_run \
  --output test_viewer.html

# Open and verify
open test_viewer.html
```

### Integration testing

Test with real benchmark data:

```bash
# Run a small benchmark
cd /path/to/openadapt-ml
uv run python -m openadapt_ml.benchmarks.cli test-collection --tasks 3

# Verify data structure
ls benchmark_results/*/tasks/*/

# Load in viewer
uv run python -m openadapt_ml.cloud.local serve --open
# Navigate to minimal_benchmark.html
```

## Success Criteria

The MVP meets these criteria:

- ✅ Single HTML file under 500 lines (492 lines)
- ✅ Works with file:// protocol (no server needed for embedded data)
- ✅ Works with HTTP API (loads from `/api/benchmark/runs`)
- ✅ Shows real benchmark data
- ✅ Easy to understand code (Alpine.js + vanilla JS)
- ✅ Clear extension points for features
- ✅ Tested with actual benchmark runs

## Next Steps

When adding features, follow this order:

**Iteration 2: Live Progress** (next)
- Add polling for `/api/benchmark/status`
- Show progress bar with ETA
- Auto-refresh on completion
- Estimated effort: 2 hours

**Iteration 3: Enhanced Display**
- Add cost display per run
- Add domain filtering
- Add search/filter for tasks
- Estimated effort: 3 hours

**Iteration 4: Charts**
- Success rate trend over time
- Domain breakdown chart
- Task difficulty distribution
- Estimated effort: 4 hours

**Iteration 5: Analysis**
- Failure pattern clustering
- Regression detection
- Model comparison view
- Estimated effort: 8 hours

## Design Decisions

### Why Alpine.js?
- Already used in other OpenAdapt viewers (consistency)
- No build step required
- Simple reactivity model
- Small footprint (~15KB)

### Why single file?
- Easier to debug (everything in one place)
- Can be opened without a server
- Easy to share/deploy
- Forces simplicity

### Why no frameworks?
- No build tooling needed
- Faster iteration
- Smaller surface area for bugs
- Easy for others to modify

### Why progressive enhancement?
- Ship working features immediately
- Each iteration adds value
- Easy to test each level independently
- Clear rollback points

## Comparison with Full Viewer

The full benchmark viewer (`benchmark_viewer.html`) has:
- Live progress tracking
- Worker utilization
- Cost tracking
- Domain filtering
- Charts and analysis

The minimal viewer intentionally omits these to:
- Reduce complexity
- Make code easier to understand
- Allow incremental feature addition
- Ensure core features work perfectly

Both viewers can coexist. Use minimal viewer for:
- Quick results review
- Debugging benchmark runs
- Sharing results with stakeholders
- Embedding in documentation

Use full viewer for:
- Live monitoring during runs
- Performance analysis
- Cost tracking
- Team coordination
