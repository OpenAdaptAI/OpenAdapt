# Benchmark Viewer Implementation Review

**Date**: January 17, 2026
**Reviewer**: Claude Code
**Context**: User reports benchmark viewer at `http://localhost:8765/benchmark.html` shows "no evaluation running" and mock data warning despite Azure evaluation (agent aace3b9) being active.

---

## Executive Summary

1. **Current viewer is a monolithic 3,065-line HTML file (568KB) generated from 4,774-line Python module (172KB)** with extensive features, most of which are unused or broken.

2. **Root cause of "no evaluation running" issue**: Viewer loads data from 48+ mock benchmark runs but shows "unknown - 0%" for all, displaying only the most recent run which was completed (status: idle).

3. **The live Azure evaluation is NOT connected to this viewer** - it's running in openadapt-evals package with separate tracking, while this viewer polls for `benchmark_live.json` which hasn't been updated since January 9.

4. **Significant technical debt**: Inline HTML generation in Python, complex SSE/polling fallback logic, multiple overlapping panels, tight coupling between viewer and server.

5. **Recommendation**: **Deprecate and rewrite** using openadapt-evals viewer architecture (1,283 lines, focused, maintainable).

---

## Current State Analysis

### Files and Sizes

| File | Size | Lines | Purpose |
|------|------|-------|---------|
| `openadapt_ml/training/benchmark_viewer.py` | 172KB | 4,774 | Python generator with inline HTML/CSS/JS |
| `training_output/current/benchmark.html` | 568KB | 3,065 | Generated multi-run benchmark viewer |
| `openadapt_evals/benchmarks/viewer.py` | 43KB | 1,283 | Newer, focused viewer generator |

### Complexity Metrics

**benchmark_viewer.py (172KB):**
- **Functions**: 12 major functions
  - 6 panel generators (background tasks, live eval, Azure jobs, VM discovery, run benchmark)
  - 3 HTML generators (single run, multi-run, empty)
  - 3 utility functions
- **CSS Classes**: 139 unique classes
- **JavaScript Functions**: 40+ functions
- **Data Loading**: SSE (EventSource) + polling fallback + stale connection detection

**Generated benchmark.html (568KB):**
- **Embedded Data**: JSON data for 48+ benchmark runs inline
- **Polling Mechanisms**:
  - SSE connection to `/api/benchmark-sse?interval=2`
  - Fallback polling to `/api/benchmark-live` every 2s
  - Background tasks polling to `/api/tasks` every 10s
  - Stale connection detection (60s timeout)
- **UI Panels**: 5 major panels (Live Eval, Background Tasks, Azure Jobs, VM Discovery, Run Benchmark)

### Feature Inventory

#### Features Implemented

| Feature | Status | Usage | Notes |
|---------|--------|-------|-------|
| Multi-run dropdown | Working | Used | Shows 48+ mock runs, all "unknown - 0%" |
| Live evaluation panel | Broken | Unused | Shows "no evaluation running" |
| Background tasks panel | Unknown | Unused | Polls `/api/tasks` endpoint |
| Azure jobs panel | Unknown | Unused | Not visible in current view |
| VM discovery panel | Unknown | Unused | Not visible in current view |
| Run benchmark panel | Unknown | Unused | Form to trigger new benchmarks |
| SSE connection | Broken | Unused | Tries to connect, falls back to polling |
| Task list with filters | Working | Partially | Can filter by domain/status |
| Step-by-step viewer | Working | Used | Shows screenshots, actions, reasoning |
| Playback controls | Working | Used | Prev/Next navigation |

#### Features Over-Engineered

1. **SSE + Polling + Stale Detection**: Three layers of connection management for live updates, but live eval never works
2. **Multiple Panel System**: 5 separate panels (Live Eval, Tasks, Azure, VM, Run) - most never visible
3. **Multi-run Support**: Loads 48+ benchmark runs into single dropdown, but meaningful comparison is impossible
4. **Mock Data Banner**: Warns about mock data, but ALL 48 runs show 0% - not helpful

#### Features Broken

