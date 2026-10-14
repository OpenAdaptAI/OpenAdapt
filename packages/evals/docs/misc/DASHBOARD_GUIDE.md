# Azure Resource Monitoring Dashboard

## Overview

The Azure Resource Monitoring Dashboard is an **auto-launching** web interface that provides real-time visibility into:

- **Active Azure Resources** (VMs, compute instances, containers)
- **Real-Time Costs** with breakdown by resource type
- **Live Activity** from WAA evaluations (screenshots, actions, task progress)
- **Resource Utilization** and status monitoring
- **Logs** from vm-setup, evaluations, and Azure operations
- **Controls** to stop/start expensive resources

**Critical Feature**: The dashboard **automatically launches in your browser** when you start Azure resources using `vm-setup`, `up`, or `azure` commands.

## Features

### 💰 Real-Time Cost Tracking

- **Cost per Hour**: Live calculation based on running resources
- **Daily Estimate**: 24-hour projection
- **Weekly Estimate**: 7-day projection
- **Monthly Estimate**: 30-day projection
- **Cost Breakdown**: Separate tracking for compute, storage, and network
- **Automatic Alerts**: Warning when costs exceed $5/hour

### 🖥️ Active Resources

- **VMs**: All Azure VMs with status, size, location
- **Azure ML Compute**: Compute instances used for parallel evaluation
- **Docker Containers**: WAA containers running on VMs
- **Public/Private IPs**: Network information for each resource
- **Uptime Tracking**: How long resources have been running

### 📊 Live Activity

- **Current Task**: What the agent is currently working on
- **Task Progress**: e.g., "5/154 tasks completed"
- **Recent Actions**: Last 5 actions taken by the agent
- **Action Count**: Total actions executed
- **Real-Time Logs**: Last 10 log entries with auto-scroll

### 🎮 Resource Controls

- **Stop/Start Buttons**: Control each resource directly from dashboard
- **One-Click VM Deallocation**: Quickly stop expensive VMs
- **Confirmation Dialogs**: Prevent accidental resource changes
- **Quick Actions**: Start/stop without leaving the dashboard

### 🔄 Auto-Refresh

- **5-Second Refresh**: Dashboard updates automatically
- **No Manual Refresh**: Always shows current state
- **Real-Time Updates**: Costs, status, and activity update live

## Quick Start

### Automatic Launch (Default)

The dashboard **automatically opens** when you run these commands:

```bash
# Setup WAA container - dashboard auto-launches
uv run python -m openadapt_evals.benchmarks.cli vm-setup

# Start VM and server - dashboard auto-launches
uv run python -m openadapt_evals.benchmarks.cli up

# Run parallel evaluation - dashboard auto-launches
uv run python -m openadapt_evals.benchmarks.cli azure --workers 10 --waa-path /path/to/WAA
```

**What happens:**
1. Command starts executing
2. Dashboard server starts in background (if not already running)
3. Browser automatically opens to http://127.0.0.1:5555
4. Dashboard begins showing live data

### Disable Auto-Launch

If you don't want the dashboard to auto-launch:

```bash
# Add --no-dashboard flag to any command
uv run python -m openadapt_evals.benchmarks.cli vm-setup --no-dashboard
uv run python -m openadapt_evals.benchmarks.cli up --no-dashboard
uv run python -m openadapt_evals.benchmarks.cli azure --workers 10 --no-dashboard
```

### Standalone Launch

Run the dashboard server independently:

```bash
# Run dashboard server (opens browser automatically)
python -m openadapt_evals.benchmarks.dashboard_server

# Custom port
python -m openadapt_evals.benchmarks.dashboard_server --port 8080

# Don't auto-open browser
python -m openadapt_evals.benchmarks.dashboard_server --no-open
```

### Programmatic Usage

Use the dashboard in your own Python code:

