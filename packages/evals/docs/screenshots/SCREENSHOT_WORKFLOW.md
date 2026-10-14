# Screenshot Workflow Guide

Complete guide for capturing, generating, and using screenshots in benchmark evaluations.

## Overview

The screenshot system has three main components:

1. **Runtime Capture** - Automatically captures screenshots during benchmark evaluation
2. **Viewer Display** - Shows screenshots in the interactive HTML viewer
3. **Documentation Generation** - Captures screenshots of the viewer for docs/demos

## Workflow 1: Running Evaluations with Screenshots

### Step 1: Run Evaluation

Screenshots are automatically captured when you run any evaluation:

```bash
# Live evaluation (connects to WAA server on Windows VM)
uv run python -m openadapt_evals.benchmarks.cli live \
  --agent api-claude \
  --server http://vm-ip:5000 \
  --task-ids notepad_1,browser_5 \
  --max-steps 15
```

Or programmatically:

```python
from openadapt_evals import (
    WAALiveAdapter,
    ApiAgent,
    evaluate_agent_on_benchmark
)
from openadapt_evals.benchmarks.data_collection import ExecutionTraceCollector

# Create collector (handles screenshot saving)
collector = ExecutionTraceCollector(
    benchmark_name="waa-live",
    run_name="my_eval",
    model_id="claude-sonnet-4-5"
)

# Create agent and adapter
agent = ApiAgent(provider="anthropic")
adapter = WAALiveAdapter(server_url="http://vm:5000")

# Evaluate (screenshots captured automatically)
results = evaluate_agent_on_benchmark(
    agent=agent,
    adapter=adapter,
    trace_collector=collector,
    task_ids=["notepad_1"],
    max_steps=15
)
```

### Step 2: Verify Screenshots Were Captured

```bash
# Check screenshots directory
ls benchmark_results/{run_name}/tasks/{task_id}/screenshots/

# Should see:
# step_000.png
# step_001.png
# step_002.png
# ...
```

Each screenshot represents the state before an action is taken.

### Step 3: Generate Viewer

```bash
# Via CLI (recommended)
uv run python -m openadapt_evals.benchmarks.cli view --run-name {run_name}

# Or programmatically
python -c "
from openadapt_evals.benchmarks.viewer import generate_benchmark_viewer
from pathlib import Path

generate_benchmark_viewer(
    benchmark_dir=Path('benchmark_results/{run_name}'),
    output_path=Path('benchmark_results/{run_name}/viewer.html')
)
"
```

### Step 4: View Results

```bash
# Open in browser
open benchmark_results/{run_name}/viewer.html

# Or use Python's built-in HTTP server
cd benchmark_results/{run_name}
python -m http.server 8000
# Visit http://localhost:8000/viewer.html
```

The viewer will:
- Load screenshots using relative paths
- Display them as you navigate through steps
- Show click markers for actions with coordinates
- Play/pause step-by-step replay

---

## Workflow 2: Generating Documentation Screenshots

### Use Case

You want to capture screenshots of the viewer itself for:
- README.md screenshots
- Documentation
- Blog posts
- Presentations

### Step 1: Install Playwright

```bash
pip install playwright
playwright install chromium
```

### Step 2: Generate Viewer Screenshots

```bash
# Capture all viewports and states
python -m openadapt_evals.benchmarks.auto_screenshot \
  --html-path benchmark_results/{run_name}/viewer.html \
  --output-dir docs/screenshots \
  --viewports desktop tablet mobile \
  --states overview task_detail log_expanded log_collapsed
```

This generates:
```
docs/screenshots/
├── desktop_overview.png
├── desktop_task_detail.png
├── desktop_log_expanded.png
├── desktop_log_collapsed.png
├── tablet_overview.png
├── tablet_task_detail.png
├── tablet_log_expanded.png
├── tablet_log_collapsed.png
├── mobile_overview.png
├── mobile_task_detail.png
├── mobile_log_expanded.png
└── mobile_log_collapsed.png
```

