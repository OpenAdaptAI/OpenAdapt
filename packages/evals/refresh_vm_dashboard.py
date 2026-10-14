#!/usr/bin/env python3
"""Refresh the VM usage dashboard with current data.

This script gathers Azure VM information and generates a dashboard
showing what the VM is being used for, recent activity, and costs.

Usage:
    python refresh_vm_dashboard.py
    python refresh_vm_dashboard.py --vm-name waa-eval-vm --resource-group openadapt-agents
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def run_az(cmd: str, timeout: int = 60) -> dict[str, Any] | str | None:
    """Run Azure CLI command and return JSON output."""
    try:
        result = subprocess.run(
            f"az {cmd}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            print(f"Warning: Command failed: az {cmd}", file=sys.stderr)
            print(f"Error: {result.stderr}", file=sys.stderr)
            return None

        # Try to parse as JSON
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            # Return as string if not JSON
            return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"Warning: Command timed out: az {cmd}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Warning: Command error: {e}", file=sys.stderr)
        return None


def get_vm_status(vm_name: str, resource_group: str) -> dict[str, Any]:
    """Get current VM power state and details."""
    print(f"Fetching VM status for {vm_name}...")

    # Get instance view with power state
    vm_data = run_az(
        f'vm get-instance-view --name {vm_name} --resource-group {resource_group} --output json'
    )

    if not vm_data:
        return {
            "error": "Failed to fetch VM status",
            "power_state": "Unknown",
            "vm_size": "Unknown",
            "location": "Unknown",
        }

    # Extract power state from instance view
    power_state = "Unknown"
    instance_view = vm_data.get("instanceView", {}) if isinstance(vm_data, dict) else {}
    statuses = instance_view.get("statuses", [])
    for status in statuses:
        code = status.get("code", "")
        if code.startswith("PowerState/"):
            power_state = code.split("/")[-1]
            break

    # Get public IP
    ip_data = run_az(
        f'vm show --name {vm_name} --resource-group {resource_group} '
        f'--show-details --query "{{publicIp:publicIps, privateIp:privateIps}}" --output json'
    )

    public_ip = ip_data.get("publicIp", "N/A") if ip_data else "N/A"
    private_ip = ip_data.get("privateIp", "N/A") if ip_data else "N/A"

    # Get VM size and location from hardware profile
    hardware_profile = vm_data.get("hardwareProfile", {}) if isinstance(vm_data, dict) else {}
    vm_size = hardware_profile.get("vmSize", "Unknown")
    location = vm_data.get("location", "Unknown") if isinstance(vm_data, dict) else "Unknown"

    # Get creation time
    created_time = vm_data.get("timeCreated")

    return {
        "power_state": power_state,
        "vm_size": vm_size,
        "location": location,
        "public_ip": public_ip,
        "private_ip": private_ip,
        "created_time": created_time,
    }


def get_recent_activity(vm_name: str, resource_group: str, days: int = 7) -> list[dict[str, Any]]:
    """Get VM activity logs for the last N days."""
    print(f"Fetching activity logs (last {days} days)...")

    logs = run_az(
        f'monitor activity-log list --resource-group {resource_group} --offset {days}d '
        f'--query "[?contains(resourceId, \'{vm_name}\')].{{time:eventTimestamp, '
        f'operation:operationName.localizedValue, status:status.localizedValue}}" --output json',
        timeout=90,
    )

    if not logs:
        return []

    # Sort by time descending
    logs.sort(key=lambda x: x.get("time", ""), reverse=True)
    return logs[:20]  # Return last 20 events


def get_azure_ml_jobs(resource_group: str, workspace_name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Get recent Azure ML jobs."""
    print(f"Fetching Azure ML jobs from workspace {workspace_name}...")

    jobs = run_az(
        f'ml job list --resource-group {resource_group} --workspace-name {workspace_name} '
        f'--query "[].{{name:name, status:status, created:creation_context.created_at, '
        f'experiment:experiment_name}}" --output json',
        timeout=90,
    )

    if not jobs:
        return []

    # Sort by created time descending
    jobs.sort(key=lambda x: x.get("created", ""), reverse=True)
    return jobs[:limit]


def format_time_ago(timestamp_str: str) -> str:
    """Format timestamp as relative time (e.g., '2 hours ago')."""
    try:
        # Parse ISO 8601 timestamp
        if timestamp_str.endswith("Z"):
            timestamp_str = timestamp_str[:-1] + "+00:00"

        timestamp = datetime.fromisoformat(timestamp_str)
        now = datetime.now(timezone.utc)
        delta = now - timestamp

        if delta.days > 0:
            return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        else:
            return f"{delta.seconds} second{'s' if delta.seconds != 1 else ''} ago"
    except Exception:
        return timestamp_str


