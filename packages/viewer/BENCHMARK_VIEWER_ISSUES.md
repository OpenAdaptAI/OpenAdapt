# Benchmark Viewer: User-Facing Issues

This document explains the specific issues the user is experiencing when viewing `http://localhost:8765/benchmark.html`.

---

## Issue 1: "No Evaluation Running"

### What the User Sees

```
┌─────────────────────────────────────────────────────────┐
│ Live Evaluation                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  No evaluation running                                  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### What's Actually Happening

The user has an Azure evaluation running (agent aace3b9) via:

```bash
uv run python -m openadapt_evals.benchmarks.cli azure \
  --workers 10 \
  --waa-path /path/to/WindowsAgentArena \
  --experiment-name waa-30tasks-demo-fix-validation
```

**Process is running** (confirmed by `ps aux`):
```
youruser 76824  python3 -m openadapt_evals.benchmarks.cli azure --workers 10
```

### Why It's Not Showing

**Root cause**: Different tracking systems in different packages

```
openadapt-evals CLI
  └─ Writes to: live_eval_state.json
  └─ Location: openadapt-evals working directory

openadapt-ml viewer
  └─ Reads from: benchmark_live.json
  └─ Location: /path/to/openadapt-ml/training_output/current/
```

**Current benchmark_live.json content** (last updated Jan 9):

```json
{
  "status": "setup",
  "timestamp": "2026-01-09T12:12:53.252452",
  "tasks_completed": 0,
  "total_tasks": 0,
  "phase": "initializing",
  "detail": "Connecting to Azure VM..."
}
```

This is **8 days old** and stuck in "setup" phase. The viewer sees:
- Status: "setup" → renders as "idle" → shows "No evaluation running"

### How to Fix

**Option A: Quick fix (manual bridge)**

While Azure eval is running, update benchmark_live.json:

```bash
# In openadapt-evals runner, add:
import json
from pathlib import Path

live_state = {
    "status": "running",
    "timestamp": datetime.now().isoformat(),
    "tasks_completed": 5,
    "total_tasks": 30,
    "current_task": {
        "task_id": task.task_id,
        "instruction": task.instruction,
        "domain": task.domain,
        "steps": [...]
    }
}

benchmark_live_path = Path("/path/to/openadapt-ml/training_output/current/benchmark_live.json")
benchmark_live_path.write_text(json.dumps(live_state, indent=2))
```

**Option B: Proper fix (unified tracking)**

Make LiveEvaluationTracker write to both files:

```python
class LiveEvaluationTracker:
    def __init__(self, output_dir: Path):
        self.live_state_path = output_dir / "live_eval_state.json"

        # ADDED: Also write to openadapt-ml location
        self.benchmark_live_path = Path("/path/to/openadapt-ml/training_output/current/benchmark_live.json")

    def update_state(self, state: dict):
        # Write to both locations
        self.live_state_path.write_text(json.dumps(state))
        self.benchmark_live_path.write_text(json.dumps(state))
```

---

## Issue 2: Mock Data Warning

### What the User Sees

```
┌─────────────────────────────────────────────────────────┐
│  ⚠️ WARNING                                             │
│                                                         │
│  Mock Data - Simulated Results Only                     │
│                                                         │
│  This benchmark run uses simulated mock data for        │
│  pipeline testing and development.                      │
└─────────────────────────────────────────────────────────┘
```

### Why It's Confusing

1. **Shows on ALL 48 runs** - not just mock runs
2. **Cannot dismiss** - always visible
3. **Not actionable** - user can't do anything about it
4. **Misleading** - some runs (like waa_eval_20260103) were real, not mock

### Detection Logic

```javascript
// Viewer checks if run_name contains "mock"
const isMock = runName.includes('mock');

if (isMock) {
    document.getElementById('mock-banner').style.display = 'block';
}
```

**Problem**: 47 out of 48 runs have "mock" in name → banner always shows

### How to Fix

**Option A: Better detection**

Check metadata.json for `is_mock` flag:

```json
{
  "benchmark_name": "waa-mock",
  "run_name": "waa-mock_eval_20260117_101209",
  "model_id": "unknown",
  "is_mock": true  // <-- explicit flag
}
```

**Option B: Remove banner**

If all recent runs are mock, the banner adds no value. Remove it.

**Option C: Make dismissible**

Add close button:

```html
<div id="mock-banner" class="mock-banner">
    <button onclick="dismissMockBanner()">×</button>
    <div class="mock-banner-content">...</div>
