# Windows Agent Arena Evaluator Integration Analysis

**Date**: 2026-01-16
**Author**: AI Analysis
**Status**: Research Complete

## Executive Summary

This document analyzes Windows Agent Arena (WAA) evaluator code to determine the best integration approach for openadapt-evals. The goal is to **reuse WAA's existing evaluator code** rather than re-implementing from scratch.

**Key Finding**: WAA has a well-structured evaluator system with ~30+ metric functions and ~18 getter functions that can be integrated via **HTTP API calls** or **Python module import**. The recommended approach is **Option B: Add `/evaluate` endpoint** combined with **Option A: Port critical Python code** for flexibility.

---

## 1. WAA Evaluator Code Structure

### Repository Location
```
WindowsAgentArena/src/win-arena-container/client/desktop_env/
├── evaluators/
│   ├── __init__.py          # Empty stub (commented code)
│   ├── README.md            # Setup instructions
│   ├── getters/             # Data retrieval functions (~18 modules)
│   │   ├── __init__.py      # Exports all getter functions
│   │   ├── file.py          # File operations (get_vm_file, get_cache_file)
│   │   ├── general.py       # Command execution (get_vm_command_line)
│   │   ├── chrome.py        # Chrome-specific getters
│   │   ├── edge.py          # Edge browser getters
│   │   ├── vscode.py        # VSCode getters
│   │   ├── vlc.py           # VLC media player getters
│   │   ├── settings.py      # System settings getters
│   │   ├── fileexplorer.py  # File Explorer state
│   │   └── ... (18 total modules)
│   └── metrics/             # Evaluation functions (~16 modules)
│       ├── __init__.py      # Exports all metric functions
│       ├── general.py       # Core: exact_match, fuzzy_match, check_json, etc.
│       ├── basic_os.py      # OS checks: clipboard, window state, settings
│       ├── chrome.py        # Chrome verification
│       ├── edge.py          # Edge verification
│       ├── docs.py          # Document formatting checks
│       ├── libreoffice.py   # LibreOffice checks
│       ├── vscode.py        # VSCode file/config checks
│       ├── vlc.py           # VLC playback/recording checks
│       ├── gimp.py          # Image manipulation verification
│       ├── table.py         # Spreadsheet comparison
│       ├── pdf.py           # PDF page count validation
│       ├── slides.py        # PowerPoint checks
│       └── utils.py         # Shared utilities
└── envs/
    └── desktop_env.py       # Main environment with evaluate() method
```

### Core Architecture

The evaluator system follows a **getter-metric pattern**:

1. **Getters**: Retrieve current state from VM (files, clipboard, app state)
2. **Metrics**: Compare retrieved state against expected values
3. **DesktopEnv.evaluate()**: Orchestrates the evaluation flow

---

## 2. Evaluator Types and Checks

### 2.1 Getter Functions (~18 modules, ~50+ functions)

| Module | Key Functions | What They Retrieve |
|--------|--------------|-------------------|
| `file.py` | `get_vm_file()`, `get_cache_file()`, `get_content_from_vm_file()` | Files from VM, Excel content, cached files |
| `general.py` | `get_vm_command_line()`, `get_vm_terminal_output()` | Shell command output, terminal state |
| `chrome.py` | `get_bookmarks()`, `get_history()`, `get_cookies()` | Browser state |
| `edge.py` | `get_edge_bookmarks()`, `get_edge_settings()` | Edge browser configuration |
| `vscode.py` | `get_vscode_settings()`, `get_open_files()` | IDE state |
| `vlc.py` | `get_vlc_config()`, `get_recording_path()` | Media player config |
| `settings.py` | `get_timezone()`, `get_wallpaper()` | System settings |
| `fileexplorer.py` | `get_directory_tree()`, `get_sorted_files()` | File system state |
| `windows_clock.py` | `get_timer_state()`, `get_alarm_state()` | Clock app state |

**Implementation Pattern** (from `general.py`):
```python
def get_vm_command_line(env, config: Dict[str, str]):
    """Execute command on VM and return output."""
    vm_ip = env.vm_ip
    port = 5000
    command = config["command"]
    shell = config.get("shell", False)

    response = requests.post(
        f"http://{vm_ip}:{port}/execute",
        json={"command": command, "shell": shell}
    )

    if response.status_code == 200:
        return response.json()["output"]
    return None
```