def generate_dashboard(
    vm_name: str,
    resource_group: str,
    workspace_name: str,
    output_file: Path,
) -> None:
    """Generate the VM usage dashboard."""
    print(f"\nGenerating VM Usage Dashboard for {vm_name}...")
    print("=" * 60)

    # Gather data
    vm_status = get_vm_status(vm_name, resource_group)
    activity_logs = get_recent_activity(vm_name, resource_group, days=7)
    ml_jobs = get_azure_ml_jobs(resource_group, workspace_name, limit=10)

    # Calculate cost estimates
    vm_hourly_cost = 0.20  # Standard_D4ds_v5 approximate cost
    power_state = vm_status.get("power_state", "Unknown")

    if power_state == "running":
        cost_today = vm_hourly_cost * 24  # If running all day
        cost_status = f"Running (${vm_hourly_cost}/hour = ${cost_today:.2f}/day)"
    elif power_state == "deallocated":
        cost_status = "Stopped (no compute costs)"
    else:
        cost_status = f"{power_state} (cost unknown)"

    # Generate markdown
    now = datetime.now(timezone.utc)

    dashboard_content = f"""# Azure VM Usage Dashboard

**VM**: {vm_name}
**Resource Group**: {resource_group}
**Location**: {vm_status.get('location', 'Unknown')}
**Size**: {vm_status.get('vm_size', 'Unknown')}

---

## Current Status

- **Power State**: {power_state}
- **Public IP**: {vm_status.get('public_ip', 'N/A')}
- **Private IP**: {vm_status.get('private_ip', 'N/A')}
- **Cost Status**: {cost_status}
- **Estimated Daily Cost (if running 24h)**: ${vm_hourly_cost * 24:.2f}

### Current Activity

"""

    # Add current activity based on recent jobs
    if ml_jobs and ml_jobs[0].get("status") in ("Running", "Queued", "Starting"):
        latest_job = ml_jobs[0]
        dashboard_content += f"""- **Active Job**: {latest_job['name']}
  - Status: {latest_job['status']}
  - Experiment: {latest_job['experiment']}
  - Created: {format_time_ago(latest_job['created'])}

"""
    elif power_state == "running":
        dashboard_content += "- Idle (VM running but no active jobs)\n\n"
    else:
        dashboard_content += "- VM is stopped\n\n"

    # Recent evaluations section
    dashboard_content += """---

## Recent Azure ML Jobs

| Job Name | Status | Experiment | Created |
|----------|--------|------------|---------|
"""

    if ml_jobs:
        for job in ml_jobs:
            dashboard_content += (
                f"| {job['name'][:40]}... | {job['status']} | "
                f"{job['experiment']} | {format_time_ago(job['created'])} |\n"
            )
    else:
        dashboard_content += "| No recent jobs found | - | - | - |\n"

    # Recent activity section
    dashboard_content += """
---

## Recent Activity (Last 7 Days)

| Time | Operation | Status |
|------|-----------|--------|
"""

    if activity_logs:
        for log in activity_logs[:15]:  # Show last 15 events
            dashboard_content += (
                f"| {format_time_ago(log['time'])} | "
                f"{log['operation']} | {log['status']} |\n"
            )
    else:
        dashboard_content += "| No recent activity found | - | - |\n"

    # Cost summary section
    dashboard_content += f"""
---

## Cost Estimates

**Pricing** (Standard_D4ds_v5 in {vm_status.get('location', 'westus2')}):
- Hourly: ${vm_hourly_cost}/hour
- Daily (24h): ${vm_hourly_cost * 24:.2f}
- Weekly (24x7): ${vm_hourly_cost * 24 * 7:.2f}
- Monthly (720h): ${vm_hourly_cost * 720:.2f}

**Note**: These are estimates. Actual costs include:
- Compute (only when VM is running)
- Storage (always charged)
- Network egress
- Other Azure services

---

## Quick Actions

### Check VM status
```bash
cd /Users/abrichr/oa/src/openadapt-evals
uv run python -m openadapt_evals.benchmarks.cli vm-status --vm-name {vm_name} --resource-group {resource_group}
```

### Start VM
```bash
uv run python -m openadapt_evals.benchmarks.cli vm-start --vm-name {vm_name} --resource-group {resource_group}
```

### Stop VM (save costs)
```bash
uv run python -m openadapt_evals.benchmarks.cli vm-stop --vm-name {vm_name} --resource-group {resource_group}
```

### Start VM + WAA server (all-in-one)
```bash
uv run python -m openadapt_evals.benchmarks.cli up --vm-name {vm_name} --resource-group {resource_group}
```

### View job details
```bash
az ml job show --name <job-name> --resource-group {resource_group} --workspace-name {workspace_name}
```

### Refresh this dashboard
```bash
cd /Users/abrichr/oa/src/openadapt-evals
python refresh_vm_dashboard.py --vm-name {vm_name} --resource-group {resource_group}
```

---

**Last Updated**: {now.strftime("%Y-%m-%d %H:%M:%S UTC")}
**Generated by**: refresh_vm_dashboard.py
"""

    # Write dashboard
    output_file.write_text(dashboard_content)
    print(f"\nDashboard generated: {output_file}")
    print(f"View with: cat {output_file}")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate Azure VM usage dashboard"
    )
    parser.add_argument(
        "--vm-name",
        type=str,
        default="waa-eval-vm",
        help="Azure VM name",
    )
    parser.add_argument(
        "--resource-group",
        type=str,
        default="openadapt-agents",
        help="Azure resource group",
    )
    parser.add_argument(
        "--workspace-name",
        type=str,
        default="openadapt-ml",
        help="Azure ML workspace name",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="VM_USAGE_DASHBOARD.md",
        help="Output file path",
    )

    args = parser.parse_args()
    output_file = Path(args.output)

    try:
        generate_dashboard(
            vm_name=args.vm_name,
            resource_group=args.resource_group,
            workspace_name=args.workspace_name,
            output_file=output_file,
        )
        return 0
    except Exception as e:
        print(f"Error generating dashboard: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