1. **Live Evaluation Display**: Shows "no evaluation running" despite active Azure eval
2. **Run Identification**: All 48 mock runs show "unknown - 0%" - no useful metadata
3. **Server Connection**: SSE fails, polling finds stale `benchmark_live.json` (last updated Jan 9)

### Data Sources

**Expected to load from:**
1. `/api/benchmark-sse?interval=2` (SSE stream) → **Fails, connection error**
2. `/api/benchmark-live` (polling fallback) → **Returns old data from Jan 9**
3. `/api/tasks` (background tasks) → **Unknown status**
4. `benchmark_results/` directory → **Loads 48+ mock runs from disk**

**Why isn't it showing the current evaluation?**

The Azure evaluation running in openadapt-evals uses a different tracking system:
- **openadapt-evals**: Uses `LiveEvaluationTracker` writing to `live_eval_state.json`
- **openadapt-ml viewer**: Expects `benchmark_live.json` in `training_output/current/`
- **Result**: No connection between the two systems

**Current benchmark_live.json status:**
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

This is 8 days old and stuck in "setup" phase.

---

## Technical Debt Analysis

### Issue 1: Inline HTML Generation in Python

**Severity**: High
**Impact**: Makes UI changes require Python changes, hard to test HTML/CSS/JS independently

```python
def _generate_benchmark_viewer_html(
    metadata: dict,
    summary: dict,
    tasks: list[dict],
    benchmark_dir: Path,
    shared_header_css: str,
    shared_header_html: str,
) -> str:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <style>
        .task-item {{ ... }}
        .task-header {{ ... }}
        # 800+ lines of inline CSS
    </style>
</head>
<body>
    # 1000+ lines of inline HTML
    <script>
        // 500+ lines of inline JavaScript
    </script>
</body>
</html>
"""
    return html
```

**Problem**:
- Can't use standard HTML/CSS/JS tooling (linters, formatters, live reload)
- Changes require Python regeneration
- Difficult to debug browser issues
- No separation of concerns

**Fix Complexity**: Hard (requires architecture change)

### Issue 2: Complex Multi-Run Support

**Severity**: Medium
**Impact**: UI becomes unusable with 48+ runs, dropdown doesn't provide meaningful comparison

**Current behavior:**
```html
<select id="run-selector">
    <option value="0">unknown - 0% (waa-mock_eval_20260117_101209)</option>
    <option value="1">unknown - 0% (waa-mock_eval_20260117_101208)</option>
    <!-- ... 46 more identical entries ... -->
</select>
```

**Problems**:
- No meaningful differentiation between runs
- All show "unknown - 0%" - metadata not loaded properly
- No side-by-side comparison (just switches active run)
- Performance issues with 48+ runs loaded into memory

**Fix Complexity**: Medium (requires better metadata extraction + UI redesign)

### Issue 3: Broken Live Evaluation Connection

**Severity**: Critical
**Impact**: Primary use case (monitoring live evaluations) doesn't work

**Architecture mismatch:**

```
openadapt-evals (running eval)
    └── LiveEvaluationTracker
        └── writes live_eval_state.json

openadapt-ml viewer (monitoring)
    └── Polls /api/benchmark-live
        └── Expects benchmark_live.json

Result: NO CONNECTION
```

**Root causes:**
1. Different file names (`live_eval_state.json` vs `benchmark_live.json`)
2. Different locations (eval runs in openadapt-evals context)
3. No HTTP server bridging the two

**Fix Complexity**: Medium (need to unify tracking or create API bridge)

### Issue 4: SSE Complexity with No Benefit

**Severity**: Low
**Impact**: Adds complexity but SSE connection never works, always falls back to polling

**Code complexity:**
```javascript
class LiveEvaluationSSEClient {
    constructor() {
        this.eventSource = null;
        this.pollingInterval = null;
        this.staleCheckInterval = null;
        this.usePolling = false;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 2000;
        this.lastHeartbeat = Date.now();
        // ... 200+ lines of connection management
    }
}
```

**Reality**: SSE connection never succeeds, immediately falls back to polling

**Fix Complexity**: Easy (remove SSE code, use polling only)