```python
from openadapt_evals.benchmarks.dashboard_server import ensure_dashboard_running

# Ensure dashboard is running and open browser
dashboard_url = ensure_dashboard_running(auto_open=True)
print(f"Dashboard available at: {dashboard_url}")

# Start without opening browser
ensure_dashboard_running(auto_open=False)

# Check if dashboard is already running
from openadapt_evals.benchmarks.dashboard_server import is_dashboard_running
if is_dashboard_running(port=5555):
    print("Dashboard is running!")
```

## Installation

Install dashboard dependencies:

```bash
# Install dashboard extra
uv sync --extra dashboard

# Or install all extras
uv sync --extra all
```

**Required Dependencies:**
- `flask>=3.0.0` - Web server framework
- `flask-cors>=4.0.0` - CORS support for API
- `requests>=2.28.0` - HTTP client for health checks

**Azure CLI Required:**
The dashboard uses Azure CLI to query resources. Ensure you have:
- Azure CLI installed (`az --version`)
- Logged in to Azure (`az login`)
- Default subscription set (`az account set --subscription <id>`)

## Dashboard Layout

### Header
```
┌─────────────────────────────────────────────────────┐
│   Azure Resource Dashboard                          │
│   Real-time monitoring of Azure resources and costs │
└─────────────────────────────────────────────────────┘
```

### Cost Alert (when costs > $5/hour)
```
┌─────────────────────────────────────────────────────┐
│ ⚠️ High Cost Alert:                                 │
│ Your resources are costing over $5/hour.            │
│ Consider stopping unused VMs to reduce costs.       │
└─────────────────────────────────────────────────────┘
```

### Summary Cards (3 columns)

**Cost Summary:**
- Per Hour: $2.50
- Today (est.): $60.00
- This Week (est.): $420.00
- This Month (est.): $1,800.00

**Active Resources:**
- Running VMs: 2
- Compute Instances: 5
- Total Resources: 7

**Current Activity:**
- Task: Open Notepad and type hello
- Progress: 5/154 tasks completed
- Actions: 23

### Resources List

```
┌─────────────────────────────────────────────────────┐
│ Resources                                            │
├─────────────────────────────────────────────────────┤
│ waa-eval-vm                          ● running      │
│ VM | Standard_D4ds_v5 | eastus | $0.20/hour        │
│ [Stop]                                              │
├─────────────────────────────────────────────────────┤
│ waa-worker-001                       ● running      │
│ COMPUTE | Standard_D4_v3 | azure-ml | $0.192/hour  │
│ [Stop]                                              │
└─────────────────────────────────────────────────────┘
```

### Recent Actions

```
┌─────────────────────────────────────────────────────┐
│ Recent Actions                                       │
├─────────────────────────────────────────────────────┤
│ Step 1: CLICK                                       │
│ Step 2: TYPE                                        │
│ Step 3: WAIT                                        │
│ Step 4: CLICK                                       │
│ Step 5: DONE                                        │
└─────────────────────────────────────────────────────┘
```

### Recent Logs

```
┌─────────────────────────────────────────────────────┐
│ Recent Logs                                          │
├─────────────────────────────────────────────────────┤
│ [INFO] Starting evaluation...                       │
│ [INFO] Task 1/154: Open Notepad                     │
│ [INFO] Agent action: CLICK(0.5, 0.5)                │
│ [INFO] Observation received                         │
│ [INFO] Task completed successfully                  │
└─────────────────────────────────────────────────────┘
```

## Cost Tracking

### How Costs Are Calculated

The dashboard queries Azure to get:

1. **VMs**: `az vm list` with instance details
2. **Compute Instances**: `az ml compute list` for Azure ML
3. **VM Size Pricing**: Mapped to East US hourly rates

**Cost Calculation:**
```
Total Cost = Compute Cost + Storage Cost + Network Cost

Where:
- Compute Cost = Sum of (running VMs × hourly rate)
- Storage Cost = Number of resources × $0.01/hour (estimate)
- Network Cost = $0.05/hour (estimate)
```

