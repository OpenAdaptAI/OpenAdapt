# Azure ML Live Monitoring

Real-time monitoring of Azure ML WAA evaluation jobs with local live viewer integration.

## Overview

This feature connects Azure ML job logs to a local live viewer, enabling:
- Real-time log streaming from Azure ML jobs
- Live progress tracking with detailed task/step information
- Auto-refreshing viewer with current evaluation state
- No need to wait for job completion to see results

## Architecture

```
Azure ML Job (running evaluation)
    ↓ (az ml job stream)
AzureWAAOrchestrator.monitor_job()
    ↓ (parses logs)
AzureJobLogParser
    ↓ (extracts progress)
LiveEvaluationTracker
    ↓ (writes JSON)
benchmark_live.json
    ↓ (HTTP API)
Flask API Server (/api/benchmark-live)
    ↓ (polls every 2s)
Benchmark Viewer (auto-refresh)
```

## Quick Start

### 1. Start Azure Evaluation

```bash
# Run Azure evaluation (creates job)
uv run python -m openadapt_evals.benchmarks.cli azure \
    --workers 1 \
    --waa-path /path/to/WAA \
    --task-ids notepad_1,browser_1

# Note the job name from output: waa-waa3718w0-1768743963-20a88242
```

### 2. Monitor the Job

In a separate terminal:

```bash
# Stream logs and update live tracking file
uv run python -m openadapt_evals.benchmarks.cli azure-monitor \
    --job-name waa-waa3718w0-1768743963-20a88242 \
    --output benchmark_live.json
```

### 3. View Live Progress

In another terminal:

```bash
# Install viewer dependencies
uv sync --extra viewer

# Start live API server
uv run python -m openadapt_evals.benchmarks.live_api \
    --live-file benchmark_live.json \
    --port 5001

# Open browser to http://localhost:5001
```

The viewer will automatically detect the live API and enable auto-refresh mode with a "LIVE" indicator.

## Components

### AzureJobLogParser

Parses Azure ML job logs to extract:
- Task start/completion (pattern: `Task 1/10: notepad_1`)
- Step execution (pattern: `Step 3: CLICK`)
- Task results (pattern: `Task notepad_1: SUCCESS`)
- Error messages

See `openadapt_evals/benchmarks/azure.py` for implementation.

### LiveEvaluationTracker

Writes real-time progress to `benchmark_live.json`:

```json
{
  "status": "running",
  "total_tasks": 10,
  "tasks_completed": 3,
  "current_task": {
    "task_id": "notepad_1",
    "instruction": "Azure ML Task notepad_1",
    "domain": "azure",
    "steps": [
      {
        "step_idx": 0,
        "action": {"type": "click", "x": 0.5, "y": 0.5},
        "reasoning": null
      }
    ],
    "result": null
  }
}
```

See `openadapt_evals/benchmarks/live_tracker.py`.

### Live API Server

Flask server exposing `/api/benchmark-live` endpoint:

```bash
# Start server
uv run python -m openadapt_evals.benchmarks.live_api

# Check status
curl http://localhost:5001/api/benchmark-live
```

See `openadapt_evals/benchmarks/live_api.py`.

### Viewer Auto-Refresh

The benchmark viewer automatically detects live API availability and:
- Polls `/api/benchmark-live` every 2 seconds
- Updates stats (total tasks, completed, failed)
- Shows pulsing "LIVE" indicator
- Stops polling when evaluation completes

See `openadapt_evals/benchmarks/viewer.py`.

## Log Parsing Patterns

The log parser recognizes these patterns:

| Pattern | Example | Extracted Data |
|---------|---------|----------------|
| Task start | `Task 1/10: notepad_1` | task_idx=1, total_tasks=10, task_id="notepad_1" |
| Step | `Step 3: CLICK` | step_idx=3, action_type="CLICK" |
| Task result | `Task notepad_1: SUCCESS` | task_id="notepad_1", success=True |
| Error | `ERROR: Connection failed` | message="ERROR: Connection failed" |

## CLI Commands

### azure-monitor

Monitor an existing Azure ML job:

```bash
uv run python -m openadapt_evals.benchmarks.cli azure-monitor \
    --job-name JOB_NAME \
    --output benchmark_live.json
```

Options:
- `--job-name`: Azure ML job name (required)
- `--output`: Live tracking file path (default: benchmark_live.json)

### live_api

Start the live API server:

```bash
uv run python -m openadapt_evals.benchmarks.live_api \
    --port 5001 \
    --live-file benchmark_live.json \
    --debug
```

Options:
- `--port`: Server port (default: 5001)
- `--host`: Host to bind to (default: 127.0.0.1)
- `--live-file`: Path to benchmark_live.json
- `--debug`: Enable Flask debug mode

## Environment Variables

Required for Azure ML access:
- `AZURE_SUBSCRIPTION_ID`: Azure subscription ID
- `AZURE_ML_RESOURCE_GROUP`: Resource group name
- `AZURE_ML_WORKSPACE_NAME`: ML workspace name