</div>

<script>
function dismissMockBanner() {
    document.getElementById('mock-banner').style.display = 'none';
    localStorage.setItem('mockBannerDismissed', 'true');
}

// Check if previously dismissed
if (localStorage.getItem('mockBannerDismissed') === 'true') {
    document.getElementById('mock-banner').style.display = 'none';
}
</script>
```

---

## Issue 3: "Unknown - 0%" for All Runs

### What the User Sees

```
┌─────────────────────────────────────────────────────────┐
│ Select Benchmark Run:                                   │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ unknown - 0% (waa-mock_eval_20260117_101209)       ▼│ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ Available runs:                                         │
│   • unknown - 0% (waa-mock_eval_20260117_101209)        │
│   • unknown - 0% (waa-mock_eval_20260117_101208)        │
│   • unknown - 0% (waa-mock_eval_20260117_101201)        │
│   • unknown - 0% (waa-mock_eval_20260116_213138)        │
│   ... (44 more identical entries)                       │
└─────────────────────────────────────────────────────────┘
```

### Why It's Broken

**Expected format**: `{model_id} - {success_rate}% ({run_name})`

**Actual values loaded**:

```json
{
  "model_id": "unknown",  // Not set during mock runs
  "success_rate": 0.0     // Mock runs all fail
}
```

**Result**: All 48 runs look identical → impossible to distinguish

### Actual Data Available

The data EXISTS in summary.json:

```json
{
  "benchmark_name": "waa-mock",
  "run_name": "waa-mock_eval_20260117_101209",
  "model_id": "unknown",
  "num_tasks": 3,
  "num_success": 0,
  "success_rate": 0.0,
  "avg_score": 0.5,
  "avg_steps": 2.0
}
```

**Better label**: `3 tasks, 2.0 avg steps (waa-mock_eval_20260117_101209)`

### How to Fix

**Option A: Use run timestamp**

```javascript
// Extract timestamp from run_name
const match = runName.match(/(\d{8}_\d{6})$/);
const timestamp = match ? match[1] : runName;