### Issue 5: Unused Panel System

**Severity**: Low
**Impact**: Code maintains 5 panels but only 2-3 are ever visible

**Panels:**
1. Live Evaluation Panel - **Broken, shows "no evaluation running"**
2. Background Tasks Panel - **Never visible**
3. Azure Jobs Panel - **Never visible**
4. VM Discovery Panel - **Never visible**
5. Run Benchmark Panel - **Never visible**

**Generated code:**
- Each panel: ~300 lines CSS + 200 lines HTML + 100 lines JS
- Total overhead: ~3,000 lines for unused features

**Fix Complexity**: Easy (remove unused panels)

---

## Root Cause Analysis

### Why Is This So Complex?

**Historical context** (inferred from code):

1. **Started simple**: Single-run viewer showing one benchmark result
2. **Added multi-run**: Dropdown to switch between runs (no comparison)
3. **Added live tracking**: SSE + polling to show running evaluations
4. **Added Azure support**: Panels for Azure jobs, VM discovery, background tasks
5. **Added run triggering**: Form to start new benchmarks from UI

**Result**: Feature accretion without refactoring

Each feature was added by extending the monolithic HTML generator, not by modularizing.

### Why Inline HTML Generation?

**Theory**: Quick prototyping turned permanent

```python
# Easy to start:
def generate_viewer():
    return f"<html>...</html>"

# Hard to maintain:
def generate_viewer():
    return f"""
        <html>
            <head>
                <style>{_get_panel_css()}</style>
            </head>
            <body>
                {_get_shared_header()}
                {_get_live_eval_panel()}
                {_get_background_tasks_panel()}
                {_get_azure_jobs_panel()}
                # ... 1000+ lines
            </body>
        </html>
    """
```

**No clear migration path** once HTML grows to 3,000+ lines.

### Why Broken Live Connection?

**Package split without coordination:**

1. `openadapt-ml/benchmarks/` → `openadapt-evals/` (code migrated)
2. Live tracking stayed in openadapt-ml viewer
3. openadapt-evals developed new LiveEvaluationTracker
4. **Result**: Two separate tracking systems, incompatible

---

## Comparison with Modern Approach

### openadapt-evals/benchmarks/viewer.py

**Size**: 1,283 lines (vs 4,774)
**Focus**: Single purpose - benchmark results visualization
**Architecture**: Clean separation

```python
def generate_benchmark_viewer(
    benchmark_dir: Path | str,
    output_path: Path | str | None = None,
) -> Path:
    # Load data
    metadata = load_benchmark_metadata(benchmark_dir)
    summary = load_benchmark_summary(benchmark_dir)
    tasks = load_task_results(benchmark_dir)

    # Generate HTML (still inline, but focused)
    html = _generate_benchmark_viewer_html(
        metadata=metadata,
        summary=summary,
        tasks=tasks,
        benchmark_dir=benchmark_dir,
    )

    output_path.write_text(html)
    return output_path
```

**Key improvements:**
1. **Single run focus**: No multi-run complexity
2. **No live tracking**: Static viewer for completed runs
3. **Simpler data model**: metadata.json + summary.json + tasks/
4. **Better metadata**: Loads full task details, not "unknown - 0%"

**Comparison table:**

| Feature | openadapt-ml viewer | openadapt-evals viewer |
|---------|---------------------|------------------------|
| File size | 172KB (4,774 lines) | 43KB (1,283 lines) |
| Generated HTML | 568KB (3,065 lines) | ~100KB (~600 lines) |
| Multi-run support | Yes (48+ runs) | No (single run) |
| Live tracking | Broken (SSE + polling) | No (static only) |
| Extra panels | 5 panels | 0 panels |
| Data loading | 3 mechanisms | 1 mechanism (file loading) |
| Maintenance burden | High | Medium |

---

## Issues Found (Categorized by Severity)

### Critical

| Issue | Impact | Fix Complexity |
|-------|--------|----------------|
| Live evaluation not connected | Primary use case broken | Medium (API bridge) |
| All runs show "unknown - 0%" | Cannot identify runs | Medium (metadata fix) |

### High