### 2.2 Metric Functions (~16 modules, ~80+ functions)

| Module | Key Functions | What They Verify |
|--------|--------------|------------------|
| `general.py` | `exact_match()`, `fuzzy_match()`, `check_json()`, `check_csv()`, `diff_text_file()` | Core comparisons |
| `basic_os.py` | `is_in_vm_clipboard()`, `check_gnome_favorite_apps()`, `check_moved_jpgs()` | OS state |
| `chrome.py` | `check_bookmarks()`, `check_extensions()`, `check_search_engine()` | Browser config |
| `edge.py` | `check_edge_homepage()`, `check_default_browser()` | Edge settings |
| `docs.py` | `check_line_spacing()`, `check_equations()`, `compare_images_in_doc()` | Document formatting |
| `vscode.py` | `compare_vscode_files()`, `check_vscode_config()`, `run_test_suite()` | IDE state |
| `vlc.py` | `check_recording_started()`, `check_fullscreen()`, `check_playback_speed()` | Media state |
| `gimp.py` | `compare_structural_similarity()`, `check_brightness()`, `check_saturation()` | Image edits |
| `table.py` | `compare_table()`, `check_cell_values()` | Spreadsheet content |
| `pdf.py` | `check_pdf_page_count()` | PDF validation |
| `slides.py` | `check_slide_number()`, `check_transitions()` | Presentation state |

**Return Values**: All metrics return `float` in range `[0.0, 1.0]`:
- `1.0` = Full success
- `0.0` = Complete failure
- `0.0-1.0` = Partial credit (for continuous metrics)

**Implementation Pattern** (from `general.py`):
```python
def exact_match(result, expected, **options) -> float:
    """Compare two values for exact equality."""
    if result == expected:
        return 1.0
    return 0.0

def fuzzy_match(result, expected, threshold=0.8, **options) -> float:
    """Fuzzy string matching using RapidFuzz."""
    from rapidfuzz import fuzz
    score = fuzz.ratio(str(result), str(expected)) / 100.0
    return 1.0 if score >= threshold else score
```

---

## 3. Task Configuration Format

### 3.1 JSON Structure

Each task JSON has 5 key components:

```json
{
  "id": "366de66e-cbae-4d72-b042-26390db2b145-WOS",
  "snapshot": "notepad",
  "instruction": "Please open Notepad, create a new file named 'draft.txt'...",
  "config": {
    "setup": [...],
    "launch": ["notepad.exe"]
  },
  "evaluator": {
    "postconfig": [...],
    "func": "exact_match",
    "result": {
      "type": "vm_file",
      "path": "C:\\Users\\...\\Documents\\draft.txt"
    },
    "expected": {
      "type": "rule",
      "rules": {"match": 1.0}
    }
  }
}
```

### 3.2 Evaluator Configuration Fields

| Field | Purpose | Example |
|-------|---------|---------|
| `postconfig` | Post-task setup (e.g., open files for inspection) | `[{"type": "activate_window", "name": "Notepad"}]` |
| `func` | Metric function name(s) | `"exact_match"` or `["exact_match", "compare_text_file"]` |
| `result` | How to get actual state | `{"type": "vm_file", "path": "..."}` |
| `expected` | Expected value or rule | `{"type": "rule", "rules": {"match": 1.0}}` |
| `conj` | Conjunction for multiple metrics | `"and"` or `"or"` |
| `options` | Additional metric options | `{"threshold": 0.9}` |

### 3.3 Evaluation Flow (in `desktop_env.py`)

```python
def evaluate(self):
    # 1. Run postconfig (e.g., activate windows)
    self.setup_controller.setup(self.evaluator.get("postconfig", []))

    # 2. Handle infeasible tasks
    if self.evaluator.get("infeasible") and agent_action == "FAIL":
        return 1.0

    # 3. Get actual result from VM
    getter_func = getattr(getters, f"get_{result['type']}")
    result_state = getter_func(self, result)

    # 4. Get expected value
    expected_getter = getattr(getters, f"get_{expected['type']}")
    expected_state = expected_getter(self, expected)

    # 5. Run metric comparison
    metric_func = getattr(metrics, self.evaluator["func"])
    score = metric_func(result_state, expected_state, **self.metric_options)

    return score
```

---

## 4. Integration Options Analysis

### Option A: Port Python Evaluator Code to Our Adapter