### Step 3: Use in Documentation

```markdown
# README.md

## Benchmark Viewer

![Benchmark Viewer](docs/screenshots/desktop_task_detail.png)

The viewer provides step-by-step replay of benchmark executions.
```

---

## Workflow 3: Generating Embedded Screenshots

### Use Case

Create a fully standalone HTML viewer that includes screenshots as base64 data URLs.

**Pros**: Single file, easy to share, no external dependencies
**Cons**: Large file size (4MB+ for 5 screenshots)

### Generate with Embedding

```bash
# Via Python
python -c "
from openadapt_evals.benchmarks.viewer import generate_benchmark_viewer
from pathlib import Path

generate_benchmark_viewer(
    benchmark_dir=Path('benchmark_results/{run_name}'),
    output_path=Path('benchmark_results/{run_name}/viewer_standalone.html'),
    embed_screenshots=True  # <- Enable base64 embedding
)
"
```

The resulting HTML file can be:
- Emailed as a single attachment
- Uploaded to cloud storage
- Shared via Slack/Teams
- Viewed offline without screenshots directory

---

## Directory Structure

After running an evaluation with screenshots:

```
benchmark_results/
└── {run_name}/
    ├── metadata.json              # Benchmark config, model info
    ├── summary.json               # Aggregate metrics
    ├── viewer.html                # Interactive viewer (relative paths)
    └── tasks/
        ├── task_001/
        │   ├── task.json          # Task definition
        │   ├── execution.json     # Execution trace
        │   └── screenshots/
        │       ├── step_000.png   # Before step 0
        │       ├── step_001.png   # Before step 1
        │       └── ...
        └── task_002/
            └── ...
```

---

## Screenshot Capture Details

### When Are Screenshots Captured?

Screenshots are captured at each step by `ExecutionTraceCollector`:

1. Agent receives observation (includes screenshot)
2. Agent decides action
3. **Before action is executed**, screenshot is saved
4. Action is executed
5. Next observation retrieved (with new screenshot)

This means:
- `step_000.png` = state before first action
- `step_001.png` = state before second action
- Last screenshot = final state before task completion

### Screenshot Format

- **Format**: PNG
- **Size**: Varies by screen resolution (typically 500KB-1MB)
- **Source**: From `BenchmarkObservation.screenshot` (base64 PNG data)
- **Encoding**: Base64 decoded to bytes before saving

### Screenshot Path Resolution

The viewer uses relative paths by default:

```javascript
// In viewer.html
img.src = "tasks/notepad_1/screenshots/step_000.png"
```

This assumes:
```
viewer.html
tasks/
  notepad_1/
    screenshots/
      step_000.png
```

If you move `viewer.html`, screenshots won't load unless you:
1. Move the entire `tasks/` directory too, OR
2. Regenerate with `embed_screenshots=True`

---

## Troubleshooting

### Problem: Viewer shows "No screenshots available"

**Diagnosis**:
```bash
# Check if screenshots exist
ls benchmark_results/{run_name}/tasks/{task_id}/screenshots/

# Check if execution.json references screenshots
cat benchmark_results/{run_name}/tasks/{task_id}/execution.json | grep screenshot_path
```

**Solutions**:

1. **Screenshots weren't captured** - Ensure `ExecutionTraceCollector` was passed:
   ```python
   results = evaluate_agent_on_benchmark(
       agent=agent,
       adapter=adapter,
       trace_collector=collector,  # <- Don't forget this!
   )
   ```

2. **Adapter doesn't provide screenshots** - Check adapter implementation:
   ```python
   # Adapter must include screenshot in observations
   observation = BenchmarkObservation(
       screenshot=screenshot_bytes,  # <- PNG bytes, not base64
       ...
   )
   ```