### Pricing Data (East US)

**Regular Instances:**
- Standard_D2_v3: $0.096/hour (2 vCPU, 8 GB)
- Standard_D4_v3: $0.192/hour (4 vCPU, 16 GB)
- Standard_D8_v3: $0.384/hour (8 vCPU, 32 GB)
- Standard_D4ds_v5: $0.20/hour (4 vCPU, 16 GB, local SSD)

**Spot Instances (70-80% discount):**
- Standard_D2_v3: ~$0.024/hour
- Standard_D4_v3: ~$0.048/hour
- Standard_D8_v3: ~$0.096/hour

### Cost Alerts

The dashboard displays an alert banner when:
- Hourly cost exceeds $5.00
- Helps prevent runaway costs
- Prompts to stop unused resources

## Activity Tracking

### Data Sources

**Live Tracking File** (`benchmark_live.json`):
```json
{
  "status": "running",
  "total_tasks": 154,
  "tasks_completed": 5,
  "current_task": {
    "task_id": "notepad_1",
    "instruction": "Open Notepad and type hello",
    "domain": "notepad",
    "steps": [
      {"step_idx": 1, "action": {"type": "CLICK", "x": 0.5, "y": 0.5}},
      {"step_idx": 2, "action": {"type": "TYPE", "text": "hello"}}
    ]
  }
}
```

**Log Files**:
- Recent `.log` files in working directory
- Last 10 lines displayed
- Auto-scrolling to latest

### Displayed Information

- **Current Task**: Full instruction text
- **Progress**: "5/154 tasks completed"
- **Recent Actions**: Last 5 actions with step number and type
- **Action Count**: Total actions executed
- **Logs**: Real-time log tail

## Resource Controls

### Stop Resource

Click **[Stop]** button next to any running resource:

1. Confirmation dialog appears
2. Click OK to proceed
3. Dashboard sends command:
   - VM: `uv run python -m openadapt_evals.benchmarks.cli vm-stop --vm-name <name> --no-wait`
4. Status updates on next refresh (5 seconds)

### Start Resource

Click **[Start]** button next to any stopped resource:

1. Confirmation dialog appears
2. Click OK to proceed
3. Dashboard sends command:
   - VM: `uv run python -m openadapt_evals.benchmarks.cli vm-start --vm-name <name>`
4. Status updates on next refresh (5 seconds)

### Notes

- Commands execute asynchronously
- Dashboard doesn't wait for completion
- Status updates on next refresh cycle
- Azure operations may take 1-2 minutes to complete

## Technical Details

### Server Architecture

**Flask Web Server:**
- Default Port: 5555
- Host: 127.0.0.1 (localhost only)
- Threads: Enabled for concurrent requests

**Background Operation:**
- Runs in daemon thread
- Persists across CLI commands
- Automatic cleanup on process exit

**Endpoints:**
- `/` - Dashboard HTML page
- `/api/dashboard` - JSON data API
- `/api/control` - Resource control API (POST)
- `/health` - Health check endpoint

### Data Collection

**Resource Query (every 5 seconds):**
```python
# VMs
az vm list --query "[].{name:name, status:powerState, ...}" -o json

# Azure ML Compute
az ml compute list --resource-group <rg> --workspace-name <ws> -o json
```

**Activity Tracking:**
- Reads `benchmark_live.json` file
- Parses recent `.log` files
- Extracts last 10 log lines

### Browser Integration

**Auto-Open:**
```python
import webbrowser
webbrowser.open("http://127.0.0.1:5555")
```

**Persistence:**
- Server continues running after browser closes
- Navigate back to http://127.0.0.1:5555 anytime
- No need to restart server

### Security

- **Localhost Only**: Binds to 127.0.0.1
- **No Authentication**: Assumes local trusted environment
- **No Remote Access**: Not exposed to internet
- **CORS Enabled**: For local development

## Troubleshooting

### Dashboard Doesn't Open

**Problem**: Command runs but browser doesn't open