**What It Involves**:
- Copy `evaluators/getters/` and `evaluators/metrics/` directories
- Adapt HTTP calls to use our `WAALiveAdapter.config.server_url`
- Create thin wrapper in `openadapt_evals/evaluators/`

**Pros**:
- Full control over evaluation logic
- Can run evaluators without WAA container
- No dependency on WAA server modifications
- Easy to debug and extend

**Cons**:
- Need to maintain synced copy of WAA code
- ~2000 lines of code to port
- May miss future WAA updates

**Effort**: Medium (1-2 days)

### Option B: Add `/evaluate` Endpoint to WAA Server

**What It Involves**:
- Add new endpoint to WAA's Flask server (`main.py`)
- Endpoint accepts task JSON and returns evaluation score
- Call from `WAALiveAdapter.evaluate()`

**Proposed Endpoint**:
```python
@app.route("/evaluate", methods=["POST"])
def evaluate_task():
    """Run WAA evaluator on current VM state."""
    task_config = request.json

    # Load evaluator spec from task config
    evaluator_spec = task_config.get("evaluator", {})

    # Get actual state using getter
    result_config = evaluator_spec.get("result", {})
    getter_type = result_config.get("type")
    getter_func = getattr(getters, f"get_{getter_type}")
    actual_state = getter_func(env, result_config)

    # Get expected state
    expected_config = evaluator_spec.get("expected", {})
    expected_state = get_expected_value(expected_config)

    # Run metric
    metric_name = evaluator_spec.get("func", "exact_match")
    metric_func = getattr(metrics, metric_name)
    score = metric_func(actual_state, expected_state)

    return jsonify({
        "success": score >= 1.0,
        "score": score,
        "actual": actual_state,
        "expected": expected_state
    })
```

**Pros**:
- Minimal code in openadapt-evals
- Uses WAA's battle-tested evaluators
- Automatically gets WAA updates
- Clean separation of concerns

**Cons**:
- Requires modifying WAA server
- Need to maintain WAA fork or PR upstream
- Network latency for each evaluation call

**Effort**: Low (0.5-1 day)

### Option C: Run WAA Evaluators as Subprocess

**What It Involves**:
- Create standalone evaluation script in WAA repo
- Call via subprocess from `WAALiveAdapter.evaluate()`
- Pass task config via stdin/file, get results via stdout/file

**Pros**:
- No modification to WAA server
- Uses original WAA code as-is
- Can run in Docker or locally

**Cons**:
- Subprocess overhead
- Harder to debug
- Requires WAA repo to be cloned locally

**Effort**: Medium (1 day)

### Option D: Use WAA's Docker Container Evaluation Pipeline

**What It Involves**:
- Use WAA's `lib_run_single.py` which already handles evaluation
- Swap agent with our implementation
- Results written to `result.txt`

**Pros**:
- Uses WAA's complete pipeline
- Battle-tested evaluation flow
- Includes proper snapshot management

**Cons**:
- Less control over agent execution loop
- Harder to inject demos dynamically
- Requires full WAA Docker setup
- Doesn't fit our adapter pattern well

**Effort**: High (2-3 days)

---

## 5. Recommendation

### Recommended Approach: Hybrid (Option B + Option A)

**Phase 1: Add `/evaluate` Endpoint (Immediate)**

1. Fork WAA or create PR adding `/evaluate` endpoint to `main.py`
2. Endpoint delegates to existing getters/metrics modules
3. Update `WAALiveAdapter.evaluate()` to call endpoint

**Implementation in `WAALiveAdapter`**:
```python
def evaluate(self, task: BenchmarkTask) -> BenchmarkResult:
    """Evaluate using WAA's evaluators via HTTP."""
    import requests

    # Task must have evaluator config from WAA JSON
    if not task.raw_config or "evaluator" not in task.raw_config:
        return BenchmarkResult(
            task_id=task.task_id,
            success=False,
            score=0.0,
            reason="No evaluator config in task"
        )

    resp = requests.post(
        f"{self.config.server_url}/evaluate",
        json=task.raw_config,
        timeout=60.0
    )

    if resp.status_code == 200:
        result = resp.json()
        return BenchmarkResult(
            task_id=task.task_id,
            success=result.get("score", 0.0) >= 1.0,
            score=result.get("score", 0.0),
            reason=result.get("reason"),
        )
    else:
        return BenchmarkResult(
            task_id=task.task_id,
            success=False,
            score=0.0,
            reason=f"Evaluation failed: {resp.status_code}"
        )
```