3. **Path mismatch** - Regenerate viewer from correct directory:
   ```bash
   cd /path/to/benchmark_results/{run_name}
   python -m openadapt_evals.benchmarks.viewer \
     --benchmark-dir . \
     --output-path viewer.html
   ```

### Problem: auto_screenshot.py fails with "Playwright not installed"

**Solution**:
```bash
pip install playwright
playwright install chromium
```

### Problem: Screenshots are blank/black

**Possible causes**:
1. **WAA server not returning screenshots** - Check server logs
2. **Observation screenshot is None** - Agent/adapter issue
3. **Screenshot corruption** - Check PNG file validity:
   ```bash
   file benchmark_results/{run_name}/tasks/{task_id}/screenshots/step_000.png
   # Should say: PNG image data, ...
   ```

### Problem: Viewer loads but screenshots don't display

**Diagnosis**:
```bash
# Open browser developer console (F12)
# Check for 404 errors on image requests
```

**Solutions**:
1. **Relative path issue** - Open viewer from correct directory:
   ```bash
   cd benchmark_results/{run_name}
   python -m http.server 8000
   # NOT: cd benchmark_results && python -m http.server 8000
   ```

2. **Use embedded screenshots**:
   ```bash
   python -m openadapt_evals.benchmarks.viewer \
     --benchmark-dir benchmark_results/{run_name} \
     --embed-screenshots
   ```

---

## Advanced Usage

### Selective Screenshot Capture

Capture screenshots only for specific steps:

```python
# In custom collector
class SelectiveCollector(ExecutionTraceCollector):
    def record_step(self, step_idx, observation, action, reasoning=None):
        # Only save every 5th screenshot
        if step_idx % 5 == 0:
            super().record_step(step_idx, observation, action, reasoning)
```

### Custom Screenshot Processing

Apply transformations before saving:

```python
from PIL import Image
import io

def save_screenshot_with_resize(screenshot_bytes, path, max_width=800):
    """Save screenshot with max width constraint."""
    img = Image.open(io.BytesIO(screenshot_bytes))

    # Resize if too wide
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    img.save(path, "PNG", optimize=True)
```

### Generate Screenshots Programmatically

```python
from openadapt_evals.benchmarks.auto_screenshot import generate_screenshots

screenshots = generate_screenshots(
    html_path="benchmark_results/my_run/viewer.html",
    output_dir="docs/screenshots",
    viewports=["desktop"],
    states=["overview", "task_detail"]
)

# screenshots = {
#     'desktop': [
#         Path('docs/screenshots/desktop_overview.png'),
#         Path('docs/screenshots/desktop_task_detail.png')
#     ]
# }

print(f"Generated {len(screenshots['desktop'])} screenshots")
```

---

## Best Practices

### 1. Always Capture Screenshots in Real Evaluations

```python
# ✅ Good - captures screenshots
collector = ExecutionTraceCollector(...)
results = evaluate_agent_on_benchmark(agent, adapter, trace_collector=collector)

# ❌ Bad - no screenshots
results = evaluate_agent_on_benchmark(agent, adapter)
```

### 2. Use Relative Paths for Portability

```python
# ✅ Good - default behavior
generate_benchmark_viewer(benchmark_dir=dir, output_path=dir/"viewer.html")

# 🟡 OK - for sharing single files
generate_benchmark_viewer(..., embed_screenshots=True)
```

### 3. Verify Screenshots After Capture

```bash
# Check screenshot count
find benchmark_results/{run_name} -name "*.png" | wc -l

# Check screenshot sizes
find benchmark_results/{run_name} -name "*.png" -ls
```

### 4. Use auto_screenshot.py for Documentation

```bash
# Generate viewer screenshots for README
python -m openadapt_evals.benchmarks.auto_screenshot \
  --html-path benchmark_results/best_run/viewer.html \
  --output-dir docs/screenshots \
  --viewports desktop \
  --states task_detail
```

### 5. Clean Up Old Results