**Solution 1**: Check if already running
```bash
# Dashboard may already be running from previous command
# Just navigate to http://127.0.0.1:5555
curl http://127.0.0.1:5555/health
```

**Solution 2**: Check port availability
```bash
# See if port 5555 is in use
lsof -i :5555

# Use different port
python -m openadapt_evals.benchmarks.dashboard_server --port 8080
```

### No Resources Shown

**Problem**: Dashboard opens but shows "No active resources"

**Possible Causes:**

1. **Azure CLI not logged in**:
   ```bash
   az login
   az account set --subscription <your-subscription-id>
   ```

2. **No running resources**:
   ```bash
   # Check Azure resources manually
   az vm list -o table
   az ml compute list --resource-group <rg> --workspace-name <ws> -o table
   ```

3. **Wrong subscription selected**:
   ```bash
   # Verify current subscription
   az account show

   # Set correct subscription
   az account set --subscription <id>
   ```

### Costs Show $0.00

**Problem**: All costs display as $0.00

**Possible Causes:**

1. **No running resources**: Only running resources incur compute costs
2. **VM size not recognized**: Unknown VM sizes default to $0.20/hour
3. **Query error**: Check console logs for Azure CLI errors

**Debug:**
```bash
# Run dashboard with debug output
python -m openadapt_evals.benchmarks.dashboard_server --debug

# Check for error messages in console
```

### Activity Not Updating

**Problem**: Current task shows "Idle" during evaluation

**Possible Causes:**

1. **No benchmark_live.json file**: File is created by LiveEvaluationTracker
2. **Wrong working directory**: Dashboard looks in current directory
3. **Evaluation not using tracker**: Ensure evaluation uses LiveEvaluationTracker

**Solution:**
```bash
# Check if tracking file exists
ls -l benchmark_live.json

# Verify file content
cat benchmark_live.json
```

### Resource Control Buttons Don't Work

**Problem**: Clicking Stop/Start shows error

**Possible Causes:**

1. **Missing CLI**: `uv` or `python` not in PATH
2. **Missing openadapt-evals**: Package not installed
3. **Permission issues**: User can't execute az commands

**Solution:**
```bash
# Test CLI manually
uv run python -m openadapt_evals.benchmarks.cli vm-status

# Check Azure permissions
az vm list
```

## Environment Variables

The dashboard respects these environment variables:

```bash
# Azure ML workspace (for compute instances)
export AZURE_ML_RESOURCE_GROUP="openadapt-agents"
export AZURE_ML_WORKSPACE_NAME="openadapt-ml"

# Azure subscription
export AZURE_SUBSCRIPTION_ID="your-subscription-id"
```

## API Reference

### GET /api/dashboard

Returns current dashboard state:

```json
{
  "resources": [
    {
      "resource_type": "vm",
      "name": "waa-eval-vm",
      "status": "running",
      "cost_per_hour": 0.20,
      "location": "eastus",
      "size": "Standard_D4ds_v5",
      "public_ip": "1.2.3.4",
      "created_time": "2026-01-18T10:00:00Z",
      "uptime_hours": 2.5
    }
  ],
  "costs": {
    "compute_per_hour": 0.20,
    "storage_per_hour": 0.01,
    "network_per_hour": 0.05,
    "total_per_hour": 0.26,
    "total_today": 6.24,
    "total_this_week": 43.68,
    "total_this_month": 187.20
  },
  "activity": {
    "current_task": "Open Notepad and type hello",
    "task_progress": "5/154 tasks completed",
    "latest_screenshot": null,
    "action_count": 23,
    "latest_actions": [
      "Step 1: CLICK",
      "Step 2: TYPE",
      "Step 3: WAIT",
      "Step 4: CLICK",
      "Step 5: DONE"
    ],
    "logs": [
      "[INFO] Starting evaluation...",
      "[INFO] Task 1/154: Open Notepad"
    ]
  },
  "last_updated": "2026-01-18T12:30:45.123456Z"
}
```