**Phase 2: Port Critical Getters/Metrics (As Needed)**

For offline evaluation or testing without VM:
1. Port `metrics/general.py` (core comparison functions)
2. Port `getters/file.py` (file operations)
3. Create `openadapt_evals/evaluators/` module

This gives us flexibility to run basic evaluations without WAA server.

---

## 6. Minimum Change for Basic Evaluation

### Step 1: Load Task JSON with Evaluator Config

Currently `WAALiveAdapter.load_task()` creates minimal tasks. Update to load full WAA JSON:

```python
def load_task(self, task_id: str) -> BenchmarkTask:
    """Load task from WAA examples directory."""
    # Parse task_id like "notepad_366de66e-cbae..."
    parts = task_id.split("_", 1)
    domain = parts[0]
    task_uuid = parts[1] if len(parts) > 1 else task_id

    # Load from WAA repo
    task_path = self.waa_examples_path / domain / f"{task_uuid}.json"
    with open(task_path) as f:
        config = json.load(f)

    return BenchmarkTask(
        task_id=task_id,
        instruction=config.get("instruction", ""),
        domain=domain,
        raw_config=config,  # Full config including evaluator
    )
```

### Step 2: Add `/evaluate` to WAA Server

Add to `WindowsAgentArena/src/win-arena-container/vm/setup/server/main.py`:

```python
# Import evaluator modules (add to top of file)
import sys
sys.path.insert(0, "/path/to/client/desktop_env")
from evaluators import getters, metrics

@app.route("/evaluate", methods=["POST"])
def evaluate_task():
    """Evaluate current VM state against task criteria."""
    config = request.json
    evaluator = config.get("evaluator", {})

    if not evaluator:
        return jsonify({"error": "No evaluator in config"}), 400

    try:
        # Get actual result
        result_spec = evaluator.get("result", {})
        result_type = result_spec.get("type", "vm_command_line")
        getter = getattr(getters, f"get_{result_type}", None)

        if getter:
            actual = getter(MockEnv(), result_spec)
        else:
            actual = None

        # Get expected value
        expected_spec = evaluator.get("expected", {})
        if expected_spec.get("type") == "rule":
            expected = expected_spec.get("rules", {}).get("match")
        else:
            expected_getter = getattr(getters, f"get_{expected_spec.get('type')}", None)
            expected = expected_getter(MockEnv(), expected_spec) if expected_getter else None

        # Run metric
        func_name = evaluator.get("func", "exact_match")
        metric_func = getattr(metrics, func_name, metrics.exact_match)
        score = metric_func(actual, expected)

        return jsonify({
            "success": score >= 1.0,
            "score": float(score),
            "actual": str(actual)[:200],
            "expected": str(expected)[:200]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

class MockEnv:
    """Minimal env object for getters that need vm_ip."""
    vm_ip = "localhost"
```

### Step 3: Update WAALiveAdapter.evaluate()

Already shown above in the recommendation section.

---

## 7. Summary

| Approach | Code Reuse | Maintainability | Effort | Recommended |
|----------|-----------|-----------------|--------|-------------|
| A: Port Python | High | Medium | Medium | Phase 2 |
| B: `/evaluate` endpoint | **Highest** | **High** | **Low** | **Phase 1** |
| C: Subprocess | High | Low | Medium | No |
| D: Docker pipeline | Highest | Low | High | No |

**Final Recommendation**:
1. **Immediate**: Add `/evaluate` endpoint to WAA server (Option B)
2. **Later**: Port critical getters/metrics for offline use (Option A subset)

This approach:
- Reuses 100% of WAA's evaluator code in Phase 1
- Requires minimal changes (~50 lines server-side, ~20 lines client-side)
- Maintains compatibility with WAA updates
- Allows future flexibility with Phase 2 porting

---

## References

- [Windows Agent Arena GitHub](https://github.com/microsoft/WindowsAgentArena)
- [WAA Project Page](https://microsoft.github.io/WindowsAgentArena/)
- [WAA Paper (arXiv)](https://arxiv.org/html/2409.08264)
- [UFO Documentation - WAA Integration](https://microsoft.github.io/UFO/benchmark/windows_agent_arena/)