| Issue | Impact | Fix Complexity |
|-------|--------|----------------|
| Inline HTML in Python | Hard to maintain, test, debug | Hard (requires rewrite) |
| Monolithic 4,774-line module | Difficult to understand, modify | Hard (requires refactor) |

### Medium

| Issue | Impact | Fix Complexity |
|-------|--------|----------------|
| 48+ runs in dropdown | UI unusable, no comparison | Medium (redesign UI) |
| Stale benchmark_live.json | Shows 8-day-old "setup" status | Easy (delete or update) |
| SSE connection always fails | Complexity with no benefit | Easy (remove SSE) |

### Low

| Issue | Impact | Fix Complexity |
|-------|--------|----------------|
| 5 panels, 3 never visible | Code bloat | Easy (remove panels) |
| Mock data banner on all runs | Not helpful (all are mock) | Easy (remove or fix detection) |

---

## User Experience Issues

### Confusion

1. **"No evaluation running"** - User expects to see Azure eval (aace3b9), sees nothing
2. **Mock data warning** - Shows on all 48 runs, not actionable
3. **"unknown - 0%"** - Every run looks identical, can't identify which is which
4. **48 runs in dropdown** - Overwhelming, no way to filter or compare

### Missing Functionality

1. **No live Azure eval tracking** - Must check Azure portal manually
2. **No run comparison** - Can't compare two runs side-by-side
3. **No filtering** - Can't filter runs by date, success rate, or model
4. **No search** - Can't search tasks by ID or instruction

### Performance

1. **568KB HTML file** - Slow to load, parse
2. **48+ runs loaded in memory** - Browser memory usage
3. **Multiple polling intervals** - Battery drain, network overhead

---

## Recommendation: Deprecate and Rewrite

### Why Not Fix?

**Effort to fix current viewer:**
1. Refactor inline HTML → separate files (2 days)
2. Fix live evaluation connection (1 day)
3. Fix metadata loading for runs (1 day)
4. Remove unused panels (0.5 days)
5. Simplify SSE/polling (0.5 days)
6. Add run comparison UI (2 days)

**Total**: ~7 days to fix technical debt

**Effort to rewrite based on openadapt-evals:**
1. Adapt openadapt-evals viewer (0.5 days)
2. Add live tracking API bridge (1 day)
3. Add multi-run comparison (optional) (1 day)
4. Test and deploy (0.5 days)

**Total**: ~3 days for clean implementation

### Rewrite Benefits

1. **Cleaner architecture**: Build on proven openadapt-evals viewer
2. **Better maintainability**: 1,283 lines vs 4,774 lines
3. **Proper separation**: HTML/CSS/JS in separate files (future improvement)
4. **Unified tracking**: Bridge to openadapt-evals LiveEvaluationTracker
5. **No technical debt**: Fresh start without legacy complexity

### Rewrite Risks

1. **Feature loss**: Some multi-run features may need reimplementation
2. **Learning curve**: Developers must learn new codebase
3. **Migration**: Existing bookmarks/links need updating

### Migration Path

**Phase 1: Parallel deployment (Week 1)**
- Deploy new viewer at `/benchmark-v2.html`
- Keep old viewer at `/benchmark.html`
- Both accessible, users can compare

**Phase 2: Feedback and iteration (Week 2)**
- Gather user feedback
- Add missing features to new viewer
- Fix any issues

**Phase 3: Deprecation (Week 3)**
- Redirect `/benchmark.html` to `/benchmark-v2.html`
- Add deprecation notice
- Update documentation

**Phase 4: Removal (Week 4)**
- Remove old viewer code
- Clean up deprecated endpoints
- Archive for reference

---

## Immediate Actions (Short-term Fixes)

While planning rewrite, these quick fixes improve current state:

### 1. Fix "No Evaluation Running" Message

**Problem**: User runs Azure eval, viewer shows "no evaluation running"

**Quick fix**: Update `benchmark_live.json` manually during eval

```bash
# In openadapt-evals Azure runner, write status updates:
echo '{"status": "running", "current_task": {"task_id": "notepad_1", ...}}' > \
    /path/to/openadapt-ml/training_output/current/benchmark_live.json
```

