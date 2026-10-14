"""VM monitoring utilities for WAA benchmark evaluation.

This module provides reusable classes for monitoring Windows VMs running WAA.
Can be used by the viewer, CLI, or as a standalone tool.

Enhanced with Azure ML job tracking, cost estimation, and activity detection.

Usage:
    # Monitor a single VM
    from openadapt_evals.infrastructure.vm_monitor import VMMonitor, VMConfig

    config = VMConfig(
        name="azure-waa-vm",
        ssh_host="172.171.112.41",
        ssh_user="azureuser",
        docker_container="winarena",
        internal_ip="localhost",
    )

    monitor = VMMonitor(config)
    status = monitor.check_status()
    print(f"VNC: {status.vnc_reachable}, WAA: {status.waa_ready}")

    # Or run continuous monitoring
    monitor.run_monitor(callback=lambda s: print(s))

    # Fetch Azure ML jobs
    jobs = fetch_azure_ml_jobs(days=7)
    print(f"Found {len(jobs)} jobs in last 7 days")

    # Calculate VM costs
    costs = calculate_vm_costs(vm_size="Standard_D4ds_v5", hours=2.5)
    print(f"Estimated cost: ${costs['total_cost_usd']:.2f}")
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class VMConfig:
    """Configuration for a WAA VM."""

    name: str
    ssh_host: str
    ssh_user: str = "azureuser"
    vnc_port: int = 8006
    waa_port: int = 5000
    qmp_port: int = 7200
    docker_container: str = "winarena"
    internal_ip: str = "localhost"

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> VMConfig:
        """Create from dictionary."""
        return cls(**data)


@dataclass
class VMStatus:
    """Status of a WAA VM at a point in time."""

    config: VMConfig
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    ssh_reachable: bool = False
    vnc_reachable: bool = False
    waa_ready: bool = False
    waa_probe_response: str | None = None
    waa_detailed_probe: dict | None = None
    container_running: bool = False
    container_logs: str | None = None
    disk_usage_gb: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "config": self.config.to_dict(),
            "timestamp": self.timestamp,
            "ssh_reachable": self.ssh_reachable,
            "vnc_reachable": self.vnc_reachable,
            "waa_ready": self.waa_ready,
            "waa_probe_response": self.waa_probe_response,
            "waa_detailed_probe": self.waa_detailed_probe,
            "container_running": self.container_running,
            "container_logs": self.container_logs,
            "disk_usage_gb": self.disk_usage_gb,
            "error": self.error,
        }


class VMMonitor:
    """Monitor a single WAA VM."""

    def __init__(self, config: VMConfig, timeout: int = 5):
        """Initialize monitor.

        Args:
            config: VM configuration.
            timeout: Timeout in seconds for network operations.
        """
        self.config = config
        self.timeout = timeout

    def check_vnc(self) -> bool:
        """Check if VNC port is reachable via SSH tunnel (localhost)."""
        try:
            # VNC is only accessible via SSH tunnel at localhost, not the public IP
            url = f"http://localhost:{self.config.vnc_port}/"
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=self.timeout):
                return True
        except (urllib.error.URLError, socket.timeout, Exception):
            return False

    def check_ssh(self) -> bool:
        """Check if SSH is reachable."""
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    f"ConnectTimeout={self.timeout}",
                    "-o",
                    "BatchMode=yes",
                    f"{self.config.ssh_user}@{self.config.ssh_host}",
                    "echo ok",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
            )
            return result.returncode == 0 and "ok" in result.stdout
        except (subprocess.TimeoutExpired, Exception):
            return False

    def check_waa_probe(self) -> tuple[bool, str | None]:
        """Check if WAA /probe endpoint responds.

        Returns:
            Tuple of (ready, response_text).
        """
        try:
            cmd = f"curl -s --connect-timeout {self.timeout} http://{self.config.internal_ip}:{self.config.waa_port}/probe"
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    f"ConnectTimeout={self.timeout}",
                    "-o",
                    "BatchMode=yes",
                    f"{self.config.ssh_user}@{self.config.ssh_host}",
                    cmd,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout + 10,
            )
            response = result.stdout.strip()
            if response and "error" not in response.lower():
                return True, response
            return False, response or None
        except (subprocess.TimeoutExpired, Exception) as e:
            return False, str(e)

    def check_waa_detailed(
        self,
        evaluate_url: str | None = None,
        layers: list[str] | None = None,
    ) -> dict | None:
        """Run a multi-layer probe against the WAA server via SSH tunnel.

        Requires the SSH tunnel to be active (WAA on localhost:{waa_port}).

        Args:
            evaluate_url: Separate URL for the score layer.
            layers: Subset of layers to probe.

        Returns:
            Probe result dict, or None on error.
        """
        try:
            from openadapt_evals.infrastructure.probe import multi_layer_probe

            server_url = f"http://localhost:{self.config.waa_port}"
            result = multi_layer_probe(
                server_url,
                timeout=self.timeout,
                layers=layers,
                evaluate_url=evaluate_url,
            )
            return result.to_dict()
        except Exception as e:
            logger.debug(f"Detailed probe failed: {e}")
            return None

    def get_container_status(self) -> tuple[bool, str | None]:
        """Check container status and get recent logs.

        Returns:
            Tuple of (running, last_log_lines).
        """
        try:
            cmd = f"docker ps -q -f name={self.config.docker_container}"
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    f"ConnectTimeout={self.timeout}",
                    "-o",
                    "BatchMode=yes",
                    f"{self.config.ssh_user}@{self.config.ssh_host}",
                    cmd,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
            )
            running = bool(result.stdout.strip())

            if running:
                # Get last few log lines
                log_cmd = f"docker logs {self.config.docker_container} 2>&1 | tail -5"
                log_result = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "StrictHostKeyChecking=no",
                        "-o",
                        f"ConnectTimeout={self.timeout}",
                        "-o",
                        "BatchMode=yes",
                        f"{self.config.ssh_user}@{self.config.ssh_host}",
                        log_cmd,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout + 10,
                )
                return True, log_result.stdout.strip()
            return False, None
        except (subprocess.TimeoutExpired, Exception) as e:
            return False, str(e)

    def get_disk_usage(self) -> float | None:
        """Get disk usage of data.img in GB."""
        try:
            # Try common paths
            paths = [
                "/home/azureuser/waa-storage/data.img",
                "/home/ubuntu/waa-storage/data.img",
                "/storage/data.img",
            ]
            for path in paths:
                cmd = f"du -b {path} 2>/dev/null | cut -f1"
                result = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "StrictHostKeyChecking=no",
                        "-o",
                        f"ConnectTimeout={self.timeout}",
                        "-o",
                        "BatchMode=yes",
                        f"{self.config.ssh_user}@{self.config.ssh_host}",
                        cmd,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout + 5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    try:
                        bytes_size = int(result.stdout.strip())
                        return round(bytes_size / (1024**3), 2)
                    except ValueError:
                        continue
            return None
        except (subprocess.TimeoutExpired, Exception):
            return None

    def check_status(self) -> VMStatus:
        """Perform full status check on the VM.

        Returns:
            VMStatus with all checks performed.
        """
        status = VMStatus(config=self.config)

        try:
            # Check VNC first (fastest, no SSH needed)
            status.vnc_reachable = self.check_vnc()

            # Check SSH
            status.ssh_reachable = self.check_ssh()

            if status.ssh_reachable:
                # Check container
                status.container_running, status.container_logs = (
                    self.get_container_status()
                )

                # Check WAA probe
                status.waa_ready, status.waa_probe_response = self.check_waa_probe()

                # Get disk usage
                status.disk_usage_gb = self.get_disk_usage()
        except Exception as e:
            status.error = str(e)

        return status

    def run_monitor(
        self,
        callback: Callable[[VMStatus], None] | None = None,
        interval: int = 30,
        stop_on_ready: bool = True,
        output_file: str | Path | None = None,
    ) -> VMStatus:
        """Run continuous monitoring until WAA is ready.

        Args:
            callback: Optional callback function called with each status update.
            interval: Seconds between checks.
            stop_on_ready: Stop monitoring when WAA is ready.
            output_file: Optional file to write status updates (JSON lines).

        Returns:
            Final VMStatus (typically when WAA is ready).
        """
        output_path = Path(output_file) if output_file else None
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            status = self.check_status()

            # Call callback if provided
            if callback:
                callback(status)

            # Write to file if provided
            if output_path:
                with open(output_path, "a") as f:
                    f.write(json.dumps(status.to_dict()) + "\n")

            # Check if we should stop
            if stop_on_ready and status.waa_ready:
                return status

            time.sleep(interval)


@dataclass
class PoolWorker:
    """A single worker in a VM pool."""

    name: str
    ip: str
    status: str = "creating"  # includes qualified-starting/qualified-ready for gated pool runs
    docker_container: str = "winarena"
    waa_ready: bool = False
    assigned_tasks: list[str] = field(default_factory=list)
    completed_tasks: list[str] = field(default_factory=list)
    current_task: str | None = None
    error: str | None = None
    qualified_run_id: str | None = None
    qualified_worker_identity_sha256: str | None = None
    qualified_egress_policy_sha256: str | None = None
    qualified_start_proof_sha256: str | None = None
    qualified_task_binding_sha256: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class VMPool:
    """A pool of worker VMs for parallel WAA evaluation."""

    pool_id: str
    created_at: str
    resource_group: str
    location: str
    vm_size: str
    workers: list[PoolWorker]
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    status: str = "active"  # active, paused
    paused_since: str | None = None  # ISO timestamp when pool was paused
    auto_pause_at: str | None = None  # ISO timestamp when pool will auto-pause
    auto_pause_hours: int = 2  # Hours until auto-pause (0 = disabled)
    ssh_username: str = "azureuser"  # SSH username for pool worker connections


class VMPoolRegistry:
    """Manage VM pools for parallel WAA evaluation."""

    REGISTRY_FILE = "benchmark_results/vm_pool_registry.json"

    def __init__(self, registry_file: str | Path | None = None):
        """Initialize pool registry.

        Args:
            registry_file: Path to JSON registry file.
        """
        self.registry_file = Path(registry_file or self.REGISTRY_FILE)
        self._pool: VMPool | None = None
        self.load()

    def load(self) -> None:
        """Load pool from registry file."""
        if self.registry_file.exists():
            try:
                with open(self.registry_file) as f:
                    data = json.load(f)
                    workers = [PoolWorker(**w) for w in data.get("workers", [])]
                    self._pool = VMPool(
                        pool_id=data["pool_id"],
                        created_at=data["created_at"],
                        resource_group=data["resource_group"],
                        location=data["location"],
                        vm_size=data["vm_size"],
                        workers=workers,
                        total_tasks=data.get("total_tasks", 0),
                        completed_tasks=data.get("completed_tasks", 0),
                        failed_tasks=data.get("failed_tasks", 0),
                        status=data.get("status", "active"),
                        paused_since=data.get("paused_since"),
                        auto_pause_at=data.get("auto_pause_at"),
                        auto_pause_hours=data.get("auto_pause_hours", 2),
                        ssh_username=data.get("ssh_username", "azureuser"),
                    )
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Warning: Could not load pool registry: {e}")
                self._pool = None

    def save(self) -> None:
        """Save pool to registry file."""
        if self._pool is None:
            return
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.registry_file, "w") as f:
            json.dump(asdict(self._pool), f, indent=2)

    def create_pool(
        self,
        workers: list[tuple[str, str]],  # [(name, ip), ...]
        resource_group: str,
        location: str,
        vm_size: str = "Standard_D4ds_v5",
    ) -> VMPool:
        """Create a new pool from created VMs.

        Args:
            workers: List of (name, ip) tuples.
            resource_group: Azure resource group.
            location: Azure region.
            vm_size: VM size used.

        Returns:
            Created VMPool.
        """
        pool_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._pool = VMPool(
            pool_id=pool_id,
            created_at=datetime.now().isoformat(),
            resource_group=resource_group,
            location=location,
            vm_size=vm_size,
            workers=[
                PoolWorker(name=name, ip=ip, status="ready") for name, ip in workers
            ],
        )
        self.save()
        return self._pool

    def get_pool(self) -> VMPool | None:
        """Get current pool."""
        return self._pool

    def update_worker(self, name: str, **kwargs) -> None:
        """Update a worker's status.

        Args:
            name: Worker name.
            **kwargs: Fields to update.
        """
        if self._pool is None:
            return
        for worker in self._pool.workers:
            if worker.name == name:
                for key, value in kwargs.items():
                    if hasattr(worker, key):
                        setattr(worker, key, value)
                worker.updated_at = datetime.now().isoformat()
                break
        self.save()

    def update_pool_progress(self, completed: int = 0, failed: int = 0) -> None:
        """Update pool-level progress.

        Args:
            completed: Increment completed count by this amount.
            failed: Increment failed count by this amount.
        """
        if self._pool is None:
            return
        self._pool.completed_tasks += completed
        self._pool.failed_tasks += failed
        self.save()

    def update_pool_status(self, status: str, paused_since: str | None = None) -> None:
        """Update pool status (e.g., active, paused).

        Args:
            status: New pool status string.
            paused_since: ISO timestamp when pool was paused (None to clear).
        """
        if self._pool is None:
            return
        self._pool.status = status
        self._pool.paused_since = paused_since
        self.save()

    def delete_pool(self) -> bool:
        """Delete the pool registry (VMs must be deleted separately).

        Returns:
            True if pool was deleted.
        """
        if self.registry_file.exists():
            self.registry_file.unlink()
            self._pool = None
            return True
        return False


class VMRegistry:
    """Manage a registry of VMs and their status."""

    def __init__(
        self, registry_file: str | Path = "benchmark_results/vm_registry.json"
    ):
        """Initialize registry.

        Args:
            registry_file: Path to JSON registry file.
        """
        self.registry_file = Path(registry_file)
        self._vms: list[VMConfig] = []
        self.load()

    def load(self) -> None:
        """Load VMs from registry file."""
        if self.registry_file.exists():
            with open(self.registry_file) as f:
                data = json.load(f)
                self._vms = [VMConfig.from_dict(vm) for vm in data]

    def save(self) -> None:
        """Save VMs to registry file."""
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.registry_file, "w") as f:
            json.dump([vm.to_dict() for vm in self._vms], f, indent=2)

    def add(self, config: VMConfig) -> None:
        """Add a VM to the registry."""
        # Remove existing VM with same name
        self._vms = [vm for vm in self._vms if vm.name != config.name]
        self._vms.append(config)
        self.save()

    def remove(self, name: str) -> bool:
        """Remove a VM from the registry.

        Returns:
            True if VM was found and removed.
        """
        original_len = len(self._vms)
        self._vms = [vm for vm in self._vms if vm.name != name]
        if len(self._vms) < original_len:
            self.save()
            return True
        return False

    def get(self, name: str) -> VMConfig | None:
        """Get a VM by name."""
        for vm in self._vms:
            if vm.name == name:
                return vm
        return None

    def list(self) -> list[VMConfig]:
        """List all VMs."""
        return list(self._vms)

    def check_all(self, timeout: int = 5) -> list[VMStatus]:
        """Check status of all VMs.

        Args:
            timeout: Timeout per VM check.

        Returns:
            List of VMStatus for each registered VM.
        """
        statuses = []
        for config in self._vms:
            monitor = VMMonitor(config, timeout=timeout)
            statuses.append(monitor.check_status())
        return statuses


def main():
    """CLI entry point for VM monitoring."""
    import argparse

    parser = argparse.ArgumentParser(description="Monitor WAA VMs")
    parser.add_argument("--host", help="SSH host")
    parser.add_argument("--user", default="azureuser", help="SSH user")
    parser.add_argument("--container", default="winarena", help="Docker container name")
    parser.add_argument(
        "--interval", type=int, default=30, help="Check interval in seconds"
    )
    parser.add_argument("--output", help="Output file for status updates (JSON lines)")
    parser.add_argument("--list", action="store_true", help="List all registered VMs")
    parser.add_argument(
        "--check-all", action="store_true", help="Check all registered VMs"
    )

    args = parser.parse_args()

    if args.list:
        registry = VMRegistry()
        for vm in registry.list():
            print(
                f"  {vm.name}: {vm.ssh_user}@{vm.ssh_host} (container: {vm.docker_container})"
            )
        return

    if args.check_all:
        registry = VMRegistry()
        for status in registry.check_all():
            print(f"\n{status.config.name}:")
            print(f"  SSH: {'✓' if status.ssh_reachable else '✗'}")
            print(f"  VNC: {'✓' if status.vnc_reachable else '✗'}")
            print(f"  WAA: {'✓ READY' if status.waa_ready else '✗ Not ready'}")
            if status.disk_usage_gb:
                print(f"  Disk: {status.disk_usage_gb} GB")
        return

    if not args.host:
        parser.error("--host is required for monitoring")

    config = VMConfig(
        name="cli-vm",
        ssh_host=args.host,
        ssh_user=args.user,
        docker_container=args.container,
    )

    monitor = VMMonitor(config)

    def print_status(status: VMStatus):
        ts = datetime.now().strftime("%H:%M:%S")
        waa_str = "READY!" if status.waa_ready else "not ready"
        disk_str = f"{status.disk_usage_gb}GB" if status.disk_usage_gb else "?"
        print(
            f"[{ts}] SSH: {'✓' if status.ssh_reachable else '✗'} | "
            f"VNC: {'✓' if status.vnc_reachable else '✗'} | "
            f"WAA: {waa_str} | Disk: {disk_str}"
        )
        if status.container_logs:
            # Show last log line
            last_line = status.container_logs.split("\n")[-1][:80]
            print(f"         Log: {last_line}")

    print(f"Monitoring {args.host}... (Ctrl+C to stop)")
    try:
        final_status = monitor.run_monitor(
            callback=print_status,
            interval=args.interval,
            output_file=args.output,
        )
        print(f"\n✓ WAA is ready! Probe response: {final_status.waa_probe_response}")
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


# ============================================================================
# Azure ML Job Tracking
# ============================================================================


@dataclass
class AzureMLJob:
    """Represents an Azure ML job."""

    job_id: str
    display_name: str
    status: str  # running, completed, failed, canceled
    created_at: str
    compute_target: str | None = None
    duration_minutes: float | None = None
    cost_usd: float | None = None
    azure_dashboard_url: str | None = None


def fetch_azure_ml_jobs(
    resource_group: str = "openadapt-agents",
    workspace_name: str = "openadapt-ml",
    days: int = 7,
    max_results: int = 20,
) -> list[AzureMLJob]:
    """Fetch recent Azure ML jobs.

    Args:
        resource_group: Azure resource group name.
        workspace_name: Azure ML workspace name.
        days: Number of days to look back.
        max_results: Maximum number of jobs to return.

    Returns:
        List of AzureMLJob objects, sorted by creation time (newest first).
    """
    try:
        result = subprocess.run(
            [
                "az",
                "ml",
                "job",
                "list",
                "--resource-group",
                resource_group,
                "--workspace-name",
                workspace_name,
                "--query",
                "[].{name:name,display_name:display_name,status:status,created_at:creation_context.created_at,compute:compute}",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.error(f"Azure CLI error: {result.stderr}")
            return []

        jobs_raw = json.loads(result.stdout)

        # Filter by date
        cutoff_date = datetime.now() - timedelta(days=days)
        jobs = []

        for job in jobs_raw[:max_results]:
            created_at = job.get("created_at", "")
            try:
                # Parse ISO format: 2026-01-17T10:30:00Z
                job_date = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                    if created_at
                    else datetime.now().isoformat()
                )
                if job_date < cutoff_date.replace(tzinfo=job_date.tzinfo):
                    continue
            except (ValueError, AttributeError):
                # If date parsing fails, include the job
                pass

            # Calculate duration for completed jobs
            duration_minutes = None
            status = job.get("status", "unknown").lower()

            # Build Azure dashboard URL
            subscription_id = get_azure_subscription_id()
            wsid = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.MachineLearningServices/workspaces/{workspace_name}"
            dashboard_url = (
                f"https://ml.azure.com/runs/{job.get('name', '')}?wsid={wsid}"
            )

            jobs.append(
                AzureMLJob(
                    job_id=job.get("name", "unknown"),
                    display_name=job.get("display_name", ""),
                    status=status,
                    created_at=created_at,
                    compute_target=job.get("compute", None),
                    duration_minutes=duration_minutes,
                    azure_dashboard_url=dashboard_url,
                )
            )

        return jobs

    except Exception as e:
        logger.error(f"Error fetching Azure ML jobs: {e}")
        return []


def get_azure_subscription_id() -> str:
    """Get the current Azure subscription ID."""
    try:
        result = subprocess.run(
            ["az", "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


# ============================================================================
# Cost Tracking
# ============================================================================


@dataclass
class VMCostEstimate:
    """Estimated costs for VM usage."""

    vm_size: str
    hourly_rate_usd: float
    hours_elapsed: float
    cost_usd: float
    cost_per_hour_usd: float
    cost_per_day_usd: float
    cost_per_week_usd: float


# Azure VM pricing (US East, as of Jan 2025)
VM_PRICING = {
    "Standard_D2_v3": 0.096,
    "Standard_D4_v3": 0.192,
    "Standard_D8_v3": 0.384,
    "Standard_D4s_v3": 0.192,
    "Standard_D8s_v3": 0.384,
    "Standard_D4ds_v5": 0.192,
    "Standard_D8ds_v5": 0.384,
    "Standard_D16ds_v5": 0.768,
    "Standard_D32ds_v5": 1.536,
}


def calculate_vm_costs(
    vm_size: str, hours: float, hourly_rate_override: float | None = None
) -> VMCostEstimate:
    """Calculate VM cost estimates.

    Args:
        vm_size: Azure VM size (e.g., "Standard_D4ds_v5").
        hours: Number of hours the VM has been running.
        hourly_rate_override: Override default hourly rate (for custom pricing).

    Returns:
        VMCostEstimate with cost breakdown.
    """
    hourly_rate = hourly_rate_override or VM_PRICING.get(vm_size, 0.20)
    cost_usd = hourly_rate * hours

    return VMCostEstimate(
        vm_size=vm_size,
        hourly_rate_usd=hourly_rate,
        hours_elapsed=hours,
        cost_usd=cost_usd,
        cost_per_hour_usd=hourly_rate,
        cost_per_day_usd=hourly_rate * 24,
        cost_per_week_usd=hourly_rate * 24 * 7,
    )


def get_vm_uptime_hours(
    resource_group: str, vm_name: str, check_actual_state: bool = True
) -> float:
    """Get VM uptime in hours.

    Args:
        resource_group: Azure resource group.
        vm_name: VM name.
        check_actual_state: If True, check if VM is actually running.

    Returns:
        Hours since VM started, or 0 if VM is not running.
    """
    try:
        # Get VM creation time or last start time
        result = subprocess.run(
            [
                "az",
                "vm",
                "show",
                "-d",
                "-g",
                resource_group,
                "-n",
                vm_name,
                "--query",
                "{powerState:powerState}",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return 0.0

        info = json.loads(result.stdout)
        power_state = info.get("powerState", "")

        # Check if VM is running
        if check_actual_state and "running" not in power_state.lower():
            return 0.0

        # Try to get activity logs for last start time
        result = subprocess.run(
            [
                "az",
                "monitor",
                "activity-log",
                "list",
                "--resource-group",
                resource_group,
                "--resource-id",
                f"/subscriptions/{get_azure_subscription_id()}/resourceGroups/{resource_group}/providers/Microsoft.Compute/virtualMachines/{vm_name}",
                "--query",
                "[?operationName.localizedValue=='Start Virtual Machine' || operationName.localizedValue=='Create or Update Virtual Machine'].eventTimestamp | [0]",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0 and result.stdout.strip():
            start_time_str = result.stdout.strip()
            start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            elapsed = datetime.now(start_time.tzinfo) - start_time
            return elapsed.total_seconds() / 3600

        # Fallback: assume started 1 hour ago if we can't determine
        return 1.0

    except Exception as e:
        logger.debug(f"Error getting VM uptime: {e}")
        return 0.0


# ============================================================================
# VM Activity Detection
# ============================================================================


@dataclass
class VMActivity:
    """Current VM activity information."""

    is_active: bool
    activity_type: str  # idle, benchmark_running, training, setup, unknown
    description: str
    benchmark_progress: dict | None = None  # If benchmark is running
    last_action_time: str | None = None


def detect_vm_activity(
    ip: str,
    ssh_user: str = "azureuser",
    docker_container: str = "winarena",
    internal_ip: str = "localhost",  # WAA server bound to localhost via Docker port forward
) -> VMActivity:
    """Detect what the VM is currently doing.

    Args:
        ip: VM IP address.
        ssh_user: SSH username.
        docker_container: Docker container name.
        internal_ip: Internal IP for WAA server.

    Returns:
        VMActivity with current activity information.
    """
    try:
        # Check if container is running
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=5",
                f"{ssh_user}@{ip}",
                f"docker ps -q -f name={docker_container}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return VMActivity(
                is_active=False,
                activity_type="idle",
                description="Container not running",
            )

        # Check WAA probe for benchmark status
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=5",
                f"{ssh_user}@{ip}",
                f"curl -s --connect-timeout 3 http://{internal_ip}:5000/probe",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0 and result.stdout.strip():
            probe_response = result.stdout.strip()
            try:
                probe_data = json.loads(probe_response)
                # WAA is ready and responsive - check if benchmark is actually running
                # by looking for python processes (Navi agent or our client)
                python_check = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "StrictHostKeyChecking=no",
                        "-o",
                        "ConnectTimeout=5",
                        f"{ssh_user}@{ip}",
                        f"docker exec {docker_container} pgrep -f 'python.*run' 2>/dev/null | head -1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                is_running = bool(python_check.stdout.strip())

                return VMActivity(
                    is_active=is_running,
                    activity_type="benchmark_running" if is_running else "idle",
                    description="WAA benchmark running"
                    if is_running
                    else "WAA ready - idle",
                    benchmark_progress=probe_data,
                )
            except json.JSONDecodeError:
                # Got response but not JSON - maybe setup phase
                return VMActivity(
                    is_active=True,
                    activity_type="setup",
                    description="WAA starting up",
                )

        # Container running but WAA not ready
        return VMActivity(
            is_active=True,
            activity_type="setup",
            description="Windows VM booting or WAA initializing",
        )

    except Exception as e:
        logger.debug(f"Error detecting VM activity: {e}")
        return VMActivity(
            is_active=False,
            activity_type="unknown",
            description=f"Error checking activity: {str(e)[:100]}",
        )


# ============================================================================
# Evaluation History
# ============================================================================


@dataclass
class EvaluationRun:
    """Historical evaluation run."""

    run_id: str
    started_at: str
    completed_at: str | None
    num_tasks: int
    success_rate: float | None
    agent_type: str
    status: str  # running, completed, failed


def get_evaluation_history(
    results_dir: Path | str = "benchmark_results", max_runs: int = 10
) -> list[EvaluationRun]:
    """Get history of evaluation runs from results directory.

    Args:
        results_dir: Path to benchmark results directory.
        max_runs: Maximum number of runs to return.

    Returns:
        List of EvaluationRun objects, sorted by start time (newest first).
    """
    results_path = Path(results_dir)
    if not results_path.exists():
        return []

    runs = []

    # Look for run directories or result files
    for item in sorted(results_path.iterdir(), reverse=True):
        if item.is_dir():
            # Check for summary.json or similar
            summary_file = item / "summary.json"
            if summary_file.exists():
                try:
                    summary = json.loads(summary_file.read_text())
                    runs.append(
                        EvaluationRun(
                            run_id=item.name,
                            started_at=summary.get("started_at", "unknown"),
                            completed_at=summary.get("completed_at", None),
                            num_tasks=summary.get("num_tasks", 0),
                            success_rate=summary.get("success_rate", None),
                            agent_type=summary.get("agent_type", "unknown"),
                            status=summary.get("status", "completed"),
                        )
                    )
                except (json.JSONDecodeError, KeyError):
                    continue

        if len(runs) >= max_runs:
            break

    return runs


if __name__ == "__main__":
    main()