```bash
# Remove old benchmark results (keep last 5)
ls -t benchmark_results | tail -n +6 | xargs -I {} rm -rf benchmark_results/{}
```

---

## Examples

### Example 1: Full Evaluation with Viewer

```bash
#!/bin/bash
# evaluate_and_view.sh

# 1. Run evaluation
RUN_NAME="waa_eval_$(date +%Y%m%d_%H%M%S)"

uv run python -m openadapt_evals.benchmarks.cli live \
  --agent api-claude \
  --server http://192.168.1.100:5000 \
  --task-ids notepad_1,notepad_2,browser_5 \
  --max-steps 15 \
  --run-name "$RUN_NAME"

# 2. Generate viewer
uv run python -m openadapt_evals.benchmarks.cli view --run-name "$RUN_NAME"

# 3. Open in browser
open "benchmark_results/$RUN_NAME/viewer.html"
```

### Example 2: Generate Documentation Screenshots

```bash
#!/bin/bash
# generate_docs_screenshots.sh

# Find most recent evaluation
RUN_NAME=$(ls -t benchmark_results | head -1)

echo "Generating screenshots for: $RUN_NAME"

# Capture viewer screenshots
python -m openadapt_evals.benchmarks.auto_screenshot \
  --html-path "benchmark_results/$RUN_NAME/viewer.html" \
  --output-dir docs/screenshots \
  --viewports desktop tablet mobile \
  --states overview task_detail log_expanded

echo "Screenshots saved to: docs/screenshots/"
ls -lh docs/screenshots/
```

### Example 3: Embedded Viewer for Sharing

```python
# create_standalone_viewer.py

from openadapt_evals.benchmarks.viewer import generate_benchmark_viewer
from pathlib import Path
import sys

if len(sys.argv) < 2:
    print("Usage: python create_standalone_viewer.py <run_name>")
    sys.exit(1)

run_name = sys.argv[1]
benchmark_dir = Path("benchmark_results") / run_name
output_path = benchmark_dir / "viewer_standalone.html"

print(f"Generating standalone viewer for: {run_name}")

generate_benchmark_viewer(
    benchmark_dir=benchmark_dir,
    output_path=output_path,
    embed_screenshots=True
)

print(f"Standalone viewer created: {output_path}")
print(f"File size: {output_path.stat().st_size / 1024 / 1024:.1f} MB")
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Run evaluation with screenshots | `uv run python -m openadapt_evals.benchmarks.cli live --agent api-claude --server http://vm:5000 --task-ids task_1` |
| Generate viewer | `uv run python -m openadapt_evals.benchmarks.cli view --run-name {run_name}` |
| Open viewer | `open benchmark_results/{run_name}/viewer.html` |
| Capture viewer screenshots | `python -m openadapt_evals.benchmarks.auto_screenshot --html-path viewer.html --output-dir screenshots` |
| Generate standalone viewer | `python -m openadapt_evals.benchmarks.viewer --benchmark-dir {dir} --embed-screenshots` |
| Verify screenshots | `find benchmark_results/{run_name} -name "*.png"` |

---

## Related Documentation

- [SCREENSHOT_TOOLING_REVIEW.md](../SCREENSHOT_TOOLING_REVIEW.md) - Technical review of screenshot infrastructure
- [PR #6](https://github.com/OpenAdaptAI/openadapt-evals/pull/6) - Original screenshot validation PR
- [CLAUDE.md](../CLAUDE.md) - Full documentation for Claude Code agents

---

## Support

If you encounter issues with screenshots:

1. Check [Troubleshooting](#troubleshooting) section above
2. Verify screenshot infrastructure: `python -m openadapt_evals.benchmarks.auto_screenshot --help`
3. Review [SCREENSHOT_TOOLING_REVIEW.md](../SCREENSHOT_TOOLING_REVIEW.md) for technical details
4. Open an issue on GitHub with:
   - Run name
   - Task IDs
   - Screenshot count
   - Error messages