### POST /api/control

Control resources:

**Request:**
```json
{
  "action": "stop",
  "name": "waa-eval-vm",
  "type": "vm"
}
```

**Response (Success):**
```json
{
  "message": "Stop command sent to waa-eval-vm"
}
```

**Response (Error):**
```json
{
  "error": "Command failed: ..."
}
```

**Supported Actions:**
- `stop` - Stop/deallocate resource
- `start` - Start resource

**Supported Types:**
- `vm` - Azure VM
- `compute` - Azure ML compute instance (not yet implemented)

### GET /health

Health check:

```json
{
  "status": "ok"
}
```

## Examples

### Example 1: VM Setup with Dashboard

```bash
# Start VM setup - dashboard auto-launches
$ uv run python -m openadapt_evals.benchmarks.cli vm-setup

Dashboard launched: http://127.0.0.1:5555
Monitor your Azure resources and costs in real-time!

Setting up WAA container on VM 'waa-eval-vm'...
This will take 15-20 minutes on a fresh VM.

# Dashboard opens in browser showing:
# - Cost Summary: $0.00/hour (VM not running yet)
# - Active Resources: 0
# - Current Activity: Idle
```

### Example 2: Parallel Evaluation with Cost Tracking

```bash
# Run parallel evaluation
$ uv run python -m openadapt_evals.benchmarks.cli azure --workers 10 --waa-path /path/to/WAA

Dashboard launched: http://127.0.0.1:5555
Monitor your Azure resources and costs in real-time!

Setting up Azure evaluation...

# Dashboard updates to show:
# - Cost Summary: $1.92/hour (10 workers × $0.192/hour)
# - Active Resources: 10 compute instances
# - Current Activity: Task 1/154
# - Recent Actions: Step 1: CLICK, Step 2: TYPE, ...
```

### Example 3: Stopping Resources from Dashboard

```bash
# Resources running, costing $2.50/hour
# Dashboard shows:
# ┌─────────────────────────────────────┐
# │ Cost Summary                        │
# │ Per Hour: $2.50                     │
# │ ...                                 │
# └─────────────────────────────────────┘

# Click [Stop] next to waa-eval-vm
# Confirmation: "Stop waa-eval-vm?"
# Click OK

# Dashboard sends: uv run ... vm-stop --vm-name waa-eval-vm --no-wait
# Alert: "Stop command sent to waa-eval-vm"

# After 5 seconds:
# Dashboard updates to show VM as stopped
# Cost per hour drops to $0.00
```

## Best Practices

1. **Leave Dashboard Open**: Keep browser tab open during long evaluations to monitor costs and progress

2. **Check Costs Regularly**: Glance at hourly cost to catch runaway expenses early

3. **Stop Unused Resources**: Use Stop buttons immediately when evaluation completes

4. **Monitor Progress**: Watch "Current Activity" to ensure evaluation is progressing

5. **Review Logs**: Check "Recent Logs" for errors or warnings

6. **Set Up Alerts**: Watch for cost alert banner (> $5/hour)

7. **Use --no-dashboard for Scripts**: Disable auto-launch in automated scripts to prevent browser spam

## Future Enhancements

Planned improvements:

- [ ] Screenshot display in activity section
- [ ] Azure Cost Management API integration for actual costs
- [ ] Resource utilization graphs (CPU, memory, disk)
- [ ] Historical cost charts
- [ ] Email/Slack notifications for cost alerts
- [ ] Batch resource control (stop all, start all)
- [ ] Export cost reports to CSV/JSON
- [ ] Custom cost thresholds per resource type

## Support

For issues, questions, or feature requests:

- **GitHub Issues**: https://github.com/OpenAdaptAI/openadapt-evals/issues
- **Documentation**: See CLAUDE.md for complete project documentation
- **Slack**: #openadapt-evals channel

## License

MIT License - See LICENSE file for details