Authentication (one of):
- `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` (service principal)
- Azure CLI login (`az login`)

## Troubleshooting

### "No live tracking data available"

The `benchmark_live.json` file doesn't exist or is empty. Ensure:
1. `azure-monitor` is running
2. `--output` path is correct
3. Azure ML job is producing logs

### Viewer not auto-refreshing

The viewer couldn't connect to `/api/benchmark-live`. Check:
1. Flask server is running (`live_api.py`)
2. Server port matches viewer URL
3. CORS is enabled (automatically handled by Flask-CORS)

### Logs not parsing

Azure ML job may use different log format. Update patterns in `AzureJobLogParser`:

```python
# In azure.py
TASK_START_PATTERN = re.compile(r"Your custom pattern")
```

## Advanced Usage

### Custom Log Parser

Extend `AzureJobLogParser` for custom log formats:

```python
from openadapt_evals.benchmarks.azure import AzureJobLogParser

class CustomLogParser(AzureJobLogParser):
    def parse_line(self, line: str):
        # Custom parsing logic
        if "MY_PATTERN" in line:
            return {"type": "custom", "data": ...}
        return super().parse_line(line)
```

### Programmatic Monitoring

Use the orchestrator directly:

```python
from openadapt_evals.benchmarks.azure import AzureConfig, AzureWAAOrchestrator

config = AzureConfig.from_env()
orchestrator = AzureWAAOrchestrator(config, waa_repo_path="/tmp/dummy")

orchestrator.monitor_job(
    job_name="waa-xxx-xxx",
    live_tracking_file="custom_live.json",
)
```

### Multiple Jobs

Monitor multiple jobs simultaneously:

```bash
# Terminal 1
uv run python -m openadapt_evals.benchmarks.cli azure-monitor \
    --job-name job1 --output live_job1.json

# Terminal 2
uv run python -m openadapt_evals.benchmarks.cli azure-monitor \
    --job-name job2 --output live_job2.json

# Terminal 3
uv run python -m openadapt_evals.benchmarks.live_api \
    --live-file live_job1.json --port 5001

# Terminal 4
uv run python -m openadapt_evals.benchmarks.live_api \
    --live-file live_job2.json --port 5002
```

## Integration with Existing Evaluations

The live monitoring works with both:

1. **New evaluations**: Start `azure-monitor` immediately after `azure` command
2. **Existing running jobs**: Connect to any running Azure ML job by name

No code changes needed to existing evaluation logic.

## Performance

- Log streaming: Real-time (via `az ml job stream`)
- Viewer polling: Every 2 seconds
- Live file writes: On every parsed event
- Minimal overhead: Parsing runs in background thread

## Limitations

1. **Log format dependency**: Requires specific log patterns (configurable)
2. **No screenshots**: Live tracking doesn't include screenshots (memory constraint)
3. **Single job**: One `azure-monitor` instance per job
4. **Local only**: Viewer API runs locally (not deployed to cloud)

## Future Enhancements

- [ ] WebSocket support for instant updates (no polling)
- [ ] Screenshot streaming from Azure blob storage
- [ ] Multi-job dashboard view
- [ ] Cloud-deployed viewer API
- [ ] Historical playback of completed jobs
- [ ] Real-time error notifications
- [ ] Progress charts and visualizations
- [ ] Export live data to CSV/JSON

## Examples

### Full Workflow

```bash
# 1. Start Azure evaluation
uv run python -m openadapt_evals.benchmarks.cli azure \
    --workers 1 \
    --task-ids notepad_1,browser_1,office_1 \
    --waa-path ~/WindowsAgentArena

# Output: Job submitted: waa-waa3718w0-1768743963-20a88242

# 2. Monitor in real-time (separate terminal)
uv run python -m openadapt_evals.benchmarks.cli azure-monitor \
    --job-name waa-waa3718w0-1768743963-20a88242

# 3. Start viewer API (separate terminal)
uv sync --extra viewer
uv run python -m openadapt_evals.benchmarks.live_api

# 4. Open browser to http://localhost:5001
# Watch live progress as evaluation runs!
```

### Monitor-Only Mode

If you just want to track progress without viewing:

```bash
# Stream logs to console and file
uv run python -m openadapt_evals.benchmarks.cli azure-monitor \
    --job-name waa-xxx \
    --output /tmp/live.json

# Check progress
cat /tmp/live.json | jq '.'
```

## Related Documentation

- [Azure ML Job Documentation](https://learn.microsoft.com/en-us/azure/machine-learning/)
- [LiveEvaluationTracker API](./openadapt_evals/benchmarks/live_tracker.py)
- [Benchmark Viewer](./openadapt_evals/benchmarks/viewer.py)
- [Azure Orchestrator](./openadapt_evals/benchmarks/azure.py)