// Format: "Jan 17 10:12 - 0% (3 tasks)"
const label = `${formatTimestamp(timestamp)} - ${success_rate}% (${num_tasks} tasks)`;
```

**Option B: Use task count + avg steps**

```javascript
// Distinctive metric: number of tasks and avg steps
const label = `${num_tasks} tasks, ${avg_steps.toFixed(1)} steps - ${run_name}`;
```

**Option C: Set proper model_id**

In mock runner, set meaningful model_id:

```python
metadata = {
    "benchmark_name": "waa-mock",
    "run_name": f"waa-mock_eval_{timestamp}",
    "model_id": f"mock-{agent_type}",  # e.g., "mock-random", "mock-scripted"
    "created_at": datetime.now().isoformat(),
}
```

---

## Issue 4: 48 Runs in Dropdown (Overwhelming)

### What the User Sees

```
┌─────────────────────────────────────────────────────────┐
│ Select Benchmark Run: [unknown - 0% (waa-mock_eva...▼] │
└─────────────────────────────────────────────────────────┘
  ↓ Click dropdown
┌─────────────────────────────────────────────────────────┐
│ unknown - 0% (waa-mock_eval_20260117_101209)            │
│ unknown - 0% (waa-mock_eval_20260117_101208)            │
│ unknown - 0% (waa-mock_eval_20260117_101201)            │
│ unknown - 0% (waa-mock_eval_20260116_213138)            │
│ unknown - 0% (waa-mock_eval_20260116_212946)            │
│ ... (scrolls forever)                                   │
│ unknown - 0% (waa-mock_eval_20251215_094501)            │
│ unknown - 0% (waa-mock_eval_20251215_094453)            │
└─────────────────────────────────────────────────────────┘
```

### Problems

1. **Cannot see all runs** - dropdown requires scrolling
2. **Cannot filter** - no search or filter controls
3. **Cannot compare** - switching runs replaces view, no side-by-side
4. **Cognitive overload** - 48 identical-looking entries

### How to Fix

**Option A: Pagination**

```javascript
const RUNS_PER_PAGE = 10;
let currentPage = 0;

function renderRunsPage(page) {
    const start = page * RUNS_PER_PAGE;
    const end = start + RUNS_PER_PAGE;
    const visibleRuns = allRuns.slice(start, end);

    updateDropdown(visibleRuns);
    updatePagination(page, Math.ceil(allRuns.length / RUNS_PER_PAGE));
}
```

**Option B: Search/Filter**

```html
<input type="text" id="run-search" placeholder="Search runs...">
<select id="run-selector">
    <!-- Filtered results only -->
</select>

<script>
document.getElementById('run-search').addEventListener('input', (e) => {
    const query = e.target.value.toLowerCase();
    const filtered = allRuns.filter(run =>
        run.run_name.toLowerCase().includes(query) ||
        run.model_id.toLowerCase().includes(query)
    );
    updateDropdown(filtered);
});
</script>
```

**Option C: Recent runs only**

```javascript
// Show only last 10 runs by default
const recentRuns = allRuns.slice(0, 10);

// Add "Show all" button
document.getElementById('show-all-runs').addEventListener('click', () => {
    updateDropdown(allRuns);
});
```

---

## Issue 5: No Connection to Live Evaluation

### What the User Expects

When running:

```bash
uv run python -m openadapt_evals.benchmarks.cli azure --workers 10
```

The viewer should show:

```
┌─────────────────────────────────────────────────────────┐
│ Live Evaluation                                         │
├─────────────────────────────────────────────────────────┤
│  ✅ Running (5/30 tasks completed)                      │
│                                                         │
│  Current task: notepad_1                                │
│  Domain: notepad                                        │
│  Instruction: Open Notepad and type hello               │
│                                                         │
│  Steps so far: 3                                        │
│  Last action: CLICK(x=0.5, y=0.3)                       │
│                                                         │
│  [Screenshot of current state]                          │
└─────────────────────────────────────────────────────────┘
```

### What the User Actually Sees

```
┌─────────────────────────────────────────────────────────┐
│ Live Evaluation                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  No evaluation running                                  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### Architecture Problem

```
┌─────────────────────────────────────────────────────┐
│ Terminal: Azure CLI running                         │
│ Process: openadapt_evals.benchmarks.cli azure       │
└─────────────────────────────────────────────────────┘
         │
         │ writes to
         ↓
┌─────────────────────────────────────────────────────┐
│ live_eval_state.json                                │
│ (in openadapt-evals working directory)              │
└─────────────────────────────────────────────────────┘

         ❌ NOT CONNECTED ❌

┌─────────────────────────────────────────────────────┐
│ HTTP Server: localhost:8765                         │
│ polls /api/benchmark-live                           │
└─────────────────────────────────────────────────────┘
         ↑
         │ polls
         │
┌─────────────────────────────────────────────────────┐
│ Browser: benchmark.html                             │
│ Shows: "No evaluation running"                      │
└─────────────────────────────────────────────────────┘
```

### How to Fix

**Option 1: Symlink (quick fix)**

```bash
# Create symlink from viewer location to eval location
ln -sf \
  /path/to/openadapt-evals/live_eval_state.json \
  /path/to/openadapt-ml/training_output/current/benchmark_live.json
```

**Option 2: File watcher (robust)**

HTTP server watches both locations:

```python
class BenchmarkServer:
    def get_live_state(self):
        # Try openadapt-ml location first
        ml_path = Path("training_output/current/benchmark_live.json")
        if ml_path.exists():
            return json.loads(ml_path.read_text())

        # Fallback to openadapt-evals location
        evals_path = Path("../openadapt-evals/live_eval_state.json")
        if evals_path.exists():
            return json.loads(evals_path.read_text())

        # Return idle state
        return {"status": "idle"}
```

**Option 3: HTTP API (proper fix)**

openadapt-evals exposes live state via HTTP:

```python
# In openadapt-evals
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/live-state")
def get_live_state():
    return json.loads(Path("live_eval_state.json").read_text())

# Run: uvicorn server:app --port 9000
```

Then viewer polls `http://localhost:9000/api/live-state` instead of local file.

---

## Issue 6: No Visual Feedback for Running Evaluation

### What's Missing

Even if live connection works, the viewer lacks:

1. **Progress bar** - No visual progress (5/30 tasks)
2. **Task timeline** - No history of completed tasks
3. **Time estimate** - No ETA for completion
4. **Success/failure indicators** - Can't see which tasks passed/failed
5. **Screenshot stream** - No live screenshots of current task

### What Modern Viewers Show

Example: Azure ML Studio during training run:

```
┌─────────────────────────────────────────────────────────┐
│ Experiment: waa-30tasks-demo-fix-validation             │
│ Status: Running                                         │
│ Progress: ██████████░░░░░░░░░░ 50% (15/30 tasks)       │
│ Elapsed: 45m 23s                                        │
│ ETA: 45m 23s remaining                                  │
├─────────────────────────────────────────────────────────┤
│ Current Task: browser_5                                 │
│ Domain: browser                                         │
│ Steps: 7                                                │
│                                                         │
│ Recent Tasks:                                           │
│   ✅ notepad_1 (2s, 4 steps)                            │
│   ✅ notepad_2 (3s, 5 steps)                            │
│   ❌ coding_1 (15s, 15 steps, timeout)                  │
│   ✅ browser_4 (8s, 6 steps)                            │
│   🔄 browser_5 (running...)                             │
└─────────────────────────────────────────────────────────┘
```

### How to Add

**Step 1: Enhanced LiveEvaluationTracker**

```python
class LiveEvaluationTracker:
    def update_state(self, state: dict):
        # Add timing information
        state["elapsed_seconds"] = (datetime.now() - self.start_time).total_seconds()
        state["eta_seconds"] = self._calculate_eta(state)

        # Add task history
        state["completed_tasks"] = self.completed_tasks

        # Write state
        self.state_path.write_text(json.dumps(state, indent=2))
```

**Step 2: Enhanced Viewer UI**

```javascript
function renderLiveEvaluation(state) {
    const progress = state.tasks_completed / state.total_tasks * 100;
    const elapsed = formatTime(state.elapsed_seconds);
    const eta = formatTime(state.eta_seconds);

    html = `
        <div class="progress-bar">
            <div class="progress-fill" style="width: ${progress}%"></div>
        </div>
        <div class="progress-text">${state.tasks_completed}/${state.total_tasks} tasks</div>
        <div class="time-info">
            <span>Elapsed: ${elapsed}</span>
            <span>ETA: ${eta}</span>
        </div>
        <div class="task-history">
            ${state.completed_tasks.map(task => `
                <div class="task-item ${task.success ? 'success' : 'failure'}">
                    ${task.success ? '✅' : '❌'} ${task.task_id}
                    (${task.num_steps} steps, ${formatTime(task.duration)})
                </div>
            `).join('')}
        </div>
    `;
}
```

---

## Summary of User-Facing Issues

| Issue | Severity | User Impact | Fix Complexity |
|-------|----------|-------------|----------------|
| "No evaluation running" | Critical | Can't monitor live eval | Medium |
| Mock data warning | Low | Confusion, noise | Easy |
| "Unknown - 0%" runs | High | Can't identify runs | Medium |
| 48 runs overwhelming | Medium | Hard to find runs | Medium |
| No live connection | Critical | Primary use case broken | Medium |
| No visual feedback | Medium | Poor UX during eval | Hard |

**Total user-facing issues**: 6 critical/high, 2 medium, 1 low

**Recommended fix order**:
1. Live evaluation connection (Critical, 1 day)
2. Run identification labels (High, 0.5 days)
3. Filter/search for runs (Medium, 0.5 days)
4. Visual feedback (Medium, 1 day)
5. Remove mock banner (Low, 0.1 days)

**Total fix time**: ~3 days (same as rewrite time)

---

## See Also

- **BENCHMARK_VIEWER_REVIEW.md** - Full technical analysis
- **BENCHMARK_VIEWER_METRICS.md** - Complexity metrics and comparisons