**Fix Complexity**: Easy (5 minutes)
**Proper fix**: Bridge LiveEvaluationTracker to benchmark_live.json (1 day)

### 2. Fix "Unknown - 0%" Run Labels

**Problem**: All 48 runs show identical labels

**Quick fix**: Generate better metadata during mock runs

```python
# In openadapt_evals/benchmarks/runner.py
metadata = {
    "run_name": f"{benchmark_name}_{timestamp}",
    "model_id": agent.model_id or "unknown",  # Get from agent
    "created_at": datetime.now().isoformat(),
}
```

**Fix Complexity**: Easy (30 minutes)

### 3. Remove SSE Connection Code

**Problem**: Adds complexity, never works

**Quick fix**: Remove SSE client, use polling only

```javascript
// Remove 200+ lines of SSE code
// Keep only:
async function fetchLiveEvaluationPolling() { ... }
setInterval(fetchLiveEvaluationPolling, 2000);
```

**Fix Complexity**: Easy (1 hour)

---

## Long-term Vision

### Unified Viewer Architecture

```
openadapt-viewer/ (new package)
├── src/
│   ├── components/
│   │   ├── TaskList.tsx       # Reusable task list
│   │   ├── StepViewer.tsx     # Step-by-step replay
│   │   ├── MetricsPanel.tsx   # Success rate, charts
│   │   └── Filters.tsx        # Domain, status filters
│   ├── pages/
│   │   ├── BenchmarkViewer.tsx  # Completed runs
│   │   ├── LiveTracker.tsx      # Running evaluations
│   │   └── Comparison.tsx       # Side-by-side runs
│   └── api/
│       ├── benchmark-api.ts   # Load benchmark data
│       └── live-api.ts        # Live eval updates
├── public/
│   └── benchmark.html         # Entry point
└── package.json               # React + Vite
```

**Benefits:**
1. **Component reuse**: TaskList used in benchmark + live tracker
2. **Standard tooling**: React DevTools, Vite HMR, TypeScript
3. **Easy testing**: Jest + React Testing Library
4. **Real separation**: HTML/CSS/JS in separate files
5. **Scalable**: Add new views without modifying core

**Timeline**: 2-3 weeks for full React rewrite

---

## Appendix: File Locations

| File | Path | Size |
|------|------|------|
| Old viewer generator | `/path/to/openadapt-ml/openadapt_ml/training/benchmark_viewer.py` | 172KB |
| Generated viewer | `/path/to/openadapt-ml/training_output/current/benchmark.html` | 568KB |
| New viewer generator | `/path/to/openadapt-evals/openadapt_evals/benchmarks/viewer.py` | 43KB |
| Live state (old) | `/path/to/openadapt-ml/training_output/current/benchmark_live.json` | 180B |
| Benchmark results | `/path/to/openadapt-ml/benchmark_results/` | 48+ runs |

---

## Conclusion

The current benchmark viewer represents a classic case of **feature accretion without refactoring**. What started as a simple single-run viewer grew to support multi-run comparison, live tracking, Azure integration, and benchmark triggering - all added as inline HTML generation in a monolithic Python module.

**The viewer is not broken by bugs, it's broken by design.** The architecture cannot support the intended use cases:

1. **Live evaluation tracking**: Requires API bridge to openadapt-evals
2. **Multi-run comparison**: Current UI can't handle 48+ runs meaningfully
3. **Maintainability**: Inline HTML in 4,774-line Python file is unmaintainable

**Recommendation: Deprecate and rewrite** based on openadapt-evals viewer architecture. The 3-day rewrite investment is better than 7 days fixing technical debt, and results in a cleaner, more maintainable codebase.

**Immediate actions** (if rewrite is delayed):
1. Bridge LiveEvaluationTracker to benchmark_live.json (1 day)
2. Fix metadata loading for runs (0.5 days)
3. Remove SSE complexity (1 hour)

**Long-term vision**: React-based component architecture in new `openadapt-viewer` package, enabling reuse across benchmark, training, and capture viewers.
