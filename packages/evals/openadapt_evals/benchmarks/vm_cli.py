#!/usr/bin/env python3
"""
WAA Benchmark CLI - Windows Agent Arena evaluation toolkit

Uses custom waa_deploy/Dockerfile with dockurr/windows:latest base and
Python 3.9 from vanilla windowsarena/winarena for GroundingDINO compatibility.

See waa_deploy/Dockerfile for details.

Usage:
    uv run python -m openadapt_evals.benchmarks.vm_cli <command> [options]

Commands:
    create      Create Azure VM with nested virtualization
    delete      Delete VM and ALL associated resources
    status      Show VM state and IP
    build       Build WAA image from waa_deploy/Dockerfile
    start       Start WAA container (Windows boots + WAA server)
    probe       Check if WAA server is ready
    run         Run benchmark tasks
    deallocate  Stop VM (preserves disk, stops billing)
    logs        Show WAA status and logs

Workflow:
    1. create    - Create Azure VM (~5 min)
    2. build     - Build custom WAA image (~10 min)
    3. start     - Start container, Windows downloads+boots (~15-20 min first time)
    4. probe --wait - Wait for WAA server
    5. run       - Run benchmark
    6. deallocate - Stop billing
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

# =============================================================================
# Constants (single source of truth)
# =============================================================================

# VM size: D8ds_v5 ($0.38/hr, 8 vCPU, 32GB RAM)
# D4ds_v4 (16GB) OOMs with navi agent's GroundingDINO + SoM models — do not use.
VM_SIZE = "Standard_D8ds_v5"

# Fallback VM sizes (in order of preference, all 8 vCPU / 32GB)
# D8ds_v5: First choice (v5 with local SSD)
# D8s_v5: v5 without local SSD
# D8ds_v4: v4 with local SSD
# D8as_v5: AMD version
VM_SIZE_FALLBACKS = [
    ("Standard_D8ds_v5", 0.38),
    ("Standard_D8s_v5", 0.36),
    ("Standard_D8ds_v4", 0.38),
    ("Standard_D8as_v5", 0.34),
]
VM_REGIONS = ["centralus", "eastus", "westus2", "eastus2"]
VM_NAME = "waa-eval-vm"

# OS disk size: 128GB required for Docker images (~66GB used).
# Default 30GB is too small and causes disk full errors during image build.
OS_DISK_SIZE_GB = 128

# WAA storage directory on the VM. MUST be on the persistent OS disk, NOT /mnt
# which is ephemeral Azure temp storage wiped on every VM deallocate/restart.
# Using /home/azureuser/waa-storage ensures the Windows disk image (data.img)
# survives deallocate/start cycles, avoiding 15-20 min cold reinstalls.
WAA_STORAGE_DIR = "/home/azureuser/waa-storage"


def _get_resource_group() -> str:
    """Get resource group from config (supports AZURE_RESOURCE_GROUP env var)."""
    try:
        from openadapt_evals.config import settings

        return settings.azure_resource_group
    except Exception:
        return "openadapt-agents"


RESOURCE_GROUP = _get_resource_group()


def _get_default_cloud() -> str:
    """Get default cloud provider from config."""
    try:
        from openadapt_evals.config import settings

        return settings.cloud_provider
    except Exception:
        return "azure"


def _get_pool_ssh_username(pool: dict) -> str:
    """Get SSH username from pool registry, defaulting to azureuser for backward compat."""
    return pool.get("ssh_username", "azureuser")


def _create_vm_manager(cloud: str | None = None, resource_group: str | None = None):
    """Factory to create the appropriate VM manager based on cloud provider.

    Args:
        cloud: Cloud provider ("azure" or "aws"). If None, uses config default.
        resource_group: Azure resource group (ignored for AWS).

    Returns:
        VMProvider instance (AzureVMManager or AWSVMManager).
    """
    cloud = cloud or _get_default_cloud()
    if cloud == "aws":
        from openadapt_evals.infrastructure.aws_vm import AWSVMManager

        return AWSVMManager()
    else:
        from openadapt_evals.infrastructure.azure_vm import AzureVMManager

        return AzureVMManager(resource_group=resource_group or RESOURCE_GROUP)


# Custom WAA image built from waa_deploy/Dockerfile
# Uses dockurr/windows:latest as base (with proper ISO download) + WAA components
DOCKER_IMAGE = "waa-auto:latest"
LOG_DIR = Path.home() / ".openadapt" / "waa"
SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=60",  # Send keepalive every 60s to prevent timeout
    "-o",
    "ServerAliveCountMax=10",  # Allow 10 missed keepalives (~10 min) before disconnect
]


def setup_vnc_tunnel_and_browser(ip: str) -> Optional[subprocess.Popen]:
    """Set up SSH tunnel for VNC and open browser.

    Returns the tunnel process on success, None on failure.
    """
    # Kill any existing tunnel on port 8006
    subprocess.run(["pkill", "-f", "ssh.*8006:localhost:8006"], capture_output=True)

    # Start SSH tunnel in background
    tunnel_proc = subprocess.Popen(
        ["ssh", *SSH_OPTS, "-N", "-L", "8006:localhost:8006", f"azureuser@{ip}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for tunnel to establish
    time.sleep(2)

    # Check if tunnel is running
    if tunnel_proc.poll() is not None:
        return None

    # Open browser
    vnc_url = "http://localhost:8006"
    webbrowser.open(vnc_url)

    return tunnel_proc


# Dockerfile location (relative to this file)
DOCKERFILE_PATH = Path(__file__).parent.parent / "waa_deploy" / "Dockerfile"

# =============================================================================
# Logging
# =============================================================================

_log_file: Optional[Path] = None
_session_id: Optional[str] = None


def init_logging() -> Path:
    """Initialize logging for this session."""
    global _log_file, _session_id

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Create session ID
    _session_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    session_dir = LOG_DIR / "sessions" / _session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Session log file
    _log_file = session_dir / "full.log"

    # Update current session pointer
    (LOG_DIR / "session_id.txt").write_text(_session_id)

    # Symlink for easy access
    current_link = LOG_DIR / "current"
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(session_dir)

    return _log_file


def log(step: str, message: str, end: str = "\n"):
    """Log message to file and stdout."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] [{step}] {message}"

    # Print to stdout
    print(formatted, end=end, flush=True)

    # Write to log file
    if _log_file:
        with open(_log_file, "a") as f:
            f.write(formatted + end)


def log_stream(step: str, process: subprocess.Popen):
    """Stream process output to log and stdout."""
    if process.stdout:
        for line in iter(process.stdout.readline, ""):
            if line:
                log(step, line.rstrip())


# =============================================================================
# Azure Helpers
# =============================================================================


def get_vm_ip() -> Optional[str]:
    """Get VM public IP if it exists."""
    from openadapt_evals.infrastructure.azure_vm import AzureVMManager

    return AzureVMManager(resource_group=RESOURCE_GROUP).get_vm_ip(VM_NAME)


def get_vm_state() -> Optional[str]:
    """Get VM power state."""
    from openadapt_evals.infrastructure.azure_vm import AzureVMManager

    return AzureVMManager(resource_group=RESOURCE_GROUP).get_vm_state(VM_NAME)


def ssh_run(
    ip: str, cmd: str, stream: bool = False, step: str = "SSH"
) -> subprocess.CompletedProcess:
    """Run command on VM via SSH."""
    from openadapt_evals.infrastructure.azure_vm import ssh_run as _ssh_run

    return _ssh_run(ip, cmd, stream=stream, step=step, log_fn=log)


def wait_for_ssh(ip: str, timeout: int = 120) -> bool:
    """Wait for SSH to become available."""
    from openadapt_evals.infrastructure.azure_vm import wait_for_ssh as _wait_for_ssh

    return _wait_for_ssh(ip, timeout=timeout)


def set_vm_auto_shutdown(
    vm_name: str,
    resource_group: str = RESOURCE_GROUP,
    shutdown_hours: int = 4,
) -> bool:
    """Set Azure auto-shutdown policy on a VM."""
    from openadapt_evals.infrastructure.azure_vm import AzureVMManager

    return AzureVMManager(resource_group=resource_group).set_auto_shutdown(
        vm_name, hours=shutdown_hours
    )


def delete_test_vm_resources(test_name: str, resource_group: str = RESOURCE_GROUP):
    """Delete a test VM and its associated resources."""
    from openadapt_evals.infrastructure.azure_vm import AzureVMManager

    AzureVMManager(resource_group=resource_group).delete_vm(test_name)


# =============================================================================
# Commands
# =============================================================================


def cmd_create(args):
    """Create Azure VM with nested virtualization."""
    init_logging()

    # Check if VM already exists
    ip = get_vm_ip()
    if ip:
        log("CREATE", f"VM already exists: {ip}")
        log("CREATE", "Use 'delete' first if you want to recreate")
        return 0

    # Try multiple D8 sizes with fallbacks (all 32GB, required for navi agent)
    sizes_to_try = VM_SIZE_FALLBACKS
    log(
        "CREATE",
        f"Creating VM '{VM_NAME}' (trying D8 sizes with fallbacks)...",
    )

    # Try size+region combinations until one works
    vm_created = False
    successful_size = None
    successful_cost = None

    for vm_size, cost_per_hour in sizes_to_try:
        log("CREATE", f"Trying size {vm_size} (${cost_per_hour:.2f}/hr)...")

        for region in VM_REGIONS:
            log("CREATE", f"  {region}...", end=" ")

            result = subprocess.run(
                [
                    "az",
                    "vm",
                    "create",
                    "--resource-group",
                    RESOURCE_GROUP,
                    "--name",
                    VM_NAME,
                    "--location",
                    region,
                    "--image",
                    "Ubuntu2204",
                    "--size",
                    vm_size,
                    "--admin-username",
                    "azureuser",
                    "--generate-ssh-keys",
                    "--public-ip-sku",
                    "Standard",
                    "--os-disk-size-gb",
                    str(OS_DISK_SIZE_GB),
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                vm_info = json.loads(result.stdout)
                ip = vm_info.get("publicIpAddress", "")
                log("CREATE", f"created ({ip})")
                vm_created = True
                successful_size = vm_size
                successful_cost = cost_per_hour
                break
            else:
                log("CREATE", "unavailable")

        if vm_created:
            break

    if not vm_created:
        log("CREATE", "ERROR: Could not create VM in any region with any size")
        log("CREATE", "Tried sizes: " + ", ".join(s[0] for s in sizes_to_try))
        return 1

    log(
        "CREATE",
        f"Successfully created {successful_size} (${successful_cost:.2f}/hr) in {region}",
    )

    # Set auto-shutdown as safety net (prevents orphaned VMs)
    auto_shutdown_hours = getattr(args, "auto_shutdown_hours", 4)
    if auto_shutdown_hours > 0:
        log("CREATE", f"Setting auto-shutdown in {auto_shutdown_hours} hours...")
        if set_vm_auto_shutdown(VM_NAME, RESOURCE_GROUP, auto_shutdown_hours):
            log("CREATE", "Auto-shutdown configured")
        else:
            log("CREATE", "Warning: Failed to set auto-shutdown (VM will stay running)")

    # Wait for SSH
    log("CREATE", "Waiting for SSH...")
    if not wait_for_ssh(ip):
        log("CREATE", "ERROR: SSH not available after 2 minutes")
        return 1
    log("CREATE", "SSH ready")

    # Install Docker with persistent storage
    log("CREATE", "Installing Docker with persistent storage...")
    docker_setup = """
set -e
export DEBIAN_FRONTEND=noninteractive
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER

# Configure Docker to use persistent storage (NOT /mnt which is ephemeral
# and gets wiped on VM deallocate, breaking pool-resume)
sudo systemctl stop docker
sudo mkdir -p /home/azureuser/docker
sudo bash -c 'echo "{\\"data-root\\": \\"/home/azureuser/docker\\"}" > /etc/docker/daemon.json'
sudo systemctl start docker

# Verify
docker --version
df -h /home
"""
    result = ssh_run(ip, docker_setup, stream=True, step="CREATE")
    if result.returncode != 0:
        log("CREATE", "ERROR: Docker setup failed")
        return 1

    log("CREATE", f"VM ready: {ip}")
    return 0


def cmd_delete(args):
    """Delete VM and ALL associated resources."""
    init_logging()
    log("DELETE", f"Deleting VM '{VM_NAME}' and all associated resources...")

    # Delete VM
    log("DELETE", "Deleting VM...")
    result = subprocess.run(
        [
            "az",
            "vm",
            "delete",
            "-g",
            RESOURCE_GROUP,
            "-n",
            VM_NAME,
            "--yes",
            "--force-deletion",
            "true",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log("DELETE", "VM deleted")
    else:
        log("DELETE", "VM not found or already deleted")

    # Delete NICs
    log("DELETE", "Deleting NICs...")
    result = subprocess.run(
        [
            "az",
            "network",
            "nic",
            "list",
            "-g",
            RESOURCE_GROUP,
            "--query",
            "[?contains(name, 'waa')].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )
    for nic in result.stdout.strip().split("\n"):
        if nic:
            subprocess.run(
                ["az", "network", "nic", "delete", "-g", RESOURCE_GROUP, "-n", nic],
                capture_output=True,
            )
            log("DELETE", f"  Deleted NIC: {nic}")

    # Delete public IPs
    log("DELETE", "Deleting public IPs...")
    result = subprocess.run(
        [
            "az",
            "network",
            "public-ip",
            "list",
            "-g",
            RESOURCE_GROUP,
            "--query",
            "[?contains(name, 'waa')].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )
    for pip in result.stdout.strip().split("\n"):
        if pip:
            subprocess.run(
                [
                    "az",
                    "network",
                    "public-ip",
                    "delete",
                    "-g",
                    RESOURCE_GROUP,
                    "-n",
                    pip,
                ],
                capture_output=True,
            )
            log("DELETE", f"  Deleted IP: {pip}")

    # Delete disks
    log("DELETE", "Deleting disks...")
    result = subprocess.run(
        [
            "az",
            "disk",
            "list",
            "-g",
            RESOURCE_GROUP,
            "--query",
            "[?contains(name, 'waa')].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )
    for disk in result.stdout.strip().split("\n"):
        if disk:
            subprocess.run(
                ["az", "disk", "delete", "-g", RESOURCE_GROUP, "-n", disk, "--yes"],
                capture_output=True,
            )
            log("DELETE", f"  Deleted disk: {disk}")

    # Delete NSGs
    log("DELETE", "Deleting NSGs...")
    result = subprocess.run(
        [
            "az",
            "network",
            "nsg",
            "list",
            "-g",
            RESOURCE_GROUP,
            "--query",
            "[?contains(name, 'waa')].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )
    for nsg in result.stdout.strip().split("\n"):
        if nsg:
            subprocess.run(
                ["az", "network", "nsg", "delete", "-g", RESOURCE_GROUP, "-n", nsg],
                capture_output=True,
            )
            log("DELETE", f"  Deleted NSG: {nsg}")

    log("DELETE", "Cleanup complete")
    return 0


def cmd_pool_status(args):
    """Show status of all VMs in the current pool."""
    init_logging()

    from openadapt_evals.infrastructure.pool import PoolManager
    from openadapt_evals.infrastructure.vm_monitor import VMConfig, VMMonitor

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)
    pool = manager.status()

    if pool is None:
        print("No active VM pool. Create one with: create --workers N")
        return 0

    pool_status_label = pool.status.upper() if hasattr(pool, "status") else "ACTIVE"
    print(f"\n=== VM Pool: {pool.pool_id} ({pool_status_label}) ===\n")
    print(f"Created: {pool.created_at}")
    print(f"Workers: {len(pool.workers)}")
    print(f"Status: {pool_status_label}")
    if hasattr(pool, "paused_since") and pool.paused_since:
        from datetime import datetime as _dt

        paused_dt = _dt.fromisoformat(pool.paused_since)
        paused_days = (_dt.now() - paused_dt).total_seconds() / 86400
        accumulated_cost = 0.25 * len(pool.workers) * paused_days
        print(f"Paused since: {pool.paused_since} ({paused_days:.1f} days)")
        print(f"Idle cost: ~${0.25 * len(pool.workers):.2f}/day (accumulated: ${accumulated_cost:.2f})")
        if paused_days >= 14:
            print(f"  WARNING: Pool paused for {paused_days:.0f} days! Delete with: oa-vm pool-cleanup -y")
        elif paused_days >= 7:
            days_left = 14 - paused_days
            print(f"  WARNING: Pool idle for {paused_days:.0f} days. Consider deleting in {days_left:.0f} days.")
    if pool.status == "active" and hasattr(pool, "auto_pause_at") and pool.auto_pause_at:
        from datetime import datetime as _dt
        try:
            auto_pause_dt = _dt.fromisoformat(pool.auto_pause_at)
            remaining = auto_pause_dt - _dt.now()
            remaining_min = remaining.total_seconds() / 60
            if remaining_min > 0:
                print(f"Auto-shutdown: in {remaining_min:.0f} minutes")
            else:
                print("Auto-shutdown: OVERDUE (check VM status)")
        except ValueError:
            pass
    print(f"Tasks: {pool.completed_tasks}/{pool.total_tasks}")
    print()

    # Table header
    print(f"{'Name':<18} {'IP':<16} {'Status':<12} {'WAA':<6} {'Tasks':<10}")
    print("-" * 65)

    for w in pool.workers:
        waa_status = "Ready" if w.waa_ready else "---"
        task_progress = f"{len(w.completed_tasks)}/{len(w.assigned_tasks)}"
        print(f"{w.name:<18} {w.ip:<16} {w.status:<12} {waa_status:<6} {task_progress:<10}")

    # Probe each VM for live status if --probe flag
    if getattr(args, "probe", False):
        print("\nProbing VMs for WAA readiness...")
        for w in pool.workers:
            try:
                monitor = VMMonitor(VMConfig(name=w.name, ssh_host=w.ip))
                status = monitor.check_status()
                ready = "READY" if status.waa_ready else "Not ready"
                print(f"  {w.name}: {ready}")
            except Exception as e:
                print(f"  {w.name}: Error - {e}")

    return 0


def cmd_delete_pool(args):
    """Delete all VMs in the current pool."""
    init_logging()
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from openadapt_evals.infrastructure.pool import PoolManager

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)
    pool = manager.status()

    if pool is None:
        print("No active VM pool.")
        return 0

    print(f"\n=== Deleting VM Pool: {pool.pool_id} ===\n")
    print(f"This will delete {len(pool.workers)} VMs:")
    for w in pool.workers:
        print(f"  - {w.name} ({w.ip})")

    if not getattr(args, "yes", False):
        confirm = input("\nType 'yes' to confirm: ")
        if confirm.lower() != "yes":
            print("Aborted.")
            return 0

    def delete_vm(name: str) -> tuple[str, bool, str]:
        success = vm_manager.delete_vm(name)
        if success:
            return (name, True, "deleted")
        else:
            return (name, False, "deletion failed")

    print("\nDeleting VMs...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(delete_vm, w.name): w.name for w in pool.workers}
        for future in as_completed(futures):
            name, success, msg = future.result()
            status = "deleted" if success else f"FAILED: {msg}"
            print(f"  {name}: {status}")

    # Delete registry
    manager.registry.delete_pool()
    print("\nPool deleted.")
    return 0


def cmd_pool_create(args):
    """Create a pool of VMs for parallel WAA evaluation.

    Creates N VMs in parallel, each configured with Docker and ready for WAA.
    Uses ThreadPoolExecutor for concurrent VM creation.
    """
    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    num_workers = getattr(args, "workers", 3)
    auto_shutdown_hours = getattr(args, "auto_shutdown_hours", 4)
    use_acr = getattr(args, "use_acr", False)
    image_id = getattr(args, "image", None)

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)

    try:
        manager.create(
            workers=num_workers,
            auto_shutdown_hours=auto_shutdown_hours,
            use_acr=use_acr,
            image_id=image_id,
        )
        return 0
    except RuntimeError as e:
        log("POOL", f"ERROR: {e}")
        return 1


def cmd_pool_wait(args):
    """Wait for all pool workers to have WAA ready.

    The command waits for guests that crossed the separate qualified start gate.
    """
    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    timeout_minutes = getattr(args, "timeout", 30)
    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)

    try:
        workers_ready = manager.wait(
            timeout_minutes=timeout_minutes,
            start_containers=False,
            qualification_dir=args.qualification_dir,
        )
        return 0 if workers_ready else 1
    except RuntimeError as e:
        log("POOL-WAIT", f"ERROR: {e}")
        return 1


def cmd_pool_run(args):
    """Run WAA benchmark tasks distributed across pool workers.

    Distributes tasks round-robin across available workers and runs them
    in parallel. Collects results from all workers.
    """
    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    num_tasks = getattr(args, "tasks", 10)
    agent = getattr(args, "agent", "navi")
    model = getattr(args, "model", "gpt-4o-mini")
    api_key = getattr(args, "api_key", None)

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)

    try:
        result = manager.run(
            tasks=num_tasks,
            agent=agent,
            model=model,
            api_key=api_key,
            qualification_dir=args.qualification_dir,
        )
        return 0 if result.failed == 0 else 1
    except RuntimeError as e:
        log("POOL-RUN", f"ERROR: {e}")
        return 1


def cmd_pool_cleanup(args):
    """Clean up orphaned pool resources (VMs, NICs, IPs, disks).

    Use this after failed pool operations to clean up resources that
    weren't properly deleted.
    """
    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)

    confirm = not getattr(args, "yes", False)
    manager.cleanup(confirm=confirm)
    return 0


def cmd_pool_auto(args):
    """Create, qualify, wait for, and run a Windows worker pool.

    Each worker crosses the reset, exact egress, and one-use start gate before
    the command waits for WAA readiness or dispatches a benchmark task.

    If a pool already exists, the command still repeats qualification.
    """
    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    num_workers = getattr(args, "workers", 1)
    auto_shutdown_hours = getattr(args, "auto_shutdown_hours", 4)
    timeout_minutes = getattr(args, "timeout", 45)
    num_tasks = getattr(args, "tasks", None) or num_workers
    agent = getattr(args, "agent", "navi")
    model = getattr(args, "model", "gpt-4o-mini")
    api_key = getattr(args, "api_key", None)
    qualification_dir = args.qualification_dir
    if qualification_dir.is_symlink() or not qualification_dir.is_dir():
        log("POOL-AUTO", "ERROR: qualification directory is not a regular directory")
        return 1
    run_evidence_dir = args.run_evidence_dir
    if run_evidence_dir.is_symlink():
        log("POOL-AUTO", "ERROR: run evidence directory is a symbolic link")
        return 1
    run_evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not run_evidence_dir.is_dir():
        log("POOL-AUTO", "ERROR: run evidence path is not a directory")
        return 1

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)

    try:
        # Step 1: Create pool (skip if one already exists)
        pool = manager.status()
        if pool is not None:
            log("POOL-AUTO", f"Pool already exists ({len(pool.workers)} workers). Skipping create.")
        else:
            log("POOL-AUTO", "=== Step 1/4: Creating pool ===")
            manager.create(
                workers=num_workers,
                auto_shutdown_hours=auto_shutdown_hours,
            )

        pool = manager.status()
        if pool is None:
            log("POOL-AUTO", "ERROR: Pool state is absent after creation.")
            return 1

        # Step 2: qualify every guest before it can start.
        log("POOL-AUTO", "=== Step 2/4: Qualifying workers ===")
        for worker in pool.workers:
            proofs = manager.qualify_worker(
                worker.name,
                identity_path=qualification_dir / f"{worker.name}.identity.json",
                policy_path=qualification_dir / f"{worker.name}.egress.json",
            )
            for kind, proof in zip(
                ("reset", "egress", "start"),
                proofs,
                strict=True,
            ):
                evidence_path = run_evidence_dir / f"{worker.name}.{kind}.json"
                evidence_fd = os.open(
                    evidence_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(evidence_fd, "w", encoding="utf-8") as evidence_file:
                    json.dump(proof.__dict__, evidence_file, sort_keys=True, separators=(",", ":"))
                    evidence_file.write("\n")
                    evidence_file.flush()
                    os.fsync(evidence_file.fileno())
            evidence_directory = os.open(run_evidence_dir, os.O_RDONLY)
            try:
                os.fsync(evidence_directory)
            finally:
                os.close(evidence_directory)

        # Step 3: wait for WAA readiness without starting another container.
        log("POOL-AUTO", "=== Step 3/4: Waiting for WAA server ===")
        workers_ready = manager.wait(
            timeout_minutes=timeout_minutes,
            start_containers=False,
            qualification_dir=qualification_dir,
        )

        if not workers_ready:
            log("POOL-AUTO", "ERROR: No workers became ready. Aborting.")
            return 1

        # Step 4: Run benchmark
        log("POOL-AUTO", "=== Step 4/4: Running benchmark ===")
        result = manager.run(
            tasks=num_tasks,
            agent=agent,
            model=model,
            api_key=api_key,
            qualification_dir=qualification_dir,
        )

        log("POOL-AUTO", "")
        log("POOL-AUTO", "=== POOL-AUTO COMPLETE ===")
        log("POOL-AUTO", f"  Workers: {len(workers_ready)}")
        log("POOL-AUTO", f"  Tasks: {result.completed}/{result.total_tasks}")
        log("POOL-AUTO", f"  Failed: {result.failed}")
        log("POOL-AUTO", f"  Time: {result.elapsed_seconds / 60:.1f} minutes")
        log("POOL-AUTO", "")
        log("POOL-AUTO", "Next: oa-vm pool-cleanup -y  (to stop billing)")

        return 0 if result.failed == 0 else 1
    except RuntimeError as e:
        log("POOL-AUTO", f"ERROR: {e}")
        return 1


def cmd_pool_pause(args):
    """Pause pool: deallocate all VMs to stop compute billing.

    Keeps disks and IPs so the pool can be quickly resumed (~5 min)
    instead of recreating from scratch (~42 min). Idle cost ~$0.25/day.
    """
    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)

    try:
        manager.pause()
        return 0
    except RuntimeError as e:
        log("POOL-PAUSE", f"ERROR: {e}")
        return 1


def cmd_pool_resume(args):
    """Resume a paused pool: start VMs and wait for WAA readiness.

    Starts all deallocated VMs, waits for SSH, restarts WAA containers,
    and waits for WAA server readiness. Much faster than pool-create
    (~5 min vs ~42 min).
    """
    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    timeout_minutes = getattr(args, "timeout", 10)

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)

    try:
        ready_workers = manager.resume(timeout_minutes=timeout_minutes)
        return 0 if ready_workers else 1
    except RuntimeError as e:
        log("POOL-RESUME", f"ERROR: {e}")
        return 1


def cmd_pool_reset(args):
    """Restore one worker from its exact verified Windows baseline."""

    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)
    try:
        proof = manager.reset_worker(
            args.worker,
            run_id=args.run_id,
            identity_path=args.worker_identity,
        )
    except (RuntimeError, ValueError) as exc:
        log("POOL-RESET", f"ERROR: {exc}")
        return 1
    print(json.dumps(proof.__dict__, sort_keys=True, separators=(",", ":")))
    return 0


def cmd_pool_egress(args):
    """Apply one host-enforced egress policy to a dedicated worker."""

    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)
    try:
        proof = manager.apply_worker_egress(
            args.worker,
            args.policy,
            identity_path=args.worker_identity,
            reset_proof_path=args.reset_proof,
        )
    except (RuntimeError, ValueError) as exc:
        log("POOL-EGRESS", f"ERROR: {exc}")
        return 1
    print(
        json.dumps(
            {
                **proof.__dict__,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def cmd_pool_start(args):
    """Start one worker through the fresh reset and egress gate."""

    init_logging()
    from openadapt_evals.infrastructure.pool import PoolManager

    vm_manager = _create_vm_manager(getattr(args, "cloud", None))
    manager = PoolManager(vm_manager=vm_manager, log_fn=log)
    try:
        proof = manager.start_worker(
            args.worker,
            identity_path=args.worker_identity,
            policy_path=args.policy,
            reset_proof_path=args.reset_proof,
            egress_proof_path=args.egress_proof,
        )
    except (RuntimeError, ValueError) as exc:
        log("POOL-START", f"ERROR: {exc}")
        return 1
    print(json.dumps(proof.__dict__, sort_keys=True, separators=(",", ":")))
    return 0


def cmd_pool_vnc(args):
    """Open VNC to a specific pool worker via SSH tunnel.

    Each worker gets a unique local port: 8006 + worker_index.
    E.g., waa-pool-00 -> localhost:8006, waa-pool-01 -> localhost:8007
    """
    init_logging()

    # Load pool registry
    registry_path = Path("benchmark_results/vm_pool_registry.json")
    if not registry_path.exists():
        log("POOL-VNC", "ERROR: No pool found. Run pool-create first.")
        return 1

    with open(registry_path) as f:
        pool = json.load(f)

    workers = pool.get("workers", [])
    if not workers:
        log("POOL-VNC", "ERROR: Pool has no workers.")
        return 1

    # Get worker to connect to
    worker_name = getattr(args, "worker", None)
    all_workers = getattr(args, "all", False)

    ssh_user = _get_pool_ssh_username(pool)

    if all_workers:
        # Set up tunnels for all workers
        log("POOL-VNC", f"Setting up VNC tunnels for {len(workers)} workers...")
        tunnel_procs = []

        for i, worker in enumerate(workers):
            name = worker.get("name", f"worker-{i}")
            ip = worker.get("ip")
            if not ip:
                log("POOL-VNC", f"  {name}: No IP address, skipping")
                continue

            local_port = 8006 + i

            # Kill any existing tunnel on this port
            subprocess.run(
                ["pkill", "-f", f"ssh.*{local_port}:localhost:8006"],
                capture_output=True,
            )

            # Start SSH tunnel
            tunnel_proc = subprocess.Popen(
                [
                    "ssh",
                    *SSH_OPTS,
                    "-N",
                    "-L",
                    f"{local_port}:localhost:8006",
                    f"{ssh_user}@{ip}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            tunnel_procs.append((name, local_port, tunnel_proc))
            log("POOL-VNC", f"  {name}: http://localhost:{local_port}")

        time.sleep(2)

        # Check tunnel status
        log("POOL-VNC", "")
        log("POOL-VNC", "VNC Access URLs:")
        for name, port, proc in tunnel_procs:
            if proc.poll() is None:
                log("POOL-VNC", f"  {name}: http://localhost:{port}")
            else:
                log("POOL-VNC", f"  {name}: FAILED")

        log("POOL-VNC", "")
        log("POOL-VNC", "Press Ctrl+C to close all tunnels")

        try:
            # Keep tunnels alive
            while any(p.poll() is None for _, _, p in tunnel_procs):
                time.sleep(1)
        except KeyboardInterrupt:
            log("POOL-VNC", "Closing tunnels...")
            for _, _, p in tunnel_procs:
                p.terminate()

    else:
        # Connect to specific worker
        if not worker_name:
            # Show available workers
            log("POOL-VNC", "Available workers:")
            for i, worker in enumerate(workers):
                name = worker.get("name", f"worker-{i}")
                ip = worker.get("ip", "no IP")
                status = worker.get("status", "unknown")
                log("POOL-VNC", f"  {i}: {name} ({ip}) - {status}")
            log("POOL-VNC", "")
            log("POOL-VNC", "Usage: pool-vnc --worker <name>  OR  pool-vnc --all")
            return 1

        # Find the worker
        target_worker = None
        worker_idx = 0
        for i, worker in enumerate(workers):
            if worker.get("name") == worker_name:
                target_worker = worker
                worker_idx = i
                break

        if not target_worker:
            log("POOL-VNC", f"ERROR: Worker '{worker_name}' not found")
            return 1

        ip = target_worker.get("ip")
        if not ip:
            log("POOL-VNC", f"ERROR: Worker '{worker_name}' has no IP address")
            return 1

        local_port = 8006 + worker_idx

        # Kill any existing tunnel on this port
        subprocess.run(["pkill", "-f", f"ssh.*{local_port}:localhost:8006"], capture_output=True)

        # Start SSH tunnel
        log("POOL-VNC", f"Setting up VNC tunnel to {worker_name} ({ip})...")
        tunnel_proc = subprocess.Popen(
            [
                "ssh",
                *SSH_OPTS,
                "-N",
                "-L",
                f"{local_port}:localhost:8006",
                f"{ssh_user}@{ip}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        time.sleep(2)

        if tunnel_proc.poll() is not None:
            log("POOL-VNC", "ERROR: SSH tunnel failed to start")
            return 1

        vnc_url = f"http://localhost:{local_port}"
        log("POOL-VNC", f"VNC available at: {vnc_url}")

        # Open browser
        webbrowser.open(vnc_url)

        log("POOL-VNC", "Press Ctrl+C to close tunnel")
        try:
            tunnel_proc.wait()
        except KeyboardInterrupt:
            log("POOL-VNC", "Closing tunnel...")
            tunnel_proc.terminate()

    return 0


def cmd_pool_logs(args):
    """Stream logs from all pool workers interleaved with prefixes.

    Shows Docker container logs from each worker with [worker-name] prefix.
    Use Ctrl+C to stop.
    """
    import threading
    from queue import Empty, Queue

    init_logging()

    # Load pool registry
    registry_path = Path("benchmark_results/vm_pool_registry.json")
    if not registry_path.exists():
        print("ERROR: No pool found. Run pool-create first.")
        return 1

    with open(registry_path) as f:
        pool = json.load(f)

    workers = pool.get("workers", [])
    if not workers:
        print("ERROR: Pool has no workers.")
        return 1

    ssh_user = _get_pool_ssh_username(pool)
    pool_id = pool.get("pool_id", "unknown")
    print(f"[pool-logs] Streaming logs from {len(workers)} workers (pool: {pool_id})")
    print("[pool-logs] Press Ctrl+C to stop\n", flush=True)

    # Queue for collecting output from all workers
    output_queue = Queue()
    stop_event = threading.Event()

    def stream_worker_logs(worker_name: str, ip: str):
        """Stream logs from a single worker."""
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"{ssh_user}@{ip}",
            "docker logs -f winarena",
        ]
        try:
            proc = subprocess.Popen(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in iter(proc.stdout.readline, ""):
                if stop_event.is_set():
                    break
                output_queue.put((worker_name, line.rstrip()))
            proc.terminate()
        except Exception as e:
            output_queue.put((worker_name, f"ERROR: {e}"))

    # Start threads for each worker
    threads = []
    for worker in workers:
        name = worker.get("name", "unknown")
        ip = worker.get("ip")
        if not ip:
            print(f"[{name}] WARNING: No IP address", flush=True)
            continue
        t = threading.Thread(target=stream_worker_logs, args=(name, ip), daemon=True)
        t.start()
        threads.append(t)

    # Print output with prefixes
    try:
        while True:
            try:
                worker_name, line = output_queue.get(timeout=0.1)
                print(f"[{worker_name}] {line}", flush=True)
            except Empty:
                # Check if any threads are still alive
                if not any(t.is_alive() for t in threads):
                    print("[pool-logs] All workers disconnected", flush=True)
                    break
    except KeyboardInterrupt:
        print("\n[pool-logs] Stopping...", flush=True)
        stop_event.set()
        for t in threads:
            t.join(timeout=1)

    return 0


def cmd_pool_exec(args):
    """Execute a command on all pool workers.

    Runs the command on the VM host or inside the Docker container.
    """
    init_logging()

    # Load pool registry
    registry_path = Path("benchmark_results/vm_pool_registry.json")
    if not registry_path.exists():
        log("POOL-EXEC", "ERROR: No pool found. Run pool-create first.")
        return 1

    with open(registry_path) as f:
        pool = json.load(f)

    workers = pool.get("workers", [])
    if not workers:
        log("POOL-EXEC", "ERROR: Pool has no workers.")
        return 1

    ssh_user = _get_pool_ssh_username(pool)
    cmd = getattr(args, "cmd", None)
    docker = getattr(args, "docker", False)
    worker_filter = getattr(args, "worker", None)

    if not cmd:
        log("POOL-EXEC", "ERROR: --cmd required")
        return 1

    # Filter workers if specified
    if worker_filter:
        workers = [w for w in workers if w.get("name") == worker_filter]
        if not workers:
            log("POOL-EXEC", f"ERROR: Worker '{worker_filter}' not found")
            return 1

    # Run command on each worker
    for worker in workers:
        name = worker.get("name", "unknown")
        ip = worker.get("ip")
        if not ip:
            log("POOL-EXEC", f"[{name}] No IP address, skipping")
            continue

        if docker:
            full_cmd = f"docker exec winarena {cmd}"
        else:
            full_cmd = cmd

        log("POOL-EXEC", f"[{name}] Running: {full_cmd[:60]}...")

        try:
            result = subprocess.run(
                ["ssh", *SSH_OPTS, f"{ssh_user}@{ip}", full_cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout.strip() or result.stderr.strip()
            for line in output.split("\n")[:20]:  # Limit output lines
                log("POOL-EXEC", f"[{name}] {line}")
            if len(output.split("\n")) > 20:
                log("POOL-EXEC", f"[{name}] ... (truncated)")
        except subprocess.TimeoutExpired:
            log("POOL-EXEC", f"[{name}] TIMEOUT")
        except Exception as e:
            log("POOL-EXEC", f"[{name}] ERROR: {e}")

    return 0


def cmd_status(args):
    """Show VM status."""
    ip = get_vm_ip()
    state = get_vm_state()

    if not ip:
        print(f"VM '{VM_NAME}' not found")
        return 1

    print(f"VM: {VM_NAME}")
    print(f"  State: {state or 'unknown'}")
    print(f"  IP: {ip}")
    print(f"  Size: {VM_SIZE}")
    print(f"  SSH: ssh azureuser@{ip}")
    return 0


def cmd_build(args):
    """Build WAA image from waa_deploy/Dockerfile.

    This builds our custom image that:
    - Uses dockurr/windows:latest (has working ISO auto-download)
    - Copies WAA components from windowsarena/winarena:latest
    - Patches IP addresses and adds automation

    Supports both local (--local) and remote (default) builds.
    """
    init_logging()
    start_time = time.time()

    # Check Dockerfile exists
    if not DOCKERFILE_PATH.exists():
        log("BUILD", f"ERROR: Dockerfile not found: {DOCKERFILE_PATH}")
        return 1

    local_build = getattr(args, "local", False)
    push_to_acr = getattr(args, "push", False)
    acr_name = getattr(args, "acr", "openadaptacr")

    if local_build:
        return _build_local(args, start_time, push_to_acr, acr_name)
    else:
        return _build_remote(args, start_time, push_to_acr, acr_name)


def _build_local(args, start_time: float, push_to_acr: bool, acr_name: str) -> int:
    """Build WAA image locally using Docker for Mac/Linux."""
    log("BUILD", "Building WAA image LOCALLY...")
    log("BUILD", f"Dockerfile: {DOCKERFILE_PATH}")

    waa_deploy_dir = DOCKERFILE_PATH.parent

    # Build image locally
    log("BUILD", "Running docker build (this takes ~10-15 minutes)...")
    result = subprocess.run(
        ["docker", "build", "--pull", "-t", DOCKER_IMAGE, "."],
        cwd=str(waa_deploy_dir),
        capture_output=False,  # Stream output
    )

    elapsed = time.time() - start_time
    if result.returncode != 0:
        log("BUILD", f"ERROR: Docker build failed after {elapsed:.1f}s")
        return 1

    log("BUILD", f"Image built: {DOCKER_IMAGE} ({elapsed:.1f}s)")

    # Push to ACR if requested
    if push_to_acr:
        return _push_to_acr(DOCKER_IMAGE, acr_name, start_time)

    return 0


def _build_remote(args, start_time: float, push_to_acr: bool, acr_name: str) -> int:
    """Build WAA image on remote Azure VM."""
    ip = get_vm_ip()
    if not ip:
        log("BUILD", "ERROR: VM not found. Run 'create' first or use --local.")
        return 1

    log("BUILD", f"Building WAA image on REMOTE VM ({ip})...")

    # Copy Dockerfile and supporting files to VM
    log("BUILD", "Copying build files to VM...")
    ssh_run(ip, "mkdir -p ~/build")

    waa_deploy_dir = DOCKERFILE_PATH.parent
    files_to_copy = [
        "Dockerfile",
        "start_waa_server.bat",
        "api_agent.py",
        "evaluate_server.py",
        "start_with_evaluate.sh",
        "patch_setup_ps1.sh",
    ]
    for filename in files_to_copy:
        src = waa_deploy_dir / filename
        if src.exists():
            result = subprocess.run(
                ["scp", *SSH_OPTS, str(src), f"azureuser@{ip}:~/build/"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log("BUILD", f"ERROR: Failed to copy {filename}: {result.stderr}")
                return 1

    # Pre-build cleanup
    log("BUILD", "Cleaning up dangling images before build...")
    ssh_run(ip, "docker image prune -f 2>/dev/null")

    # Build image (streams output)
    log("BUILD", "Running docker build (this takes ~10-15 minutes)...")
    build_cmd = f"cd ~/build && docker build --pull -t {DOCKER_IMAGE} . 2>&1"
    result = ssh_run(ip, build_cmd, stream=True, step="BUILD")

    elapsed = time.time() - start_time
    if result.returncode != 0:
        log("BUILD", f"ERROR: Docker build failed after {elapsed:.1f}s")
        return 1

    # Post-build cleanup
    log("BUILD", "Cleaning up dangling images after build...")
    ssh_run(ip, "docker image prune -f 2>/dev/null")

    log("BUILD", f"Image built: {DOCKER_IMAGE} ({elapsed:.1f}s)")

    # Push to ACR if requested
    if push_to_acr:
        return _push_to_acr_remote(ip, DOCKER_IMAGE, acr_name, start_time)

    return 0


def _push_to_acr(image: str, acr_name: str, start_time: float) -> int:
    """Push image to Azure Container Registry (local Docker)."""
    acr_image = f"{acr_name}.azurecr.io/{image}"
    log("BUILD", f"Pushing to ACR: {acr_image}")

    # Login to ACR
    log("BUILD", "Logging into ACR...")
    result = subprocess.run(
        ["az", "acr", "login", "--name", acr_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log("BUILD", f"ERROR: ACR login failed: {result.stderr}")
        return 1

    # Tag and push
    log("BUILD", "Tagging image...")
    subprocess.run(["docker", "tag", image, acr_image], check=True)

    log("BUILD", "Pushing image (this may take a while)...")
    result = subprocess.run(["docker", "push", acr_image])

    elapsed = time.time() - start_time
    if result.returncode != 0:
        log("BUILD", f"ERROR: Push failed after {elapsed:.1f}s")
        return 1

    log("BUILD", f"Pushed: {acr_image} (total: {elapsed:.1f}s)")
    return 0


def _push_to_acr_remote(ip: str, image: str, acr_name: str, start_time: float) -> int:
    """Push image to Azure Container Registry (from remote VM)."""
    acr_image = f"{acr_name}.azurecr.io/{image}"
    log("BUILD", f"Pushing to ACR from VM: {acr_image}")

    # Login to ACR on VM
    log("BUILD", "Logging into ACR on VM...")
    result = ssh_run(ip, f"az acr login --name {acr_name}")
    if result.returncode != 0:
        log("BUILD", "ERROR: ACR login failed on VM")
        return 1

    # Tag and push
    log("BUILD", "Tagging and pushing image...")
    push_cmd = f"docker tag {image} {acr_image} && docker push {acr_image}"
    result = ssh_run(ip, push_cmd, stream=True, step="BUILD")

    elapsed = time.time() - start_time
    if result.returncode != 0:
        log("BUILD", f"ERROR: Push failed after {elapsed:.1f}s")
        return 1

    log("BUILD", f"Pushed: {acr_image} (total: {elapsed:.1f}s)")
    return 0


def cmd_build_status(args):
    """Check status of Docker build on remote VM."""
    init_logging()

    ip = get_vm_ip()
    if not ip:
        log("BUILD-STATUS", "ERROR: VM not found.")
        return 1

    lines = getattr(args, "lines", 30)

    # Find most recent build log
    result = ssh_run(
        ip,
        "ls -t ~/cli_logs/build_*.log 2>/dev/null | head -1",
    )
    if result.returncode != 0 or not result.stdout.strip():
        log("BUILD-STATUS", "No build logs found")
        return 0

    log_file = result.stdout.strip()
    log("BUILD-STATUS", f"Latest build log: {log_file}")

    # Check if build is still running
    result = ssh_run(ip, "pgrep -f 'docker build' >/dev/null && echo RUNNING || echo DONE")
    status = "RUNNING" if "RUNNING" in result.stdout else "DONE"
    log("BUILD-STATUS", f"Build status: {status}")

    # Show log tail
    log("BUILD-STATUS", f"\n--- Last {lines} lines ---")
    subprocess.run(
        [
            "ssh",
            *SSH_OPTS,
            f"azureuser@{ip}",
            f"tail -{lines} {log_file}",
        ],
    )

    # If done, check exit code
    if status == "DONE":
        exit_file = log_file + ".exit"
        result = ssh_run(ip, f"cat {exit_file} 2>/dev/null")
        exit_code = result.stdout.strip()
        if exit_code == "0":
            log("BUILD-STATUS", "\nBuild completed successfully!")
        elif exit_code:
            log("BUILD-STATUS", f"\nBuild failed with exit code: {exit_code}")
        else:
            log("BUILD-STATUS", "\nBuild status unknown (no exit file)")

    return 0


def cmd_push_acr(args):
    """Push Docker image to Azure Container Registry.

    Handles:
    - Installing Azure CLI if missing
    - Logging into ACR
    - Tagging and pushing the image
    """
    init_logging()
    start_time = time.time()

    acr_name = getattr(args, "acr", "openadaptacr")
    image = getattr(args, "image", DOCKER_IMAGE)
    local = getattr(args, "local", False)

    acr_image = f"{acr_name}.azurecr.io/{image}"

    if local:
        # Push from local Docker
        log("PUSH-ACR", f"Pushing {image} to {acr_image} (local Docker)...")

        # Login to ACR
        log("PUSH-ACR", "Logging into ACR...")
        result = subprocess.run(
            ["az", "acr", "login", "--name", acr_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log("PUSH-ACR", f"ERROR: ACR login failed: {result.stderr}")
            return 1

        # Tag and push
        log("PUSH-ACR", "Tagging image...")
        subprocess.run(["docker", "tag", image, acr_image], check=True)

        log("PUSH-ACR", "Pushing image (this may take a while)...")
        result = subprocess.run(["docker", "push", acr_image])

    else:
        # Push from VM
        ip = get_vm_ip()
        if not ip:
            log("PUSH-ACR", "ERROR: VM not found.")
            return 1

        log("PUSH-ACR", f"Pushing {image} to {acr_image} (from VM)...")

        # Check if az is installed
        log("PUSH-ACR", "Checking Azure CLI...")
        result = ssh_run(ip, "which az >/dev/null 2>&1 && echo FOUND || echo MISSING")

        if "MISSING" in result.stdout:
            log("PUSH-ACR", "Azure CLI not found. Installing...")
            install_cmd = "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
            result = ssh_run(ip, install_cmd, stream=True, step="PUSH-ACR")
            if result.returncode != 0:
                log("PUSH-ACR", "ERROR: Failed to install Azure CLI")
                return 1
            log("PUSH-ACR", "Azure CLI installed")

        # Login using service principal
        log("PUSH-ACR", "Logging into Azure with service principal...")
        from openadapt_evals.config import settings

        if not all(
            [
                settings.azure_client_id,
                settings.azure_client_secret,
                settings.azure_tenant_id,
            ]
        ):
            log("PUSH-ACR", "ERROR: Missing Azure service principal credentials in .env")
            log(
                "PUSH-ACR",
                "  Required: AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID",
            )
            return 1

        # Login with service principal
        sp_login_cmd = (
            f"az login --service-principal "
            f"-u {settings.azure_client_id} "
            f"-p {settings.azure_client_secret} "
            f"--tenant {settings.azure_tenant_id}"
        )
        result = ssh_run(ip, sp_login_cmd)
        if result.returncode != 0:
            log("PUSH-ACR", "ERROR: Service principal login failed")
            return 1

        # Login to ACR
        log("PUSH-ACR", "Logging into ACR...")
        result = ssh_run(ip, f"az acr login --name {acr_name}")
        if result.returncode != 0:
            log("PUSH-ACR", "ERROR: ACR login failed")
            return 1

        # Tag and push
        log("PUSH-ACR", "Tagging and pushing image...")
        push_cmd = f"docker tag {image} {acr_image} && docker push {acr_image}"
        result = ssh_run(ip, push_cmd, stream=True, step="PUSH-ACR")

    elapsed = time.time() - start_time
    if result.returncode != 0:
        log("PUSH-ACR", f"ERROR: Push failed after {elapsed:.1f}s")
        return 1

    log("PUSH-ACR", f"Successfully pushed: {acr_image} ({elapsed:.1f}s)")
    return 0


def cmd_image_create(args):
    """Create a golden image from an existing pool VM.

    The VM is deallocated, generalized, and converted into an Azure Managed
    Image. Future pool-create --image <id> skips Docker setup entirely.

    WARNING: The source VM is destroyed (cannot be restarted after generalize).
    """
    init_logging()
    from openadapt_evals.infrastructure.azure_vm import AzureVMManager
    from openadapt_evals.infrastructure.vm_monitor import VMPoolRegistry

    vm_manager = AzureVMManager(resource_group=RESOURCE_GROUP)
    registry = VMPoolRegistry()
    pool = registry.get_pool()

    if pool is None:
        log("IMAGE", "ERROR: No active pool. Create one first with: pool-create --workers 1")
        return 1

    # Use specified worker or first one
    worker_name = getattr(args, "worker", None) or pool.workers[0].name
    worker = next((w for w in pool.workers if w.name == worker_name), None)
    if not worker:
        log("IMAGE", f"ERROR: Worker {worker_name} not found in pool")
        return 1

    image_name = getattr(args, "name", None)
    if not image_name:
        from datetime import datetime
        image_name = f"waa-golden-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    log("IMAGE", f"Creating golden image from {worker_name}...")
    log("IMAGE", f"  Image name: {image_name}")
    log("IMAGE", "  WARNING: Source VM will be destroyed after generalization")

    # Step 1: Clean up the VM
    log("IMAGE", "Cleaning up VM state...")
    from openadapt_evals.infrastructure.azure_vm import ssh_run
    ssh_run(worker.ip, "docker stop winarena 2>/dev/null || true")
    ssh_run(worker.ip, "sudo apt-get clean && sudo rm -rf /tmp/* /var/tmp/*")
    ssh_run(worker.ip, "sudo waagent -deprovision+user -force")

    # Step 2: Deallocate
    log("IMAGE", "Deallocating VM...")
    if not vm_manager.deallocate_vm(worker_name):
        log("IMAGE", "ERROR: Failed to deallocate VM")
        return 1

    # Step 3: Generalize
    log("IMAGE", "Generalizing VM...")
    if not vm_manager.generalize_vm(worker_name):
        log("IMAGE", "ERROR: Failed to generalize VM")
        return 1

    # Step 4: Create image
    log("IMAGE", "Creating managed image (this may take a few minutes)...")
    image_id = vm_manager.create_image(worker_name, image_name)
    if not image_id:
        log("IMAGE", "ERROR: Failed to create image")
        return 1

    # Step 5: Clean up source VM (can't restart after generalize)
    log("IMAGE", "Cleaning up source VM (no longer usable)...")
    vm_manager.delete_vm(worker_name)
    registry.delete_pool()

    log("IMAGE", "=" * 60)
    log("IMAGE", f"Golden image created: {image_name}")
    log("IMAGE", f"  ID: {image_id}")
    log("IMAGE", "")
    log("IMAGE", "Use with pool-create:")
    log("IMAGE", f"  oa-vm pool-create --workers 1 --image {image_id}")
    log("IMAGE", "=" * 60)
    return 0


def cmd_image_list(args):
    """List available golden images."""
    init_logging()
    from openadapt_evals.infrastructure.azure_vm import AzureVMManager

    vm_manager = AzureVMManager(resource_group=RESOURCE_GROUP)
    images = vm_manager.list_images(prefix="waa-golden")

    if not images:
        log("IMAGE", "No golden images found")
        return 0

    log("IMAGE", f"Found {len(images)} golden image(s):")
    for img in images:
        log("IMAGE", f"  {img['name']}")
        log("IMAGE", f"    ID: {img['id']}")
        log("IMAGE", f"    Location: {img['location']}")
    return 0


def cmd_image_delete(args):
    """Delete a golden image."""
    init_logging()
    from openadapt_evals.infrastructure.azure_vm import AzureVMManager

    image_name = args.name
    vm_manager = AzureVMManager(resource_group=RESOURCE_GROUP)

    log("IMAGE", f"Deleting image: {image_name}")
    if vm_manager.delete_image(image_name):
        log("IMAGE", "Image deleted")
        return 0
    else:
        log("IMAGE", "ERROR: Failed to delete image")
        return 1


def cmd_start(args):
    """Start WAA container."""
    init_logging()

    ip = get_vm_ip()
    if not ip:
        log("START", "ERROR: VM not found. Run 'create' first.")
        return 1

    log("START", "Starting WAA container...")

    # Stop existing container
    log("START", "Stopping any existing container...")
    ssh_run(ip, "docker stop winarena 2>/dev/null; docker rm -f winarena 2>/dev/null")

    # Clean storage if --fresh
    if args.fresh:
        log("START", "Cleaning storage for fresh Windows install...")
        ssh_run(ip, f"sudo rm -rf {WAA_STORAGE_DIR}/*")

    # Create storage directory (on persistent OS disk, not ephemeral /mnt)
    ssh_run(
        ip,
        f"sudo mkdir -p {WAA_STORAGE_DIR} && sudo chown azureuser:azureuser {WAA_STORAGE_DIR}",
    )

    # Start container
    # Our custom image has ENTRYPOINT that handles everything:
    # - Downloads Windows 11 Enterprise if not present
    # - Boots QEMU VM
    # - Runs WAA server automatically via FirstLogonCommands
    # QEMU resource allocation — always use 16G/6 cores (D8ds_v5, 32GB host)
    ram_size = "16G"
    cpu_cores = 6
    log("START", "Starting container with VERSION=11e (6 cores, 16GB RAM)...")

    # Get agent and model from args (defaults match WAA defaults)
    getattr(args, "agent", "navi")
    getattr(args, "model", "gpt-4o")
    getattr(args, "som_origin", "oss")
    getattr(args, "a11y_backend", "uia")

    # Our waa-auto:latest image uses a custom ENTRYPOINT (start_with_evaluate.sh)
    # that starts evaluate_server.py on port 5050 before running entry.sh
    docker_cmd = f"""docker run -d \\
  --name winarena \\
  --device=/dev/kvm \\
  --cap-add NET_ADMIN \\
  --stop-timeout 120 \\
  -p 8006:8006 \\
  -p 5000:5000 \\
  -p 7200:7200 \\
  -v {WAA_STORAGE_DIR}:/storage \\
  -e VERSION=11e \\
  -e RAM_SIZE={ram_size} \\
  -e CPU_CORES={cpu_cores} \\
  -e DISK_SIZE=64G \\
  -p 5050:5050 \\
  {DOCKER_IMAGE} \\
  /run/entry.sh --prepare-image false --start-client false"""
    # Note: --start-client false means just boot Windows + Flask server
    # The benchmark client is started separately by the 'run' command

    result = ssh_run(ip, docker_cmd)
    if result.returncode != 0:
        log("START", f"ERROR: Failed to start container: {result.stderr}")
        return 1

    log("START", "Container started")
    log("START", "Windows will boot and install (15-20 min on first run)")

    # Auto-launch VNC unless --no-vnc specified
    if not getattr(args, "no_vnc", False):
        log("START", "Auto-launching VNC viewer...")
        tunnel_proc = setup_vnc_tunnel_and_browser(ip)
        if tunnel_proc:
            log(
                "START",
                f"VNC auto-launched at http://localhost:8006 (tunnel PID: {tunnel_proc.pid})",
            )
        else:
            log("START", "WARNING: VNC tunnel failed to start")
            log("START", f"Manual VNC: ssh -L 8006:localhost:8006 azureuser@{ip}")
    else:
        log("START", f"VNC (via SSH tunnel): ssh -L 8006:localhost:8006 azureuser@{ip}")

    return 0


def cmd_stop(args):
    """Stop and remove WAA container."""
    ip = get_vm_ip()
    if not ip:
        print("ERROR: VM not found")
        return 1

    print(f"Stopping container on VM ({ip})...")

    # Stop container
    result = ssh_run(ip, "docker stop winarena 2>/dev/null && echo STOPPED || echo NOT_RUNNING")
    if "STOPPED" in result.stdout:
        print("  Container stopped")
    else:
        print("  Container was not running")

    # Remove container
    result = ssh_run(ip, "docker rm -f winarena 2>/dev/null && echo REMOVED || echo NOT_FOUND")
    if "REMOVED" in result.stdout:
        print("  Container removed")
    else:
        print("  Container already removed")

    # Optionally clean storage
    if hasattr(args, "clean") and args.clean:
        print("  Cleaning Windows storage...")
        ssh_run(ip, f"sudo rm -rf {WAA_STORAGE_DIR}/*")
        print("  Storage cleaned")

    print("Done")
    return 0


def cmd_test_golden_image(args):
    """Test that golden image boots and WAA server responds.

    This validates the golden image before uploading to Azure blob storage.
    A successful test means:
    1. Container starts from existing storage (not fresh install)
    2. Windows boots from golden image
    3. WAA server responds to probe within timeout

    Expected boot time: ~30-60 seconds (vs 15-20 min for fresh install)
    """
    init_logging()

    ip = get_vm_ip()
    if not ip:
        log("TEST", "ERROR: VM not found. Run 'create' first.")
        return 1

    # Check if golden image exists
    log("TEST", "Checking for golden image...")
    result = ssh_run(ip, f"ls -la {WAA_STORAGE_DIR}/data.img 2>/dev/null || echo 'NOT_FOUND'")
    if "NOT_FOUND" in result.stdout:
        log("TEST", f"ERROR: Golden image not found at {WAA_STORAGE_DIR}/data.img")
        log(
            "TEST",
            "  Run 'start' first to create a golden image, then wait for Windows to install",
        )
        return 1

    # Get image size
    size_result = ssh_run(ip, f"du -h {WAA_STORAGE_DIR}/data.img 2>/dev/null | cut -f1")
    image_size = size_result.stdout.strip() or "unknown"
    log("TEST", f"Found golden image: {image_size}")

    # Stop existing container
    log("TEST", "Stopping existing container...")
    ssh_run(ip, "docker stop winarena 2>/dev/null; docker rm winarena 2>/dev/null")

    # Start container from golden image (NOT fresh)
    log("TEST", "Starting container from golden image...")

    # 16GB RAM / 6 cores for D8ds_v5 VM
    ram_size = "16G"
    cpu_cores = 6
    log("TEST", f"  Using {cpu_cores} cores, {ram_size} RAM")

    docker_cmd = f"""docker run -d \\
  --name winarena \\
  --device=/dev/kvm \\
  --cap-add NET_ADMIN \\
  --stop-timeout 120 \\
  -p 8006:8006 \\
  -p 5000:5000 \\
  -p 7200:7200 \\
  -v {WAA_STORAGE_DIR}:/storage \\
  -e VERSION=11e \\
  -e RAM_SIZE={ram_size} \\
  -e CPU_CORES={cpu_cores} \\
  -e DISK_SIZE=64G \\
  -p 5050:5050 \\
  {DOCKER_IMAGE} \\
  /run/entry.sh --prepare-image false --start-client false"""

    result = ssh_run(ip, docker_cmd)
    if result.returncode != 0:
        log("TEST", f"ERROR: Failed to start container: {result.stderr}")
        return 1

    log("TEST", "Container started, waiting for WAA server...")

    # Wait for probe
    start_time = time.time()
    timeout = args.timeout

    while True:
        result = ssh_run(
            ip,
            "docker exec winarena curl -s --max-time 5 http://172.30.0.2:5000/probe 2>/dev/null || echo FAIL",
        )

        if "FAIL" not in result.stdout and result.stdout.strip():
            elapsed = time.time() - start_time
            log("TEST", "")
            log("TEST", "✅ GOLDEN IMAGE TEST PASSED")
            log("TEST", f"  Boot time: {elapsed:.1f} seconds")
            log("TEST", f"  Image size: {image_size}")
            log("TEST", f"  Response: {result.stdout.strip()[:80]}")
            return 0

        elapsed = time.time() - start_time
        if elapsed > timeout:
            log("TEST", "")
            log("TEST", "❌ GOLDEN IMAGE TEST FAILED")
            log("TEST", f"  WAA server did not respond after {timeout}s")
            log("TEST", "  This may indicate a corrupted golden image")
            log("TEST", "  Try: cli.py start --fresh  # to create new golden image")
            return 1

        # Progress display
        print(f"  [{int(elapsed):3d}s] Waiting for WAA server...", end="\r")
        time.sleep(5)


def cmd_test_blob_access(args):
    """Test Azure blob storage access for golden image.

    Verifies:
    1. Azure CLI is authenticated on VM
    2. Storage account is accessible
    3. Golden image container exists
    4. Can list/read blobs (permissions work)
    """
    init_logging()

    ip = get_vm_ip()
    if not ip:
        log("TEST-BLOB", "ERROR: VM not found. Run 'create' first.")
        return 1

    log("TEST-BLOB", "Testing Azure blob storage access...")

    # Check Azure CLI login
    log("TEST-BLOB", "1. Checking Azure CLI authentication...")
    result = ssh_run(ip, "az account show --query name -o tsv 2>/dev/null || echo 'NOT_LOGGED_IN'")
    if "NOT_LOGGED_IN" in result.stdout:
        log("TEST-BLOB", "❌ Azure CLI not logged in on VM")
        log("TEST-BLOB", "   Run: az login --identity  (if using managed identity)")
        return 1
    log("TEST-BLOB", f"   ✓ Logged in as: {result.stdout.strip()}")

    # Get storage account - try to detect it dynamically
    result = ssh_run(ip, "az storage account list --query '[0].name' -o tsv 2>/dev/null")
    storage_account = result.stdout.strip()
    if not storage_account:
        storage_account = "openadaptstorage"  # Fallback
    # Try common container names for golden images
    container_candidates = [
        "waa-golden-images",
        "azureml",
        "golden-images",
    ]
    # Also check for azureml-blobstore-* containers
    result = ssh_run(
        ip,
        f"az storage container list --account-name {storage_account} --auth-mode login --query '[].name' -o tsv 2>/dev/null",
    )
    available_containers = result.stdout.strip().split("\n") if result.stdout.strip() else []
    azureml_containers = [c for c in available_containers if c.startswith("azureml")]
    container_candidates = azureml_containers + container_candidates

    container_name = None  # Will be set if found

    # Check storage account access
    log("TEST-BLOB", f"2. Checking storage account: {storage_account}...")
    result = ssh_run(
        ip,
        f"az storage account show --name {storage_account} --query 'name' -o tsv 2>/dev/null || echo 'NOT_FOUND'",
    )
    if "NOT_FOUND" in result.stdout:
        log(
            "TEST-BLOB",
            f"❌ Storage account '{storage_account}' not found or not accessible",
        )
        return 1
    log("TEST-BLOB", "   ✓ Storage account accessible")

    # Check container exists - try candidates in order
    log("TEST-BLOB", "3. Checking for usable container...")
    for candidate in container_candidates:
        if not candidate:
            continue
        result = ssh_run(
            ip,
            f"az storage container exists --name {candidate} --account-name {storage_account} --auth-mode login --query exists -o tsv 2>/dev/null || echo 'ERROR'",
        )
        if result.stdout.strip() == "true":
            container_name = candidate
            log("TEST-BLOB", f"   ✓ Found container: {container_name}")
            break

    if not container_name:
        log("TEST-BLOB", "❌ No suitable container found")
        log("TEST-BLOB", f"   Available: {available_containers}")
        log(
            "TEST-BLOB",
            f"   Create one with: az storage container create --name waa-golden-images --account-name {storage_account}",
        )
        return 1

    # List blobs in container (optional - may fail due to permissions)
    log("TEST-BLOB", "4. Listing blobs in container...")
    result = ssh_run(
        ip,
        f"az storage blob list --container-name {container_name} --account-name {storage_account} --auth-mode login --query '[].{{name:name, size:properties.contentLength}}' -o table 2>/dev/null || echo 'ERROR'",
    )
    if "ERROR" in result.stdout:
        log(
            "TEST-BLOB",
            "   ⚠ Cannot list blobs (may be normal - some containers restrict listing)",
        )
        log(
            "TEST-BLOB",
            "   Container exists, which is sufficient for golden image upload",
        )
    else:
        blob_output = result.stdout.strip()
        if not blob_output or "Name" not in blob_output:
            log("TEST-BLOB", "   ⚠ Container is empty (no golden image uploaded yet)")
        else:
            log("TEST-BLOB", "   ✓ Blobs found:")
            for line in blob_output.split("\n")[:10]:  # Show first 10
                log("TEST-BLOB", f"     {line}")

    log("TEST-BLOB", "")
    log("TEST-BLOB", "✅ BLOB ACCESS TEST PASSED")
    return 0


def cmd_test_api_key(args):
    """Test that OpenAI API key is valid.

    Makes a minimal API call to verify the key works.
    """
    init_logging()

    # Get API key from args or environment
    api_key = args.api_key
    if not api_key:
        try:
            from openadapt_evals.config import settings

            api_key = settings.openai_api_key
        except Exception:
            pass

    if not api_key:
        import os

        api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        log("TEST-API", "❌ No API key found")
        log("TEST-API", "   Set OPENAI_API_KEY in .env or pass --api-key")
        return 1

    # Mask key for display
    masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
    log("TEST-API", f"Testing API key: {masked}")

    # Make minimal API call
    log("TEST-API", "Making test API call...")
    try:
        import httpx

        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Say 'OK'"}],
                "max_tokens": 5,
            },
            timeout=30,
        )

        if response.status_code == 200:
            log("TEST-API", "✅ API KEY TEST PASSED")
            log("TEST-API", "   Model: gpt-4o-mini responded successfully")
            return 0
        elif response.status_code == 401:
            log("TEST-API", "❌ API KEY INVALID (401 Unauthorized)")
            return 1
        elif response.status_code == 429:
            log("TEST-API", "⚠ API KEY VALID but rate limited (429)")
            log("TEST-API", "   This is OK - key works, just hit rate limit")
            return 0
        else:
            log("TEST-API", f"❌ Unexpected response: {response.status_code}")
            log("TEST-API", f"   {response.text[:200]}")
            return 1
    except Exception as e:
        log("TEST-API", f"❌ API call failed: {e}")
        return 1


def cmd_test_waa_tasks(args):
    """Test WAA task list accessibility.

    Verifies the task definitions are accessible and parses them.
    """
    init_logging()

    ip = get_vm_ip()
    if not ip:
        log("TEST-TASKS", "ERROR: VM not found. Run 'create' first.")
        return 1

    log("TEST-TASKS", "Testing WAA task accessibility...")

    # Check if container is running
    result = ssh_run(ip, "docker ps --filter name=winarena --format '{{.Status}}' 2>/dev/null")
    if not result.stdout.strip():
        log("TEST-TASKS", "❌ Container not running. Start it first with 'start'")
        return 1
    log("TEST-TASKS", f"Container status: {result.stdout.strip()}")

    # Try to get task list from WAA
    log("TEST-TASKS", "Fetching task list from WAA server...")
    result = ssh_run(
        ip,
        "docker exec winarena curl -s --max-time 10 http://172.30.0.2:5000/tasks 2>/dev/null || echo 'FAIL'",
    )

    if "FAIL" in result.stdout or not result.stdout.strip():
        # Fall back to checking task files directly
        log("TEST-TASKS", "WAA /tasks endpoint not available, checking task files...")
        result = ssh_run(
            ip,
            "docker exec winarena ls -la /app/WindowsAgentArena/src/win-arena-container/tasks/ 2>/dev/null | head -20 || echo 'NOT_FOUND'",
        )
        if "NOT_FOUND" in result.stdout:
            log("TEST-TASKS", "❌ Task directory not found")
            return 1
        log("TEST-TASKS", "Task files found:")
        for line in result.stdout.strip().split("\n")[:10]:
            log("TEST-TASKS", f"   {line}")
    else:
        # Parse task list
        try:
            tasks = json.loads(result.stdout)
            # Handle both list and dict responses
            if isinstance(tasks, dict):
                # Could be {"tasks": [...]} or {"task_id": {...}, ...}
                if "tasks" in tasks:
                    task_list = tasks["tasks"]
                else:
                    task_list = list(tasks.values()) if tasks else []
            else:
                task_list = tasks if isinstance(tasks, list) else []

            log("TEST-TASKS", f"✓ Found {len(task_list)} tasks")
            # Show sample tasks
            for task in task_list[:5]:
                if isinstance(task, dict):
                    task_id = task.get("id", task.get("task_id", "unknown"))
                    domain = task.get("domain", "unknown")
                    log("TEST-TASKS", f"   - {task_id} ({domain})")
                else:
                    log("TEST-TASKS", f"   - {task}")
            if len(task_list) > 5:
                log("TEST-TASKS", f"   ... and {len(task_list) - 5} more")
        except json.JSONDecodeError:
            log("TEST-TASKS", f"Response (not JSON): {result.stdout[:200]}")

    log("TEST-TASKS", "")
    log("TEST-TASKS", "✅ WAA TASKS TEST PASSED")
    return 0


def cmd_test_all(args):
    """Run all pre-flight tests before Azure ML benchmark.

    Runs in sequence:
    1. test-golden-image - Verify golden image boots
    2. test-api-key - Verify OpenAI API key
    3. test-blob-access - Verify Azure blob storage
    4. test-waa-tasks - Verify task accessibility
    """
    init_logging()

    log("TEST-ALL", "=" * 50)
    log("TEST-ALL", "Running all pre-flight tests...")
    log("TEST-ALL", "=" * 50)

    results = {}

    # Test 1: API Key (fast, no VM needed)
    log("TEST-ALL", "")
    log("TEST-ALL", "[1/4] Testing API key...")
    log("TEST-ALL", "-" * 30)

    class FakeArgs:
        api_key = getattr(args, "api_key", None)

    results["api_key"] = cmd_test_api_key(FakeArgs()) == 0

    # Test 2: Golden Image
    log("TEST-ALL", "")
    log("TEST-ALL", "[2/4] Testing golden image...")
    log("TEST-ALL", "-" * 30)

    class FakeArgs2:
        timeout = 120

    results["golden_image"] = cmd_test_golden_image(FakeArgs2()) == 0

    # Test 3: WAA Tasks (requires running container from golden image test)
    log("TEST-ALL", "")
    log("TEST-ALL", "[3/4] Testing WAA tasks...")
    log("TEST-ALL", "-" * 30)

    class FakeArgs3:
        pass

    results["waa_tasks"] = cmd_test_waa_tasks(FakeArgs3()) == 0

    # Test 4: Blob Access
    log("TEST-ALL", "")
    log("TEST-ALL", "[4/4] Testing blob storage access...")
    log("TEST-ALL", "-" * 30)

    class FakeArgs4:
        pass

    results["blob_access"] = cmd_test_blob_access(FakeArgs4()) == 0

    # Summary
    log("TEST-ALL", "")
    log("TEST-ALL", "=" * 50)
    log("TEST-ALL", "TEST SUMMARY")
    log("TEST-ALL", "=" * 50)

    all_passed = True
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        log("TEST-ALL", f"  {test_name}: {status}")
        if not passed:
            all_passed = False

    log("TEST-ALL", "")
    if all_passed:
        log("TEST-ALL", "✅ ALL TESTS PASSED - Ready for Azure ML benchmark!")
        return 0
    else:
        log("TEST-ALL", "❌ SOME TESTS FAILED - Fix issues before running benchmark")
        return 1


def cmd_smoke_test_aws(args):
    """Smoke-test the AWS VM backend with incremental stages.

    Stages 1-5 are read-only (credentials, SSH key, AMI, instance type, VPC).
    Stages 6-9 require --full and create/stop/start/delete a real EC2 instance.
    """
    init_logging()

    region = getattr(args, "region", None)
    instance_type = getattr(args, "instance_type", "m5a.xlarge")
    full = getattr(args, "full", False)
    vm_name = "waa-smoke-test"
    prefix = "SMOKE-AWS"
    passed = 0
    failed = 0
    total_read_only = 5
    total_full = 9

    def stage_pass(n, desc):
        nonlocal passed
        passed += 1
        log(prefix, f"Stage {n}: {desc} ... PASS")

    def stage_fail(n, desc, err):
        nonlocal failed
        failed += 1
        log(prefix, f"Stage {n}: {desc} ... FAIL")
        log(prefix, f"  Error: {err}")

    log(prefix, "=" * 60)
    log(prefix, f"AWS Smoke Test ({'full lifecycle' if full else 'read-only'})")
    log(prefix, "=" * 60)

    # --- Stage 1: Credentials ---
    try:
        import boto3

        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        log(prefix, f"  Account: {identity['Account']}, ARN: {identity['Arn']}")
        stage_pass(1, "AWS credentials")
    except Exception as e:
        stage_fail(1, "AWS credentials", e)
        log(prefix, "Cannot continue without valid AWS credentials.")
        return 1

    # --- Stage 2: SSH key ---
    ssh_pub = Path.home() / ".ssh" / "id_rsa.pub"
    if ssh_pub.exists():
        key_size = ssh_pub.stat().st_size
        log(prefix, f"  Key: {ssh_pub} ({key_size} bytes)")
        stage_pass(2, "SSH public key")
    else:
        stage_fail(2, "SSH public key", f"{ssh_pub} not found")
        log(prefix, "Cannot continue without SSH key (needed for VPC infra).")
        return 1

    # --- Stage 3: AMI lookup ---
    from openadapt_evals.infrastructure.aws_vm import AWSVMManager

    mgr = AWSVMManager()
    if region:
        mgr.region = region

    try:
        ami = mgr._find_latest_ubuntu_ami(region=mgr.region)
        log(prefix, f"  AMI: {ami} (region: {mgr.region})")
        stage_pass(3, "AMI lookup")
    except Exception as e:
        stage_fail(3, "AMI lookup", e)
        log(prefix, "Cannot continue without a valid AMI.")
        return 1

    # --- Stage 4: Instance type availability ---
    try:
        found_type, found_region, cost = mgr.find_available_size_and_region()
        log(prefix, f"  Type: {found_type}, Region: {found_region}, Cost: ${cost:.3f}/hr")
        stage_pass(4, "Instance type availability")
    except Exception as e:
        stage_fail(4, "Instance type availability", e)
        log(prefix, "No available instance type/region found (non-fatal for read-only).")
        # Non-fatal: we can still test VPC infra in the current region

    # --- Stage 5: VPC infrastructure ---
    try:
        infra = mgr._ensure_vpc_infrastructure(region=mgr.region)
        log(prefix, f"  Subnet: {infra['subnet_id']}")
        log(prefix, f"  Security group: {infra['security_group_id']}")
        log(prefix, f"  Key pair: {infra['key_name']}")
        stage_pass(5, "VPC infrastructure")
    except Exception as e:
        stage_fail(5, "VPC infrastructure", e)
        if full:
            log(prefix, "Cannot continue to lifecycle stages without VPC.")
            return 1

    # --- Summary for read-only stages ---
    if not full:
        log(prefix, "")
        log(prefix, "-" * 60)
        log(prefix, f"Read-only stages: {passed} passed, {failed} failed (of {total_read_only})")
        log(prefix, "Run with --full to test VM lifecycle (stages 6-9).")
        return 1 if failed > 0 else 0

    # === Full lifecycle stages (6-9) ===
    log(prefix, "")
    log(prefix, "--- Full lifecycle (creates real EC2 resources) ---")

    ip = None
    try:
        # --- Stage 6: Create VM ---
        try:
            log(prefix, f"  Creating {vm_name} ({instance_type} in {mgr.region})...")
            result = mgr.create_vm(
                name=vm_name,
                region=mgr.region,
                size=instance_type,
                image_id=ami,
            )
            ip = result.get("publicIpAddress")
            log(prefix, f"  IP: {ip}")
            stage_pass(6, "Create VM")
        except Exception as e:
            stage_fail(6, "Create VM", e)
            log(prefix, "Cannot continue without a running VM.")
            return 1

        # --- Stage 7: SSH connectivity ---
        try:
            from openadapt_evals.infrastructure.azure_vm import ssh_run, wait_for_ssh

            log(prefix, f"  Waiting for SSH on {ip}...")
            ssh_ok = wait_for_ssh(ip, timeout=180, username=mgr.ssh_username)
            if not ssh_ok:
                raise RuntimeError("SSH did not become reachable within 180s")

            result = ssh_run(ip, "hostname", username=mgr.ssh_username)
            hostname = result.stdout.strip()
            log(prefix, f"  Hostname: {hostname}")
            stage_pass(7, "SSH connectivity")
        except Exception as e:
            stage_fail(7, "SSH connectivity", e)

        # --- Stage 8: Stop / Start ---
        try:
            log(prefix, "  Stopping VM...")
            ok = mgr.deallocate_vm(vm_name)
            if not ok:
                raise RuntimeError("deallocate_vm returned False")
            log(prefix, "  VM stopped. Starting VM...")

            ok = mgr.start_vm(vm_name)
            if not ok:
                raise RuntimeError("start_vm returned False")

            new_ip = mgr.get_vm_ip(vm_name)
            log(prefix, f"  New IP after restart: {new_ip}")
            ip = new_ip  # update for cleanup
            stage_pass(8, "Stop/Start cycle")
        except Exception as e:
            stage_fail(8, "Stop/Start cycle", e)

    finally:
        # --- Stage 9: Cleanup ---
        try:
            log(prefix, f"  Deleting {vm_name}...")
            ok = mgr.delete_vm(vm_name)
            if not ok:
                raise RuntimeError("delete_vm returned False")
            log(prefix, "  VM terminated and cleaned up.")
            stage_pass(9, "Cleanup")
        except Exception as e:
            stage_fail(9, "Cleanup", e)
            log(prefix, f"  WARNING: {vm_name} may still exist — check AWS console!")

    # --- Final summary ---
    total = total_full
    log(prefix, "")
    log(prefix, "=" * 60)
    log(prefix, f"RESULT: {passed}/{total} stages passed, {failed} failed")
    log(prefix, "=" * 60)
    return 1 if failed > 0 else 0


def cmd_probe(args):
    """Check if WAA server is ready."""
    ip = get_vm_ip()
    if not ip:
        print("ERROR: VM not found")
        return 1

    timeout = args.timeout
    start = time.time()
    last_storage = None

    while True:
        # Check via SSH - must run curl INSIDE container to reach Docker network
        result = ssh_run(
            ip,
            "docker exec winarena curl -s --max-time 5 http://172.30.0.2:5000/probe 2>/dev/null || echo FAIL",
        )

        if "FAIL" not in result.stdout and result.stdout.strip():
            print("\nWAA server is READY")
            print(f"  Response: {result.stdout.strip()[:100]}")
            return 0

        if not args.wait:
            print("WAA server is NOT ready")
            return 1

        elapsed = time.time() - start
        if elapsed > timeout:
            print(f"\nTIMEOUT: WAA server not ready after {timeout}s")
            return 1

        # Get detailed status for progress display
        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)

        # Get storage in bytes for detailed view
        storage_result = ssh_run(ip, "docker exec winarena du -sb /storage/ 2>/dev/null | cut -f1")
        storage_bytes = storage_result.stdout.strip()
        if storage_bytes.isdigit():
            storage_mb = int(storage_bytes) / (1024 * 1024)
            storage_str = f"{storage_mb:,.1f} MB"
            # Show delta if we have previous value
            if last_storage is not None:
                delta = int(storage_bytes) - last_storage
                if delta > 0:
                    delta_mb = delta / (1024 * 1024)
                    storage_str += f" (+{delta_mb:,.1f} MB)"
            last_storage = int(storage_bytes)
        else:
            storage_str = "unknown"

        # Get QEMU uptime
        qemu_result = ssh_run(
            ip,
            'docker exec winarena sh -c \'QPID=$(pgrep -f qemu-system 2>/dev/null | head -1); [ -n "$QPID" ] && ps -o etime= -p $QPID 2>/dev/null | tr -d " " || echo N/A\'',
        )
        qemu_uptime = qemu_result.stdout.strip() or "N/A"

        # Get container uptime
        container_result = ssh_run(
            ip, "docker ps --filter name=winarena --format '{{.Status}}' 2>/dev/null"
        )
        container_status = container_result.stdout.strip() or "unknown"

        print(
            f"[{elapsed_min:02d}:{elapsed_sec:02d}] Waiting... | Storage: {storage_str} | QEMU: {qemu_uptime} | Container: {container_status}"
        )
        time.sleep(30)


def cmd_run(args):
    """Run benchmark tasks using vanilla WAA's navi agent.

    Note: For API-based agents (Claude, GPT-4 direct), use openadapt-evals
    which communicates with WAA's Flask API externally.
    """
    init_logging()

    ip = get_vm_ip()
    if not ip:
        log("RUN", "ERROR: VM not found")
        return 1

    # Check WAA is ready
    log("RUN", "Checking WAA server...")
    result = ssh_run(
        ip,
        "docker exec winarena curl -s --max-time 5 http://172.30.0.2:5000/probe 2>/dev/null || echo FAIL",
    )
    if "FAIL" in result.stdout or not result.stdout.strip():
        log("RUN", "ERROR: WAA server not ready. Run 'probe --wait' first.")
        return 1

    log("RUN", "WAA server is ready")

    # Get API key (navi uses GPT-4o for reasoning)
    api_key = args.api_key
    if not api_key:
        try:
            from openadapt_evals.config import settings

            api_key = settings.openai_api_key or ""
        except ImportError:
            api_key = ""

    if not api_key:
        log("RUN", "ERROR: OpenAI API key required (navi uses GPT-4o)")
        log("RUN", "  Set OPENAI_API_KEY in .env file or pass --api-key")
        return 1

    # Build task selection
    domain = args.domain
    task = args.task
    model = args.model

    task_info = []
    if task:
        task_info.append(f"task={task}")
    elif domain != "all":
        task_info.append(f"domain={domain}")
    elif args.num_tasks:
        task_info.append(f"{args.num_tasks} task(s)")
    else:
        task_info.append("all tasks")

    log("RUN", f"Starting benchmark: {', '.join(task_info)}, model={model}")

    # Build run.py arguments
    run_args = [
        "--agent_name navi",
        f"--model {model}",
        f"--domain {domain}",
    ]

    # Add parallelization flags if specified (argparse converts hyphens to underscores)
    worker_id = getattr(args, "worker_id", 0)
    num_workers = getattr(args, "num_workers", 1)
    if num_workers > 1:
        run_args.append(f"--worker_id {worker_id}")
        run_args.append(f"--num_workers {num_workers}")
        log("RUN", f"Parallel mode: worker {worker_id}/{num_workers}")

    # If specific task requested, create custom test config
    # test_all.json is a dict {domain: [task_ids...]} — run.py indexes by domain key,
    # so we must look up which domain contains this task and write that format
    if task:
        create_custom_test_cmd = f"""cat > /tmp/find_task.py << 'FINDEOF'
import json, sys
d = json.load(open("/client/evaluation_examples_windows/test_all.json"))
task_id = "{task}"
for domain, tasks in d.items():
    if task_id in tasks:
        json.dump({{domain: [task_id]}}, open("/client/evaluation_examples_windows/test_custom.json", "w"))
        print(f"Task {{task_id}} found in domain {{domain}}")
        sys.exit(0)
print(f"ERROR: Task {{task_id}} not found in test_all.json")
sys.exit(1)
FINDEOF
python3 /tmp/find_task.py && """
        run_args.append("--test_all_meta_path evaluation_examples_windows/test_custom.json")
        pre_cmd = create_custom_test_cmd
    elif args.num_tasks and args.num_tasks < 154:
        # Limit tasks by creating custom test config with first N tasks
        num = args.num_tasks
        # Write a temp Python script then run it (avoids quote escaping hell)
        # test_all.json is a dict {{domain: [task_ids...]}} - preserve domain structure
        create_limited_test_cmd = f"""cat > /tmp/limit_tasks.py << LIMITEOF
import json
d = json.load(open("/client/evaluation_examples_windows/test_all.json"))
# Collect (domain, task_id) pairs to preserve domain info
all_tasks = []
for domain, tasks in d.items():
    for task in tasks:
        all_tasks.append((domain, task))
# Limit total tasks
limited = all_tasks[:{num}]
# Rebuild dict preserving original domain structure
result = {{}}
for domain, task in limited:
    if domain not in result:
        result[domain] = []
    result[domain].append(task)
json.dump(result, open("/client/evaluation_examples_windows/test_limited.json", "w"))
print("Limited to", len(limited), "tasks from", len(result), "domains")
LIMITEOF
python /tmp/limit_tasks.py && """
        run_args.append("--test_all_meta_path evaluation_examples_windows/test_limited.json")
        pre_cmd = create_limited_test_cmd
    else:
        pre_cmd = ""

    # Run the benchmark inside the container
    run_cmd = (
        f'export OPENAI_API_KEY="{api_key}" && '
        f"docker exec -e OPENAI_API_KEY winarena "
        f"bash -c '{pre_cmd}cd /client && python run.py {' '.join(run_args)}'"
    )

    log("RUN", "Executing benchmark...")
    log("RUN", f"  Model: {model}")
    log("RUN", f"  Tasks: {task_info[0]}")
    log("RUN", "-" * 60)

    # Run with streaming output
    result = ssh_run(ip, run_cmd, stream=True, step="RUN")

    if result.returncode != 0:
        log("RUN", f"Benchmark failed with exit code {result.returncode}")
    else:
        log("RUN", "Benchmark completed!")

    # Download results unless --no-download
    if not args.no_download:
        log("RUN", "Downloading results...")
        download_benchmark_results(ip)

    return result.returncode


def download_benchmark_results(ip: str) -> str:
    """Download benchmark results from the container.

    Results are saved to benchmark_results/waa_results_TIMESTAMP/
    Returns the path to the results directory, or None if failed.
    """
    from pathlib import Path

    # Create local results directory with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = Path("benchmark_results") / f"waa_results_{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)

    log("RUN", f"Saving results to {results_dir}/")

    # Create tarball of results inside container
    log("RUN", "Creating results archive...")
    tar_cmd = "docker exec winarena tar -czvf /tmp/results.tar.gz -C /client/results . 2>/dev/null"
    result = subprocess.run(
        ["ssh", *SSH_OPTS, f"azureuser@{ip}", tar_cmd], capture_output=True, text=True
    )

    if result.returncode != 0:
        log(
            "RUN",
            f"Warning: Failed to create archive: {result.stderr[:200] if result.stderr else 'unknown'}",
        )
        log("RUN", "Trying direct copy...")

        # Try copying results directory directly
        copy_cmd = "docker cp winarena:/client/results/. /tmp/waa-results/"
        subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                f"azureuser@{ip}",
                f"rm -rf /tmp/waa-results && mkdir -p /tmp/waa-results && {copy_cmd}",
            ],
            capture_output=True,
        )

        # Download via scp
        scp_result = subprocess.run(
            [
                "scp",
                "-r",
                *SSH_OPTS,
                f"azureuser@{ip}:/tmp/waa-results/*",
                str(results_dir),
            ],
            capture_output=True,
            text=True,
        )
        if scp_result.returncode == 0:
            log("RUN", f"Results saved to: {results_dir}")
            return str(results_dir)
        else:
            log(
                "RUN",
                f"Warning: Failed to download results: {scp_result.stderr[:200] if scp_result.stderr else 'unknown'}",
            )
            return None

    # Copy tarball from container to VM host
    copy_tar_cmd = "docker cp winarena:/tmp/results.tar.gz /tmp/results.tar.gz"
    subprocess.run(["ssh", *SSH_OPTS, f"azureuser@{ip}", copy_tar_cmd], capture_output=True)

    # Download tarball
    local_tar = results_dir / "results.tar.gz"
    scp_result = subprocess.run(
        ["scp", *SSH_OPTS, f"azureuser@{ip}:/tmp/results.tar.gz", str(local_tar)],
        capture_output=True,
        text=True,
    )

    if scp_result.returncode != 0:
        log(
            "RUN",
            f"Warning: Failed to download tarball: {scp_result.stderr[:200] if scp_result.stderr else 'unknown'}",
        )
        return None

    # Extract tarball
    log("RUN", "Extracting results...")
    import tarfile

    try:
        with tarfile.open(local_tar, "r:gz") as tar:
            tar.extractall(path=results_dir)
        local_tar.unlink()  # Remove tarball after extraction
    except Exception as e:
        log("RUN", f"Warning: Failed to extract: {e}")
        log("RUN", f"Tarball saved at: {local_tar}")

    # Clean up remote tarball
    subprocess.run(
        ["ssh", *SSH_OPTS, f"azureuser@{ip}", "rm -f /tmp/results.tar.gz"],
        capture_output=True,
    )

    # List what we downloaded
    result_files = list(results_dir.glob("**/*"))
    log("RUN", f"Downloaded {len(result_files)} files to {results_dir}/")

    # Show summary if available
    summary_file = results_dir / "summary.json"
    if summary_file.exists():
        import json

        try:
            with open(summary_file) as f:
                summary = json.load(f)
            log("RUN", f"Summary: {json.dumps(summary, indent=2)[:500]}")
        except Exception:
            pass

    return str(results_dir)


def cmd_download(args):
    """Download benchmark results from VM."""
    init_logging()

    ip = get_vm_ip()
    if not ip:
        log("DOWNLOAD", "ERROR: VM not found")
        return 1

    log("DOWNLOAD", "Downloading benchmark results...")
    result_path = download_benchmark_results(ip)

    if result_path:
        log("DOWNLOAD", f"Results saved to: {result_path}")
        return 0
    else:
        log("DOWNLOAD", "Failed to download results")
        return 1


def cmd_analyze(args):
    """Analyze benchmark results from downloaded logs."""
    import re
    from collections import defaultdict

    results_dir = Path(args.results_dir) if args.results_dir else Path("benchmark_results")

    # Find most recent results if no specific dir given
    if args.results_dir:
        target_dir = Path(args.results_dir)
    else:
        dirs = sorted(results_dir.glob("waa_results_*"), reverse=True)
        if not dirs:
            print("No results found in benchmark_results/")
            print("Run 'cli download' first to get results from VM")
            return 1
        target_dir = dirs[0]

    print(f"Analyzing: {target_dir}")
    print("=" * 60)

    # Find log files
    log_files = list(target_dir.glob("logs/normal-*.log"))
    if not log_files:
        print("No log files found")
        return 1

    # Parse results
    tasks = []
    current_task = None
    pending_domain = None

    for log_file in sorted(log_files):
        with open(log_file) as f:
            for line in f:
                # Strip ANSI codes
                clean = re.sub(r"\x1b\[[0-9;]*m", "", line)

                # Domain comes before Example ID
                if "[Domain]:" in clean:
                    match = re.search(r"\[Domain\]: (.+)", clean)
                    if match:
                        pending_domain = match.group(1).strip()

                # Task start (Example ID comes after Domain)
                if "[Example ID]:" in clean:
                    match = re.search(r"\[Example ID\]: (.+)", clean)
                    if match:
                        current_task = {
                            "id": match.group(1).strip(),
                            "domain": pending_domain,
                            "reward": None,
                            "error": None,
                        }
                        pending_domain = None

                # Task result
                if "Reward:" in clean and current_task:
                    match = re.search(r"Reward: ([0-9.]+)", clean)
                    if match:
                        current_task["reward"] = float(match.group(1))
                        tasks.append(current_task)
                        current_task = None

                # Task error
                if "Exception in" in clean and current_task:
                    match = re.search(r"Exception in .+: (.+)", clean)
                    if match:
                        current_task["error"] = match.group(1).strip()
                        current_task["reward"] = 0.0
                        tasks.append(current_task)
                        current_task = None

    # Summary
    print(f"\nTotal tasks attempted: {len(tasks)}")

    if not tasks:
        print("No completed tasks found")
        return 0

    # Success rate
    successes = sum(1 for t in tasks if t["reward"] and t["reward"] > 0)
    print(f"Successful: {successes} ({100 * successes / len(tasks):.1f}%)")

    # By domain
    by_domain = defaultdict(list)
    for t in tasks:
        by_domain[t["domain"] or "unknown"].append(t)

    print("\nBy domain:")
    for domain in sorted(by_domain.keys()):
        domain_tasks = by_domain[domain]
        domain_success = sum(1 for t in domain_tasks if t["reward"] and t["reward"] > 0)
        print(
            f"  {domain}: {domain_success}/{len(domain_tasks)} ({100 * domain_success / len(domain_tasks):.1f}%)"
        )

    # Errors
    errors = [t for t in tasks if t.get("error")]
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for t in errors[:5]:  # Show first 5
            print(f"  {t['id']}: {t['error'][:50]}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more")

    return 0


def cmd_tasks(args):
    """List available WAA benchmark tasks."""
    ip = get_vm_ip()
    if not ip:
        print("ERROR: VM not found")
        return 1

    print("Fetching available tasks from WAA container...")
    print("-" * 60)

    # Get list of domains (subdirectories in examples/)
    result = subprocess.run(
        [
            "ssh",
            *SSH_OPTS,
            f"azureuser@{ip}",
            "docker exec winarena ls /client/evaluation_examples_windows/examples/",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("ERROR: Could not fetch domain list")
        return 1

    domains = result.stdout.strip().split("\n")

    # Count tasks per domain
    domain_tasks = {}
    total_tasks = 0

    for domain in domains:
        if not domain:
            continue
        count_result = subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                f"azureuser@{ip}",
                f"docker exec winarena ls /client/evaluation_examples_windows/examples/{domain}/ 2>/dev/null | wc -l",
            ],
            capture_output=True,
            text=True,
        )
        count = int(count_result.stdout.strip()) if count_result.stdout.strip().isdigit() else 0
        domain_tasks[domain] = count
        total_tasks += count

    # Print summary
    print(f"Total tasks: {total_tasks}")
    print(f"Domains: {len(domains)}")
    print()

    # Print by domain
    for domain in sorted(domain_tasks.keys()):
        count = domain_tasks[domain]
        print(f"  {domain}: {count} tasks")

        if args.verbose and count > 0:
            # List actual task IDs
            tasks_result = subprocess.run(
                [
                    "ssh",
                    *SSH_OPTS,
                    f"azureuser@{ip}",
                    f"docker exec winarena ls /client/evaluation_examples_windows/examples/{domain}/",
                ],
                capture_output=True,
                text=True,
            )
            for task_file in tasks_result.stdout.strip().split("\n")[:5]:  # Limit to 5
                task_id = task_file.replace(".json", "")
                print(f"    - {task_id}")
            if count > 5:
                print(f"    ... and {count - 5} more")

    print()
    print("Usage examples:")
    print("  Run all notepad tasks:  cli_v2 run --domain notepad")
    print("  Run all chrome tasks:   cli_v2 run --domain chrome")
    print("  Run specific task:      cli_v2 run --task 366de66e-cbae-4d72-b042-26390db2b145-WOS")

    return 0


def cmd_deallocate(args):
    """Stop VM (preserves disk, stops billing)."""
    init_logging()
    log("DEALLOCATE", f"Deallocating VM '{VM_NAME}'...")

    result = subprocess.run(
        ["az", "vm", "deallocate", "-g", RESOURCE_GROUP, "-n", VM_NAME],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        log("DEALLOCATE", "VM deallocated (billing stopped)")
        log("DEALLOCATE", "Use 'vm-start' to resume")
        return 0
    else:
        log("DEALLOCATE", f"ERROR: {result.stderr}")
        return 1


def cmd_vm_start(args):
    """Start a deallocated VM."""
    init_logging()
    log("VM-START", f"Starting VM '{VM_NAME}'...")

    result = subprocess.run(
        ["az", "vm", "start", "-g", RESOURCE_GROUP, "-n", VM_NAME],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        ip = get_vm_ip()
        log("VM-START", f"VM started: {ip}")
        log("VM-START", "Run 'build' then 'start' to launch WAA container")
        return 0
    else:
        log("VM-START", f"ERROR: {result.stderr}")
        return 1


def cmd_windows_restart(args):
    """Restart Windows inside QEMU via the monitor interface."""
    from openadapt_evals.infrastructure.qemu_reset import QEMUResetManager

    init_logging()

    ip = args.vm_ip or get_vm_ip()
    if not ip:
        log("WIN-RESTART", "ERROR: VM not found. Specify --vm-ip or ensure VM is running.")
        return 1

    log("WIN-RESTART", f"Restarting Windows on {ip} via QEMU monitor...")

    mgr = QEMUResetManager(
        vm_ip=ip,
        ssh_user="azureuser",
        timeout_seconds=args.timeout,
    )

    success, message = mgr.restart_windows(server_url=args.server)
    log("WIN-RESTART", message)
    return 0 if success else 1


def cmd_exec(args):
    """Run command on VM host."""
    ip = get_vm_ip()
    if not ip:
        print("ERROR: VM not found or not running")
        return 1

    cmd = args.cmd
    if not cmd:
        print("ERROR: --cmd is required")
        return 1

    result = ssh_run(ip, cmd, stream=True)
    return result.returncode


def cmd_docker_exec(args):
    """Run command inside winarena container."""
    ip = get_vm_ip()
    if not ip:
        print("ERROR: VM not found or not running")
        return 1

    cmd = args.cmd
    if not cmd:
        print("ERROR: --cmd is required")
        return 1

    docker_cmd = f"docker exec winarena {cmd}"
    result = ssh_run(ip, docker_cmd, stream=True)
    return result.returncode


def cmd_vnc(args):
    """Open VNC to view Windows desktop via SSH tunnel."""
    ip = get_vm_ip()
    if not ip:
        print("ERROR: VM not found or not running")
        return 1

    print(f"Setting up SSH tunnel to VM ({ip})...")
    print("VNC will be available at: http://localhost:8006")
    print("-" * 60)

    # Kill any existing tunnel on port 8006
    subprocess.run(["pkill", "-f", "ssh.*8006:localhost:8006"], capture_output=True)

    # Start SSH tunnel in background
    tunnel_proc = subprocess.Popen(
        ["ssh", *SSH_OPTS, "-N", "-L", "8006:localhost:8006", f"azureuser@{ip}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Give tunnel a moment to establish
    time.sleep(2)

    # Check if tunnel is running
    if tunnel_proc.poll() is not None:
        print("ERROR: SSH tunnel failed to start")
        return 1

    print(f"SSH tunnel established (PID: {tunnel_proc.pid})")

    # Open browser
    import webbrowser

    vnc_url = "http://localhost:8006"
    print(f"Opening {vnc_url} in browser...")
    webbrowser.open(vnc_url)

    print()
    print("VNC is now accessible at: http://localhost:8006")
    print("Press Ctrl+C to close the tunnel")
    print("-" * 60)

    try:
        # Keep tunnel alive
        tunnel_proc.wait()
    except KeyboardInterrupt:
        print("\nClosing SSH tunnel...")
        tunnel_proc.terminate()

    return 0


def _show_benchmark_progress(ip: str) -> int:
    """Show benchmark progress with estimated completion time.

    Parses the run log to count completed tasks and estimate remaining time.
    """
    # Find the most recent run log
    result = ssh_run(ip, "ls -t /home/azureuser/cli_logs/run_*.log 2>/dev/null | head -1")
    log_file = result.stdout.strip()

    if not log_file:
        print("No benchmark running. Start one with: run --num-tasks N")
        return 1

    # Get task count and timestamps
    result = ssh_run(
        ip,
        f"""
        echo "=== WAA Benchmark Progress ==="
        echo ""

        # Count completed tasks (each "Result:" line = 1 task done)
        COMPLETED=$(grep -c "Result:" {log_file} 2>/dev/null || echo 0)
        # Count total tasks from task list (sum of all domain counts)
        TOTAL=$(grep -A20 "Left tasks:" {log_file} | grep -E "^[a-z_]+: [0-9]+" | awk -F': ' '{{sum+=$2}} END {{print sum}}')
        [ -z "$TOTAL" ] || [ "$TOTAL" -eq 0 ] && TOTAL=154

        # Get timestamps
        FIRST_TS=$(grep -oE '\\[2026-[0-9-]+ [0-9:]+' {log_file} | head -1 | tr -d '[')
        LAST_TS=$(grep -oE '\\[2026-[0-9-]+ [0-9:]+' {log_file} | tail -1 | tr -d '[')

        echo "Log: {log_file}"
        echo "Started: $FIRST_TS"
        echo "Latest:  $LAST_TS"
        echo ""
        echo "Tasks completed: $COMPLETED / $TOTAL"

        # Calculate elapsed minutes
        if [ -n "$FIRST_TS" ] && [ -n "$LAST_TS" ]; then
            START_H=$(echo "$FIRST_TS" | awk '{{print $2}}' | cut -d: -f1)
            START_M=$(echo "$FIRST_TS" | awk '{{print $2}}' | cut -d: -f2)
            NOW_H=$(echo "$LAST_TS" | awk '{{print $2}}' | cut -d: -f1)
            NOW_M=$(echo "$LAST_TS" | awk '{{print $2}}' | cut -d: -f2)

            ELAPSED_MIN=$(( (NOW_H - START_H) * 60 + (NOW_M - START_M) ))
            echo "Elapsed: $ELAPSED_MIN minutes"

            if [ "$COMPLETED" -gt 0 ] && [ "$ELAPSED_MIN" -gt 0 ]; then
                MIN_PER_TASK=$((ELAPSED_MIN / COMPLETED))
                REMAINING=$((TOTAL - COMPLETED))
                EST_MIN=$((REMAINING * MIN_PER_TASK))
                EST_H=$((EST_MIN / 60))
                EST_M=$((EST_MIN % 60))

                echo ""
                echo "Avg time per task: ~$MIN_PER_TASK min"
                echo "Remaining tasks: $REMAINING"
                echo "Estimated remaining: ~${{EST_H}}h ${{EST_M}}m"

                # Progress bar
                PCT=$((COMPLETED * 100 / TOTAL))
                echo ""
                echo "Progress: $PCT% [$COMPLETED/$TOTAL]"
            fi
        fi
        """,
    )
    print(result.stdout)
    return 0


def _show_run_logs(ip: str, follow: bool = False, tail: Optional[int] = None) -> int:
    """Show the most recent run command log file.

    Args:
        ip: VM IP address
        follow: If True, use tail -f to stream the log
        tail: Number of lines to show (default: entire file or 100 for follow)

    Returns:
        Exit code (0 for success, 1 for error)
    """
    # Find the most recent run log file
    result = ssh_run(ip, "ls -t /home/azureuser/cli_logs/run_*.log 2>/dev/null | head -1")
    log_file = result.stdout.strip()

    if not log_file:
        print("No run logs found at /home/azureuser/cli_logs/run_*.log")
        print("Run a benchmark first: cli_v2 run --task <task_id>")
        return 1

    print(f"Run log: {log_file}")
    print("-" * 60)

    if follow:
        # Stream the log file
        print("Streaming log (Ctrl+C to stop)...")
        subprocess.run(["ssh", *SSH_OPTS, f"azureuser@{ip}", f"tail -f {log_file}"])
    else:
        # Show the log file contents
        if tail:
            cmd = f"tail -n {tail} {log_file}"
        else:
            # Check file size first - if small, cat it; if large, use tail
            size_result = ssh_run(ip, f"wc -l < {log_file}")
            line_count = (
                int(size_result.stdout.strip()) if size_result.stdout.strip().isdigit() else 0
            )

            if line_count <= 200:
                cmd = f"cat {log_file}"
            else:
                print(f"(Showing last 100 of {line_count} lines, use --tail N for more)")
                cmd = f"tail -n 100 {log_file}"

        subprocess.run(["ssh", *SSH_OPTS, f"azureuser@{ip}", cmd])

    return 0


def cmd_logs(args):
    """Show comprehensive logs from the WAA container.

    Default behavior shows all relevant logs (docker, storage, probe status).
    Use --follow to stream docker logs continuously.
    Use --run to show run command output instead of container logs.
    Use --progress to show benchmark progress and ETA.
    """
    ip = get_vm_ip()
    if not ip:
        print("ERROR: VM not found")
        return 1

    # Handle --progress flag: show benchmark progress
    if getattr(args, "progress", False):
        return _show_benchmark_progress(ip)

    # Handle --run flag: show run command output
    if args.run:
        return _show_run_logs(ip, args.follow, args.tail)

    # Check if container exists
    result = ssh_run(ip, "docker ps -a --filter name=winarena --format '{{.Status}}'")
    container_status = result.stdout.strip()
    container_exists = bool(container_status)

    # If --follow, stream the most relevant logs
    if args.follow:
        # Priority 1: If container is running, stream container logs
        if container_exists and "Up" in container_status:
            print(f"Streaming container logs from VM ({ip}):")
            print("Press Ctrl+C to stop")
            print("-" * 60)
            subprocess.run(["ssh", *SSH_OPTS, f"azureuser@{ip}", "docker logs -f winarena 2>&1"])
            return 0

        # Priority 2: Check for active docker build
        result = ssh_run(
            ip,
            "pgrep -f 'docker build' >/dev/null && echo BUILD_RUNNING || echo NO_BUILD",
        )
        if "BUILD_RUNNING" in result.stdout:
            print(f"Docker build in progress on VM ({ip})")
            print("Streaming build logs (Ctrl+C to stop):")
            print("-" * 60)
            # Find and tail the most recent build log
            subprocess.run(
                [
                    "ssh",
                    *SSH_OPTS,
                    f"azureuser@{ip}",
                    "tail -f $(ls -t ~/cli_logs/build_*.log 2>/dev/null | head -1) 2>/dev/null || "
                    "tail -f ~/build.log 2>/dev/null || "
                    "echo 'No build logs found - build may have just started'",
                ]
            )
            return 0

        # Priority 3: No container, no build - show helpful message
        print(f"Container 'winarena' not running on VM ({ip})")
        print()
        # Check if image exists
        result = ssh_run(ip, "docker images waa-auto:latest --format '{{.Repository}}:{{.Tag}}'")
        if result.stdout.strip():
            print("Image 'waa-auto:latest' is ready.")
            print("Run: uv run python -m openadapt_evals.benchmarks.vm_cli start")
        else:
            print("Image not yet built.")
            print("Run: uv run python -m openadapt_evals.benchmarks.vm_cli build")
        return 1

    # Default: show comprehensive status
    import sys

    print(f"WAA Status ({ip})")
    print("=" * 60)
    sys.stdout.flush()

    # Docker images
    print("\n[Docker Images]", flush=True)
    subprocess.run(
        [
            "ssh",
            *SSH_OPTS,
            f"azureuser@{ip}",
            "docker images --format 'table {{.Repository}}\\t{{.Tag}}\\t{{.Size}}' 2>/dev/null | head -5",
        ]
    )

    # Container status
    print("\n[Container]", flush=True)
    if container_exists:
        print(f"  Status: {container_status}", flush=True)
    else:
        print("  Container 'winarena' not created yet", flush=True)
        # Check for active build
        result = ssh_run(
            ip,
            "pgrep -f 'docker build' >/dev/null && echo BUILD_RUNNING || echo NO_BUILD",
        )
        if "BUILD_RUNNING" in result.stdout:
            print("  Docker build in progress...", flush=True)

    # Only show these sections if container exists
    if container_exists and "Up" in container_status:
        # Storage info
        print("\n[Storage]", flush=True)
        subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                f"azureuser@{ip}",
                "docker exec winarena sh -c '"
                'echo "  Total: $(du -sh /storage/ 2>/dev/null | cut -f1)"; '
                'ls -lh /storage/*.img 2>/dev/null | awk "{print \\"  Disk image: \\" \\$5}" || true'
                "'",
            ]
        )

        # QEMU VM status
        print("\n[QEMU VM]", flush=True)
        subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                f"azureuser@{ip}",
                "docker exec winarena sh -c '"
                "QPID=$(pgrep -f qemu-system 2>/dev/null | head -1); "
                'if [ -n "$QPID" ]; then '
                '  echo "  Status: Running (PID $QPID)"; '
                '  ps -o %cpu,%mem,etime -p $QPID 2>/dev/null | tail -1 | awk "{print \\"  CPU: \\" \\$1 \\"%, MEM: \\" \\$2 \\"%, Uptime: \\" \\$3}"; '
                "else "
                '  echo "  Status: Not running"; '
                "fi"
                "'",
            ]
        )

        # WAA server probe
        print("\n[WAA Server]", flush=True)
        subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                f"azureuser@{ip}",
                "docker exec winarena curl -s --max-time 5 http://172.30.0.2:5000/probe 2>/dev/null && echo ' (READY)' || echo 'Not ready (Windows installing - check VNC for progress)'",
            ]
        )

        # Windows install log (written by install.bat to Samba share at Z:\install_log.txt)
        # The Samba share \\host.lan\Data maps to /tmp/smb inside the container
        result = ssh_run(
            ip, "docker exec winarena cat /tmp/smb/install_log.txt 2>/dev/null | wc -l"
        )
        install_log_lines = result.stdout.strip()
        if install_log_lines and install_log_lines != "0":
            print("\n[Windows Install Log]", flush=True)
            # Show last 10 lines of the install log (shows current step like [5/14] Installing Git...)
            subprocess.run(
                [
                    "ssh",
                    *SSH_OPTS,
                    f"azureuser@{ip}",
                    "docker exec winarena tail -10 /tmp/smb/install_log.txt 2>/dev/null",
                ]
            )

        # Recent docker logs
        tail_lines = args.tail if args.tail else 20
        print(f"\n[Recent Logs (last {tail_lines} lines)]", flush=True)
        print("-" * 60, flush=True)
        subprocess.run(
            [
                "ssh",
                *SSH_OPTS,
                f"azureuser@{ip}",
                f"docker logs --tail {tail_lines} winarena 2>&1",
            ]
        )

        print("\n" + "=" * 60, flush=True)
        print("VNC: ssh -L 8006:localhost:8006 azureuser@" + ip, flush=True)
        print("     Then open http://localhost:8006", flush=True)
        print("     (Windows installation % visible on VNC screen)", flush=True)
    else:
        # Show next steps
        print("\n[Next Steps]")
        result = ssh_run(ip, "docker images waa-auto:latest --format '{{.Repository}}'")
        if result.stdout.strip():
            print("  Image ready. Run: cli_v2 start")
        else:
            print("  Build image first. Run: cli_v2 build")

    return 0


# Minimal startup script for Azure ML compute instances.
# Previously lived at vendor/WindowsAgentArena/scripts/azure_files/compute-instance-startup.sh
COMPUTE_INSTANCE_STARTUP_SH = """\
#!/bin/bash

# Minimal startup script - completes quickly to avoid Azure ML timeout
# The actual work (Docker pull, Windows boot) happens in the job itself

echo "$(date): Compute instance startup script - minimal version"

# Just exit successfully - the job will handle Docker setup
exit 0
"""


def upload_startup_script_to_datastore(script_content: str, file_path: str) -> bool:
    """Upload startup script to Azure ML workspace code file share.

    Azure ML mounts the 'code-*' file share at:
    /mnt/batch/tasks/shared/LS_root/mounts/clusters/<instance>/code/

    Args:
        script_content: Content of the startup script
        file_path: Destination path in file share (e.g., 'Users/openadapt/compute-instance-startup.sh')

    Returns:
        True if successful, False otherwise
    """
    from openadapt_evals.config import settings

    # Get storage account from workspace
    workspace = settings.azure_ml_workspace_name
    resource_group = settings.azure_ml_resource_group

    # Get storage account name from workspace
    log("AZURE-ML", f"Getting storage account for workspace {workspace}...")
    result = subprocess.run(
        [
            "az",
            "ml",
            "workspace",
            "show",
            "--name",
            workspace,
            "--resource-group",
            resource_group,
            "--query",
            "storage_account",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"ERROR: Failed to get storage account: {result.stderr}")
        return False

    # Extract storage account name from resource ID
    storage_account_id = result.stdout.strip()
    storage_account = storage_account_id.split("/")[-1]
    log("AZURE-ML", f"Storage account: {storage_account}")

    # Get storage account key
    result = subprocess.run(
        [
            "az",
            "storage",
            "account",
            "keys",
            "list",
            "--account-name",
            storage_account,
            "--query",
            "[0].value",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"ERROR: Failed to get storage key: {result.stderr}")
        return False

    storage_key = result.stdout.strip()

    # List file shares and find the 'code-*' one
    # Azure ML uses the code file share for startup scripts
    result = subprocess.run(
        [
            "az",
            "storage",
            "share",
            "list",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--query",
            "[].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"ERROR: Failed to list file shares: {result.stderr}")
        return False

    shares = result.stdout.strip().split("\n")
    code_share = None
    for s in shares:
        if s.startswith("code-"):
            code_share = s
            break

    if not code_share:
        log("AZURE-ML", f"ERROR: Could not find code file share. Available: {shares}")
        return False

    log("AZURE-ML", f"Using file share: {code_share}")

    # Create directory structure (Users/openadapt/)
    dir_path = "/".join(file_path.split("/")[:-1])  # e.g., "Users/openadapt"
    if dir_path:
        log("AZURE-ML", f"Creating directory: {dir_path}")
        # Create nested directories
        parts = dir_path.split("/")
        current_path = ""
        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            subprocess.run(
                [
                    "az",
                    "storage",
                    "directory",
                    "create",
                    "--account-name",
                    storage_account,
                    "--account-key",
                    storage_key,
                    "--share-name",
                    code_share,
                    "--name",
                    current_path,
                ],
                capture_output=True,
                text=True,
            )
            # Ignore errors - directory may already exist

    # Write content to a temp file for az storage upload
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tmp:
        tmp.write(script_content)
        tmp_path = tmp.name

    try:
        log("AZURE-ML", f"Uploading startup script to {file_path}...")
        result = subprocess.run(
            [
                "az",
                "storage",
                "file",
                "upload",
                "--account-name",
                storage_account,
                "--account-key",
                storage_key,
                "--share-name",
                code_share,
                "--source",
                tmp_path,
                "--path",
                file_path,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(tmp_path)

    if result.returncode != 0:
        log("AZURE-ML", f"ERROR: Failed to upload: {result.stderr}")
        return False

    log(
        "AZURE-ML",
        f"SUCCESS: Uploaded startup script to file share {code_share}/{file_path}",
    )
    return True


def get_azure_ml_storage_info() -> tuple[str, str, str]:
    """Get Azure ML workspace storage account info.

    Returns:
        Tuple of (storage_account, storage_key, blob_container)
    """
    from openadapt_evals.config import settings

    workspace = settings.azure_ml_workspace_name
    resource_group = settings.azure_ml_resource_group

    # Get storage account name from workspace
    log("AZURE-ML", f"Getting storage account for workspace {workspace}...")
    result = subprocess.run(
        [
            "az",
            "ml",
            "workspace",
            "show",
            "--name",
            workspace,
            "--resource-group",
            resource_group,
            "--query",
            "storage_account",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to get storage account: {result.stderr}")

    storage_account_id = result.stdout.strip()
    storage_account = storage_account_id.split("/")[-1]
    log("AZURE-ML", f"Storage account: {storage_account}")

    # Get storage account key
    result = subprocess.run(
        [
            "az",
            "storage",
            "account",
            "keys",
            "list",
            "--account-name",
            storage_account,
            "--query",
            "[0].value",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to get storage key: {result.stderr}")

    storage_key = result.stdout.strip()

    # List blob containers and find the 'azureml-blobstore-*' one
    result = subprocess.run(
        [
            "az",
            "storage",
            "container",
            "list",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--query",
            "[].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to list containers: {result.stderr}")

    containers = result.stdout.strip().split("\n")
    blob_container = None
    for c in containers:
        if c.startswith("azureml-blobstore-"):
            blob_container = c
            break

    if not blob_container:
        raise RuntimeError(f"Could not find azureml-blobstore container. Available: {containers}")

    log("AZURE-ML", f"Blob container: {blob_container}")

    return storage_account, storage_key, blob_container


def check_golden_image_in_blob(storage_account: str, storage_key: str, blob_container: str) -> dict:
    """Check if golden image files exist in blob storage.

    Returns:
        Dict with 'exists' bool, 'files' list, and 'total_size' in bytes
    """
    log("AZURE-ML", "Checking for golden image in blob storage...")

    result = subprocess.run(
        [
            "az",
            "storage",
            "blob",
            "list",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--container-name",
            blob_container,
            "--prefix",
            "storage/",
            "--query",
            "[].{name:name, size:properties.contentLength}",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"ERROR: Failed to list blobs: {result.stderr}")
        return {"exists": False, "files": [], "total_size": 0}

    try:
        files = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        files = []

    total_size = sum(f.get("size", 0) or 0 for f in files)

    # Check for required files (only data.img is truly required, OVMF files come from Docker image)
    # windows.vars contains UEFI variables for the specific Windows install
    required_files = ["data.img"]
    found_files = [f["name"].replace("storage/", "") for f in files]
    has_required = all(rf in found_files for rf in required_files)

    return {
        "exists": has_required,
        "files": files,
        "total_size": total_size,
        "found_files": found_files,
    }


def upload_golden_image_to_blob(source_path: str) -> bool:
    """Upload golden image files to Azure blob storage.

    Args:
        source_path: Local path to storage directory containing data.img, OVMF_*, etc.

    Returns:
        True if successful, False otherwise
    """
    source_dir = Path(source_path)

    if not source_dir.exists():
        log("AZURE-ML", f"ERROR: Source directory not found: {source_dir}")
        log("AZURE-ML", "")
        log("AZURE-ML", "You need to prepare the golden image first.")
        log(
            "AZURE-ML",
            "See https://github.com/microsoft/WindowsAgentArena for setup instructions.",
        )
        return False

    # Check for required files (only data.img is truly required, OVMF files come from Docker image)
    required_files = ["data.img"]
    missing = [f for f in required_files if not (source_dir / f).exists()]
    if missing:
        log("AZURE-ML", f"ERROR: Missing required files: {missing}")
        log("AZURE-ML", f"Available files: {list(source_dir.iterdir())}")
        return False

    # Calculate total size
    total_size = sum(f.stat().st_size for f in source_dir.iterdir() if f.is_file())
    log("AZURE-ML", f"Source directory: {source_dir}")
    log("AZURE-ML", f"Total size to upload: {total_size / (1024**3):.2f} GB")

    try:
        storage_account, storage_key, blob_container = get_azure_ml_storage_info()
    except RuntimeError as e:
        log("AZURE-ML", f"ERROR: {e}")
        return False

    # Check if azcopy is available (faster for large files)
    azcopy_available = subprocess.run(["which", "azcopy"], capture_output=True).returncode == 0

    if azcopy_available:
        log("AZURE-ML", "Using azcopy for faster upload...")
        # Generate SAS token for azcopy
        result = subprocess.run(
            [
                "az",
                "storage",
                "container",
                "generate-sas",
                "--account-name",
                storage_account,
                "--account-key",
                storage_key,
                "--name",
                blob_container,
                "--permissions",
                "racwdl",
                "--expiry",
                (datetime.now().replace(hour=23, minute=59, second=59)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            sas_token = result.stdout.strip()
            dest_url = f"https://{storage_account}.blob.core.windows.net/{blob_container}/storage?{sas_token}"

            log("AZURE-ML", "Uploading with azcopy (progress will be shown)...")
            result = subprocess.run(
                [
                    "azcopy",
                    "copy",
                    str(source_dir) + "/*",
                    dest_url,
                    "--recursive=false",
                    "--overwrite=true",
                ],
                text=True,
            )

            if result.returncode == 0:
                log("AZURE-ML", "SUCCESS: Upload completed with azcopy")
                return True
            else:
                log(
                    "AZURE-ML",
                    "azcopy failed, falling back to az storage blob upload-batch...",
                )

    # Fallback to az storage blob upload-batch
    log("AZURE-ML", "Uploading with az storage blob upload-batch...")
    log("AZURE-ML", f"Destination: {blob_container}/storage/")
    log("AZURE-ML", "This may take 10-30 minutes for 30GB...")

    result = subprocess.run(
        [
            "az",
            "storage",
            "blob",
            "upload-batch",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--destination",
            blob_container,
            "--destination-path",
            "storage",
            "--source",
            str(source_dir),
            "--overwrite",
            "--progress",
        ],
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", "ERROR: Upload failed")
        return False

    # Verify upload
    log("AZURE-ML", "Verifying upload...")
    info = check_golden_image_in_blob(storage_account, storage_key, blob_container)

    if info["exists"]:
        log(
            "AZURE-ML",
            f"SUCCESS: Golden image uploaded ({info['total_size'] / (1024**3):.2f} GB)",
        )
        log("AZURE-ML", f"Files: {info['found_files']}")
        return True
    else:
        log("AZURE-ML", "ERROR: Upload verification failed")
        return False


def upload_placeholder_to_blob() -> bool:
    """Upload a minimal placeholder to blob storage for VERSION=11e approach.

    This creates an empty placeholder that satisfies Azure ML's mount requirement
    while letting the container auto-download Windows via VERSION=11e.

    Returns:
        True if successful, False otherwise
    """
    try:
        storage_account, storage_key, blob_container = get_azure_ml_storage_info()
    except RuntimeError as e:
        log("AZURE-ML", f"ERROR: {e}")
        return False

    # Create a placeholder file
    placeholder_content = "# Placeholder for VERSION=11e auto-download\n# Windows will be downloaded automatically on first run\n"

    log("AZURE-ML", "Creating placeholder for VERSION=11e approach...")

    # Upload placeholder
    result = subprocess.run(
        [
            "az",
            "storage",
            "blob",
            "upload",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--container-name",
            blob_container,
            "--name",
            "storage/README.txt",
            "--data",
            placeholder_content,
            "--overwrite",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"ERROR: Failed to upload placeholder: {result.stderr}")
        return False

    log("AZURE-ML", "SUCCESS: Placeholder uploaded to storage/README.txt")
    log("AZURE-ML", "")
    log(
        "AZURE-ML",
        "The container will auto-download Windows 11 Enterprise on first run.",
    )
    log("AZURE-ML", "This adds ~15-20 minutes to the first startup time.")
    return True


def upload_golden_image_from_vm() -> bool:
    """Upload golden image from Azure VM to blob storage.

    For macOS users who can't prepare the golden image locally (no KVM).
    Uses the existing Azure VM to extract storage files and upload to blob.

    Process:
    1. Check if golden image exists on VM at WAA_STORAGE_DIR
    2. Get Azure storage credentials
    3. Run az storage blob upload-batch on VM

    Returns:
        True if successful, False otherwise
    """
    ip = get_vm_ip()
    if not ip:
        log("AZURE-ML", "ERROR: Azure VM not running. Start it first:")
        log("AZURE-ML", "  uv run python -m openadapt_evals.benchmarks.vm_cli vm start")
        return False

    log("AZURE-ML", f"Connected to Azure VM at {ip}")

    # Check if golden image exists on VM
    log("AZURE-ML", "Checking for golden image on VM...")
    result = ssh_run(ip, f"ls -la {WAA_STORAGE_DIR}/data.img 2>/dev/null || echo 'NOT_FOUND'")
    if "NOT_FOUND" in result.stdout or result.returncode != 0:
        log("AZURE-ML", f"ERROR: Golden image not found on VM at {WAA_STORAGE_DIR}/")
        log("AZURE-ML", "")
        log("AZURE-ML", "To create the golden image:")
        log("AZURE-ML", "  1. Start Windows container with VERSION=11e")
        log("AZURE-ML", "  2. Wait for Windows to fully install (~15-20 min)")
        log("AZURE-ML", "  3. Stop the container gracefully")
        log("AZURE-ML", f"  4. The storage files will be at {WAA_STORAGE_DIR}/")
        return False

    # Get file sizes
    result = ssh_run(ip, f"du -sh {WAA_STORAGE_DIR}/")
    if result.returncode == 0:
        log("AZURE-ML", f"Golden image size: {result.stdout.strip().split()[0]}")

    # Get Azure storage credentials
    try:
        storage_account, storage_key, blob_container = get_azure_ml_storage_info()
    except RuntimeError as e:
        log("AZURE-ML", f"ERROR: {e}")
        return False

    log("AZURE-ML", f"Uploading to: {storage_account}/{blob_container}/storage/")
    log("AZURE-ML", "This may take several minutes for the 25GB image...")

    # Upload using az CLI on the VM (faster than downloading locally first)
    upload_cmd = f"""
az storage blob upload-batch \\
    --account-name {storage_account} \\
    --account-key '{storage_key}' \\
    --destination {blob_container} \\
    --source {WAA_STORAGE_DIR} \\
    --destination-path storage \\
    --overwrite
"""
    result = ssh_run(ip, upload_cmd, stream=True, step="UPLOAD")

    if result.returncode != 0:
        log("AZURE-ML", "ERROR: Upload failed")
        log("AZURE-ML", "Try running manually on the VM:")
        log("AZURE-ML", f"  ssh azureuser@{ip}")
        log("AZURE-ML", "  az login")
        log("AZURE-ML", "  <upload command>")
        return False

    # Verify upload
    log("AZURE-ML", "Verifying upload...")
    info = check_golden_image_in_blob(storage_account, storage_key, blob_container)
    if info["exists"]:
        log("AZURE-ML", "SUCCESS: Golden image uploaded to Azure blob storage")
        log("AZURE-ML", f"  Total size: {info['total_size'] / (1024**3):.2f} GB")
        log("AZURE-ML", f"  Files: {info['found_files']}")
        log("AZURE-ML", "")
        log("AZURE-ML", "Ready to run benchmark:")
        log(
            "AZURE-ML",
            "  uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --workers 1",
        )
        return True
    else:
        log("AZURE-ML", "ERROR: Upload verification failed")
        return False


# VM pricing table for cost estimation
AZURE_VM_HOURLY_RATES = {
    "Standard_D2_v3": 0.096,
    "Standard_D4_v3": 0.192,
    "Standard_D8_v3": 0.384,
    "Standard_D4s_v3": 0.192,
    "Standard_D8s_v3": 0.384,
    "Standard_D4ds_v5": 0.422,
    "Standard_D8ds_v5": 0.384,
    "Standard_D16ds_v5": 0.768,
    "Standard_D32ds_v5": 1.536,
    "STANDARD_D4_V3": 0.192,  # Azure sometimes returns uppercase
    "STANDARD_D8_V3": 0.384,
}

# Blob storage pricing: ~$0.018/GB/month for hot tier
BLOB_STORAGE_COST_PER_GB_MONTH = 0.018


def list_azure_ml_compute_instances() -> list[dict]:
    """List all Azure ML compute instances.

    Returns:
        List of dicts with compute instance info
    """
    from openadapt_evals.config import settings

    workspace = settings.azure_ml_workspace_name
    resource_group = settings.azure_ml_resource_group

    result = subprocess.run(
        [
            "az",
            "ml",
            "compute",
            "list",
            "--workspace-name",
            workspace,
            "--resource-group",
            resource_group,
            "--query",
            "[?type=='computeinstance'].{name:name, state:state, vmSize:size}",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"Warning: Failed to list compute instances: {result.stderr}")
        return []

    try:
        instances = json.loads(result.stdout) if result.stdout.strip() else []
        return instances
    except json.JSONDecodeError:
        return []


def list_azure_ml_blob_files(
    storage_account: str,
    storage_key: str,
    blob_container: str,
    prefix: str = "storage/",
) -> list[dict]:
    """List files in Azure blob storage with given prefix.

    Returns:
        List of dicts with file info (name, size)
    """
    result = subprocess.run(
        [
            "az",
            "storage",
            "blob",
            "list",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--container-name",
            blob_container,
            "--prefix",
            prefix,
            "--query",
            "[].{name:name, size:properties.contentLength}",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"Warning: Failed to list blobs: {result.stderr}")
        return []

    try:
        files = json.loads(result.stdout) if result.stdout.strip() else []
        return files
    except json.JSONDecodeError:
        return []


def list_azure_ml_file_share_files(
    storage_account: str, storage_key: str, code_share: str, prefix: str = "Users/"
) -> list[dict]:
    """List files in Azure file share with given prefix.

    Returns:
        List of dicts with file info
    """
    result = subprocess.run(
        [
            "az",
            "storage",
            "file",
            "list",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--share-name",
            code_share,
            "--path",
            prefix,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # Try without prefix (might not exist)
        return []

    try:
        files = json.loads(result.stdout) if result.stdout.strip() else []
        return files
    except json.JSONDecodeError:
        return []


def get_azure_ml_file_share_name() -> str | None:
    """Get the code file share name from Azure ML workspace.

    Returns:
        File share name or None if not found
    """
    from openadapt_evals.config import settings

    workspace = settings.azure_ml_workspace_name
    resource_group = settings.azure_ml_resource_group

    # Get storage account
    result = subprocess.run(
        [
            "az",
            "ml",
            "workspace",
            "show",
            "--name",
            workspace,
            "--resource-group",
            resource_group,
            "--query",
            "storage_account",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    storage_account = result.stdout.strip().split("/")[-1]

    # Get storage key
    result = subprocess.run(
        [
            "az",
            "storage",
            "account",
            "keys",
            "list",
            "--account-name",
            storage_account,
            "--query",
            "[0].value",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    storage_key = result.stdout.strip()

    # List file shares
    result = subprocess.run(
        [
            "az",
            "storage",
            "share",
            "list",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--query",
            "[].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    shares = result.stdout.strip().split("\n")
    for s in shares:
        if s.startswith("code-"):
            return s

    return None


def delete_azure_ml_compute_instance(name: str) -> bool:
    """Delete an Azure ML compute instance.

    Args:
        name: Compute instance name

    Returns:
        True if deleted successfully
    """
    from openadapt_evals.config import settings

    workspace = settings.azure_ml_workspace_name
    resource_group = settings.azure_ml_resource_group

    log("AZURE-ML", f"Deleting compute instance: {name}...")

    result = subprocess.run(
        [
            "az",
            "ml",
            "compute",
            "delete",
            "--name",
            name,
            "--workspace-name",
            workspace,
            "--resource-group",
            resource_group,
            "--yes",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("AZURE-ML", f"Warning: Failed to delete {name}: {result.stderr}")
        return False

    log("AZURE-ML", f"Deleted compute instance: {name}")
    return True


def delete_azure_ml_blob_files(
    storage_account: str, storage_key: str, blob_container: str, prefix: str
) -> int:
    """Delete all blob files with given prefix.

    Args:
        storage_account: Storage account name
        storage_key: Storage account key
        blob_container: Blob container name
        prefix: Blob prefix to delete (e.g., "storage/")

    Returns:
        Number of files deleted
    """
    # First list all files
    files = list_azure_ml_blob_files(storage_account, storage_key, blob_container, prefix)

    deleted = 0
    for f in files:
        name = f.get("name", "")
        if not name:
            continue

        log("AZURE-ML", f"Deleting blob: {name}...")
        result = subprocess.run(
            [
                "az",
                "storage",
                "blob",
                "delete",
                "--account-name",
                storage_account,
                "--account-key",
                storage_key,
                "--container-name",
                blob_container,
                "--name",
                name,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            deleted += 1
        else:
            log("AZURE-ML", f"Warning: Failed to delete {name}: {result.stderr}")

    return deleted


def delete_azure_ml_file_share_files(
    storage_account: str, storage_key: str, code_share: str, path: str
) -> int:
    """Delete files in file share at given path.

    Args:
        storage_account: Storage account name
        storage_key: Storage account key
        code_share: File share name
        path: Path to delete (e.g., "Users/openadapt/compute-instance-startup.sh")

    Returns:
        Number of files deleted
    """
    log("AZURE-ML", f"Deleting file: {path}...")

    result = subprocess.run(
        [
            "az",
            "storage",
            "file",
            "delete",
            "--account-name",
            storage_account,
            "--account-key",
            storage_key,
            "--share-name",
            code_share,
            "--path",
            path,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        return 1
    else:
        log("AZURE-ML", f"Warning: Failed to delete {path}: {result.stderr}")
        return 0


def show_azure_ml_cost_summary() -> dict:
    """Show Azure ML cost summary.

    Returns:
        Dict with cost information
    """
    log("AZURE-ML", "=== Azure ML Cost Summary ===")
    log("AZURE-ML", "")

    result_summary = {
        "compute_instances": [],
        "compute_total_hours": 0.0,
        "compute_total_cost": 0.0,
        "blob_files": [],
        "blob_total_gb": 0.0,
        "blob_monthly_cost": 0.0,
    }

    # 1. List compute instances
    log("AZURE-ML", "Compute Instances:")
    instances = list_azure_ml_compute_instances()

    if not instances:
        log("AZURE-ML", "  (none found)")
    else:
        for inst in instances:
            name = inst.get("name", "unknown")
            state = inst.get("state", "unknown")
            vm_size = inst.get("vmSize", "unknown")
            hourly_rate = AZURE_VM_HOURLY_RATES.get(vm_size, 0.192)

            # Note: We don't have actual runtime, so we show hourly rate
            # Users should check Azure portal for actual usage
            log("AZURE-ML", f"  {name}  {vm_size}  {state}  ${hourly_rate:.2f}/hr")

            result_summary["compute_instances"].append(
                {
                    "name": name,
                    "state": state,
                    "vmSize": vm_size,
                    "hourly_rate": hourly_rate,
                }
            )

    log("AZURE-ML", "")

    # 2. List blob storage
    log("AZURE-ML", "Blob Storage:")
    try:
        storage_account, storage_key, blob_container = get_azure_ml_storage_info()
        files = list_azure_ml_blob_files(storage_account, storage_key, blob_container, "storage/")

        if not files:
            log("AZURE-ML", "  (no files in storage/ prefix)")
        else:
            total_bytes = 0
            for f in files:
                name = f.get("name", "").replace("storage/", "")
                size = f.get("size", 0) or 0
                total_bytes += size

                if size >= 1024**3:
                    size_str = f"{size / (1024**3):.1f} GB"
                elif size >= 1024**2:
                    size_str = f"{size / (1024**2):.1f} MB"
                else:
                    size_str = f"{size / 1024:.1f} KB"

                log("AZURE-ML", f"  {name}  {size_str}")
                result_summary["blob_files"].append({"name": name, "size": size})

            total_gb = total_bytes / (1024**3)
            monthly_cost = total_gb * BLOB_STORAGE_COST_PER_GB_MONTH
            result_summary["blob_total_gb"] = total_gb
            result_summary["blob_monthly_cost"] = monthly_cost

            log("AZURE-ML", "")
            log("AZURE-ML", f"  Total: {total_gb:.2f} GB, ~${monthly_cost:.2f}/month")
    except RuntimeError as e:
        log("AZURE-ML", f"  (error: {e})")

    log("AZURE-ML", "")

    # 3. Summary
    log("AZURE-ML", "Cost Notes:")
    log("AZURE-ML", "  - Compute instances bill per hour while running")
    log("AZURE-ML", "  - Use --teardown --confirm to delete all resources")
    log("AZURE-ML", "  - Check Azure portal for actual usage and costs")

    return result_summary


def show_azure_ml_resources() -> dict:
    """List all Azure ML resources.

    Returns:
        Dict with resource information
    """
    log("AZURE-ML", "=== Azure ML Resources ===")
    log("AZURE-ML", "")

    result = {
        "compute_instances": [],
        "blob_files": [],
        "file_share_files": [],
    }

    # 1. Compute instances
    log("AZURE-ML", "Compute Instances:")
    instances = list_azure_ml_compute_instances()

    if not instances:
        log("AZURE-ML", "  (none)")
    else:
        for inst in instances:
            name = inst.get("name", "unknown")
            state = inst.get("state", "unknown")
            vm_size = inst.get("vmSize", "unknown")
            log("AZURE-ML", f"  - {name}  {vm_size}  {state}")
            result["compute_instances"].append(inst)

    log("AZURE-ML", "")

    # 2. Blob storage
    log("AZURE-ML", "Blob Storage (storage/ prefix):")
    try:
        storage_account, storage_key, blob_container = get_azure_ml_storage_info()
        files = list_azure_ml_blob_files(storage_account, storage_key, blob_container, "storage/")

        if not files:
            log("AZURE-ML", "  (empty)")
        else:
            for f in files:
                name = f.get("name", "").replace("storage/", "")
                size = f.get("size", 0) or 0
                if size >= 1024**3:
                    size_str = f"({size / (1024**3):.1f} GB)"
                elif size >= 1024**2:
                    size_str = f"({size / (1024**2):.1f} MB)"
                else:
                    size_str = f"({size / 1024:.1f} KB)"
                log("AZURE-ML", f"  - {name} {size_str}")
                result["blob_files"].append(f)
    except RuntimeError as e:
        log("AZURE-ML", f"  (error: {e})")

    log("AZURE-ML", "")

    # 3. File share
    log("AZURE-ML", "File Share (startup scripts):")
    code_share = get_azure_ml_file_share_name()
    if code_share:
        log("AZURE-ML", f"  Share: {code_share}")
        log("AZURE-ML", "  - Users/openadapt/compute-instance-startup.sh")
        result["file_share_files"].append("Users/openadapt/compute-instance-startup.sh")
    else:
        log("AZURE-ML", "  (not found)")

    return result


def teardown_azure_ml_resources(confirm: bool = False, keep_image: bool = False) -> bool:
    """Teardown Azure ML resources to stop all costs.

    Args:
        confirm: If True, actually delete resources. If False, dry run.
        keep_image: If True, preserve the golden image in blob storage.

    Returns:
        True if successful
    """
    log("AZURE-ML", "=== Azure ML Teardown ===")
    log("AZURE-ML", "")

    # 1. List what will be deleted
    instances = list_azure_ml_compute_instances()

    try:
        storage_account, storage_key, blob_container = get_azure_ml_storage_info()
        blob_files = list_azure_ml_blob_files(
            storage_account, storage_key, blob_container, "storage/"
        )
    except RuntimeError as e:
        log("AZURE-ML", f"Warning: Could not access storage: {e}")
        storage_account = None
        storage_key = None
        blob_container = None
        blob_files = []

    code_share = get_azure_ml_file_share_name()

    # Show what will be deleted
    log("AZURE-ML", "Will delete:")
    log("AZURE-ML", "")

    # Compute instances
    log("AZURE-ML", "  Compute Instances:")
    if instances:
        for inst in instances:
            name = inst.get("name", "unknown")
            state = inst.get("state", "unknown")
            vm_size = inst.get("vmSize", "unknown")
            log("AZURE-ML", f"    - {name} ({vm_size}, {state})")
    else:
        log("AZURE-ML", "    (none)")

    # Blob storage
    log("AZURE-ML", "")
    log("AZURE-ML", "  Blob Storage:")
    if blob_files:
        for f in blob_files:
            name = f.get("name", "").replace("storage/", "")
            # Check if this is the golden image
            is_golden_image = name in [
                "data.img",
                "OVMF_CODE_4M.ms.fd",
                "OVMF_VARS_4M.ms.fd",
            ]
            if is_golden_image and keep_image:
                log("AZURE-ML", f"    - {name} (KEEPING - golden image)")
            else:
                size = f.get("size", 0) or 0
                size_str = f"({size / (1024**3):.1f} GB)" if size >= 1024**3 else ""
                log("AZURE-ML", f"    - {name} {size_str}")
    else:
        log("AZURE-ML", "    (none)")

    # File share
    log("AZURE-ML", "")
    log("AZURE-ML", "  File Share:")
    if code_share:
        log("AZURE-ML", "    - Users/openadapt/compute-instance-startup.sh")
    else:
        log("AZURE-ML", "    (none)")

    log("AZURE-ML", "")

    if not confirm:
        log("AZURE-ML", "This is a DRY RUN. Use --confirm to actually delete resources.")
        log("AZURE-ML", "Use --keep-image to preserve the golden image for future runs.")
        return True

    # Actually delete resources
    log("AZURE-ML", "Proceeding with deletion...")
    log("AZURE-ML", "")

    deleted_count = 0

    # Delete compute instances
    for inst in instances:
        name = inst.get("name", "")
        if name and delete_azure_ml_compute_instance(name):
            deleted_count += 1

    # Delete blob files
    if storage_account and storage_key and blob_container and blob_files:
        golden_image_files = {"data.img", "OVMF_CODE_4M.ms.fd", "OVMF_VARS_4M.ms.fd"}
        for f in blob_files:
            name = f.get("name", "")
            short_name = name.replace("storage/", "")

            # Skip golden image if keeping
            if keep_image and short_name in golden_image_files:
                log("AZURE-ML", f"Keeping golden image file: {short_name}")
                continue

            if name:
                log("AZURE-ML", f"Deleting blob: {name}...")
                result = subprocess.run(
                    [
                        "az",
                        "storage",
                        "blob",
                        "delete",
                        "--account-name",
                        storage_account,
                        "--account-key",
                        storage_key,
                        "--container-name",
                        blob_container,
                        "--name",
                        name,
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    deleted_count += 1

    # Delete startup script
    if code_share and storage_account and storage_key:
        deleted_count += delete_azure_ml_file_share_files(
            storage_account,
            storage_key,
            code_share,
            "Users/openadapt/compute-instance-startup.sh",
        )

    log("AZURE-ML", "")
    log("AZURE-ML", f"=== Teardown Complete: {deleted_count} resources deleted ===")

    if keep_image:
        log("AZURE-ML", "Golden image preserved for future runs.")
        log("AZURE-ML", "To re-run setup: run-azure-ml --setup")
    else:
        log("AZURE-ML", "All resources deleted. To start fresh:")
        log("AZURE-ML", "  1. run-azure-ml --setup")
        log("AZURE-ML", "  2. run-azure-ml --upload-image (or --upload-placeholder)")
        log("AZURE-ML", "  3. run-azure-ml --workers 1")

    return True


# =============================================================================
# Fully Automated Azure ML Workflow
# =============================================================================


def cmd_run_azure_ml_auto(args):
    """Fully automated Azure ML workflow - VM setup through benchmark execution.

    This command handles the entire workflow unattended:
    1. Create/start Azure VM if needed
    2. Start Windows container with VERSION=11e
    3. Wait for Windows installation and WAA server to become ready
    4. Upload golden image from VM to blob storage (if needed)
    5. Run Azure ML benchmark

    All steps are idempotent - skips steps that are already complete.
    """
    init_logging()
    start_time = time.time()

    from openadapt_evals.config import settings

    log("AUTO", "=" * 60)
    log("AUTO", "FULLY AUTOMATED AZURE ML WORKFLOW")
    log("AUTO", "=" * 60)
    log("AUTO", "")

    # Validate required settings
    required = [
        ("azure_subscription_id", "AZURE_SUBSCRIPTION_ID"),
        ("azure_ml_resource_group", "AZURE_ML_RESOURCE_GROUP"),
        ("azure_ml_workspace_name", "AZURE_ML_WORKSPACE_NAME"),
        ("openai_api_key", "OPENAI_API_KEY"),
    ]

    missing = []
    for attr, env_name in required:
        if not getattr(settings, attr, None):
            missing.append(env_name)
    if missing:
        log("AUTO", f"ERROR: Missing required settings in .env: {', '.join(missing)}")
        return 1

    # Get parameters
    num_workers = getattr(args, "workers", 1)
    timeout_minutes = getattr(args, "timeout", 45)  # Total timeout for setup
    probe_timeout = getattr(args, "probe_timeout", 1800)  # 30 min for WAA server
    skip_upload = getattr(args, "skip_upload", False)
    skip_benchmark = getattr(args, "skip_benchmark", False)

    log("AUTO", "Configuration:")
    log("AUTO", f"  Workers: {num_workers}")
    log("AUTO", f"  Setup timeout: {timeout_minutes} min")
    log("AUTO", f"  Probe timeout: {probe_timeout} sec")
    log("AUTO", "")

    # =========================================================================
    # Step 1: Ensure VM exists and is running
    # =========================================================================
    log("AUTO", "[Step 1/5] Checking Azure VM...")

    ip = get_vm_ip()
    if ip:
        # VM exists, check if it's running
        state = get_vm_state()
        if state and "running" in state.lower():
            log("AUTO", f"  VM already running at {ip}")
        elif state and "deallocated" in state.lower():
            log("AUTO", "  VM deallocated, starting...")
            result = subprocess.run(
                ["az", "vm", "start", "-g", RESOURCE_GROUP, "-n", VM_NAME],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log("AUTO", f"  ERROR: Failed to start VM: {result.stderr}")
                return 1
            # Wait for SSH
            ip = get_vm_ip()
            if not ip or not wait_for_ssh(ip):
                log("AUTO", "  ERROR: SSH not available after starting VM")
                return 1
            log("AUTO", f"  VM started at {ip}")
        else:
            log("AUTO", f"  VM in state '{state}', waiting...")
            time.sleep(30)
            state = get_vm_state()
            if not state or "running" not in state.lower():
                log("AUTO", f"  ERROR: VM not running (state: {state})")
                return 1
    else:
        # Need to create VM
        log("AUTO", "  VM not found, creating...")

        # Build args for cmd_create
        class CreateArgs:
            workers = 1

        result = cmd_create(CreateArgs())
        if result != 0:
            log("AUTO", "  ERROR: Failed to create VM")
            return 1

        ip = get_vm_ip()
        if not ip:
            log("AUTO", "  ERROR: VM created but IP not found")
            return 1
        log("AUTO", f"  VM created at {ip}")

    # Ensure SSH is ready
    if not wait_for_ssh(ip, timeout=60):
        log("AUTO", "  ERROR: SSH not available")
        return 1

    log("AUTO", "  [OK] VM ready")
    log("AUTO", "")

    # =========================================================================
    # Step 2: Check if container is running, start if needed
    # =========================================================================
    log("AUTO", "[Step 2/5] Checking Windows container...")

    # Check if winarena container is running
    result = ssh_run(ip, "docker ps --filter name=winarena --format '{{.Status}}'")
    container_running = result.returncode == 0 and result.stdout.strip()

    if container_running:
        log("AUTO", f"  Container already running: {result.stdout.strip()}")
    else:
        log("AUTO", "  Starting Windows container with VERSION=11e...")

        # Pull image if needed
        result = ssh_run(ip, "docker images windowsarena/winarena:latest --format '{{.ID}}'")
        if not result.stdout.strip():
            log("AUTO", "  Pulling windowsarena/winarena:latest...")
            result = ssh_run(
                ip, "docker pull windowsarena/winarena:latest", stream=True, step="PULL"
            )
            if result.returncode != 0:
                log("AUTO", "  ERROR: Failed to pull Docker image")
                return 1

        # Create storage directory (on persistent OS disk, not ephemeral /mnt)
        ssh_run(
            ip,
            f"sudo mkdir -p {WAA_STORAGE_DIR} && sudo chown azureuser:azureuser {WAA_STORAGE_DIR}",
        )

        # Stop any existing container
        ssh_run(ip, "docker stop winarena 2>/dev/null; docker rm -f winarena 2>/dev/null")

        # Start container with VERSION=11e
        ram_size = "16G"
        cpu_cores = 6

        docker_cmd = f"""docker run -d \\
  --name winarena \\
  --device=/dev/kvm \\
  --cap-add NET_ADMIN \\
  --stop-timeout 120 \\
  -p 8006:8006 \\
  -p 5000:5000 \\
  -p 7200:7200 \\
  -v {WAA_STORAGE_DIR}:/storage \\
  -e VERSION=11e \\
  -e RAM_SIZE={ram_size} \\
  -e CPU_CORES={cpu_cores} \\
  -e DISK_SIZE=64G \\
  -p 5050:5050 \\
  waa-auto:latest \\
  /run/entry.sh --prepare-image false --start-client false"""
        # Note: --start-client false for setup - just boot Windows + Flask server
        # Azure ML compute instances run the benchmark separately via run_entry.py

        result = ssh_run(ip, docker_cmd)
        if result.returncode != 0:
            log("AUTO", f"  ERROR: Failed to start container: {result.stderr}")
            return 1

        log("AUTO", "  Container started")

    log("AUTO", "  [OK] Container running")
    log("AUTO", "")

    # =========================================================================
    # Step 3: Wait for WAA server to become ready
    # =========================================================================
    log("AUTO", "[Step 3/5] Waiting for WAA server...")
    log("AUTO", "  (This may take 15-20 minutes on first run)")
    log("AUTO", f"  Timeout: {probe_timeout} seconds")

    probe_start = time.time()
    last_status = None
    poll_interval = 30  # Check every 30 seconds

    while True:
        elapsed = time.time() - probe_start
        if elapsed > probe_timeout:
            log("AUTO", f"  TIMEOUT: WAA server not ready after {probe_timeout}s")
            log("AUTO", "  Check VNC at http://localhost:8006 (via SSH tunnel)")
            return 1

        # Check probe endpoint
        result = ssh_run(
            ip,
            "docker exec winarena curl -s --max-time 5 http://172.30.0.2:5000/probe 2>/dev/null || echo FAIL",
        )

        if "FAIL" not in result.stdout and result.stdout.strip():
            log("AUTO", "")
            log("AUTO", "  [OK] WAA server is READY!")
            break

        # Show progress
        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)

        # Get storage size for progress indication
        storage_result = ssh_run(ip, "docker exec winarena du -sh /storage/ 2>/dev/null | cut -f1")
        storage_size = storage_result.stdout.strip() or "unknown"

        # Get container status
        container_result = ssh_run(
            ip, "docker ps --filter name=winarena --format '{{.Status}}' 2>/dev/null"
        )
        container_status = container_result.stdout.strip() or "unknown"

        status = f"  [{elapsed_min:02d}:{elapsed_sec:02d}] Storage: {storage_size}, Container: {container_status}"
        if status != last_status:
            log("AUTO", status)
            last_status = status

        time.sleep(poll_interval)

    log("AUTO", "")

    # =========================================================================
    # Step 4: Upload golden image to blob storage (if needed)
    # =========================================================================
    log("AUTO", "[Step 4/5] Checking golden image in blob storage...")

    if skip_upload:
        log("AUTO", "  Skipping upload (--skip-upload specified)")
    else:
        try:
            storage_account, storage_key, blob_container = get_azure_ml_storage_info()
            image_info = check_golden_image_in_blob(storage_account, storage_key, blob_container)

            if image_info["exists"]:
                log(
                    "AUTO",
                    f"  Golden image already exists ({image_info['total_size'] / (1024**3):.2f} GB)",
                )
                log("AUTO", f"  Files: {image_info['found_files']}")
            else:
                log("AUTO", "  Golden image not found, uploading from VM...")
                log("AUTO", "  (This may take 5-10 minutes for ~25GB)")

                # Check if golden image exists on VM
                result = ssh_run(
                    ip,
                    f"ls -la {WAA_STORAGE_DIR}/data.img 2>/dev/null || echo 'NOT_FOUND'",
                )
                if "NOT_FOUND" in result.stdout:
                    log(
                        "AUTO",
                        f"  ERROR: Golden image not found on VM at {WAA_STORAGE_DIR}/",
                    )
                    log("AUTO", "  The Windows installation may not have completed.")
                    log("AUTO", "  Wait for WAA server to be ready first, or check VNC.")
                    return 1

                # Upload using existing function
                success = upload_golden_image_from_vm()
                if not success:
                    log("AUTO", "  ERROR: Failed to upload golden image")
                    return 1

                log("AUTO", "  [OK] Golden image uploaded")
        except RuntimeError as e:
            log("AUTO", f"  ERROR: {e}")
            return 1

    log("AUTO", "")

    # =========================================================================
    # Step 5: Run Azure ML benchmark
    # =========================================================================
    log("AUTO", "[Step 5/5] Running Azure ML benchmark...")

    if skip_benchmark:
        log("AUTO", "  Skipping benchmark (--skip-benchmark specified)")
    else:
        # Upload embedded startup script
        startup_script_datastore_path = "Users/openadapt/compute-instance-startup.sh"
        log("AUTO", "  Ensuring startup script is uploaded...")
        success = upload_startup_script_to_datastore(
            COMPUTE_INSTANCE_STARTUP_SH, startup_script_datastore_path
        )
        if not success:
            log("AUTO", "  WARNING: Failed to upload startup script")
            # Continue anyway - it might already be there

        log(
            "AUTO",
            "  ERROR: run-azure-ml-auto requires vendor/WindowsAgentArena submodule "
            "(removed). Use pool-create + pool-run instead.",
        )
        return 1

    # =========================================================================
    # Complete
    # =========================================================================
    elapsed = time.time() - start_time
    elapsed_min = int(elapsed // 60)
    elapsed_sec = int(elapsed % 60)

    log("AUTO", "")
    log("AUTO", "=" * 60)
    log("AUTO", f"WORKFLOW COMPLETE ({elapsed_min}m {elapsed_sec}s)")
    log("AUTO", "=" * 60)
    log("AUTO", "")
    log("AUTO", "Next steps:")
    log("AUTO", "  - View benchmark progress in Azure ML portal")
    log("AUTO", "  - Check costs: run-azure-ml --cost-summary")
    log("AUTO", "  - Cleanup when done: run-azure-ml --teardown --confirm")
    log("AUTO", "")
    log("AUTO", "To stop the VM (saves costs):")
    log("AUTO", "  uv run python -m openadapt_evals.benchmarks.vm_cli deallocate")

    return 0


def cmd_run_azure_ml(args):
    """Run WAA benchmark on Azure ML using compute instances.

    This wraps vanilla WAA's run_azure.py with proper startup script setup.
    Uses --setup to upload the required startup script to Azure datastore.
    """
    init_logging()

    from openadapt_evals.config import settings

    # Validate required settings
    required = [
        ("azure_subscription_id", "AZURE_SUBSCRIPTION_ID"),
        ("azure_ml_resource_group", "AZURE_ML_RESOURCE_GROUP"),
        ("azure_ml_workspace_name", "AZURE_ML_WORKSPACE_NAME"),
    ]

    # Only require OpenAI key if actually running benchmark (not setup/image/management operations)
    is_management_operation = (
        getattr(args, "setup", False)
        or getattr(args, "check_image", False)
        or getattr(args, "upload_image", False)
        or getattr(args, "upload_placeholder", False)
        or getattr(args, "upload_image_from_vm", False)
        or getattr(args, "cost_summary", False)
        or getattr(args, "list_resources", False)
        or getattr(args, "teardown", False)
    )
    if not is_management_operation:
        required.append(("openai_api_key", "OPENAI_API_KEY"))

    missing = []
    for attr, env_name in required:
        if not getattr(settings, attr, None):
            missing.append(env_name)
    if missing:
        log(
            "AZURE-ML",
            f"ERROR: Missing required settings in .env: {', '.join(missing)}",
        )
        return 1

    # Startup script datastore path
    startup_script_datastore_path = (
        getattr(args, "ci_startup_script_path", None)
        or "Users/openadapt/compute-instance-startup.sh"
    )

    # Handle --setup: upload startup script to datastore
    if getattr(args, "setup", False):
        log(
            "AZURE-ML",
            "=== SETUP MODE: Uploading startup script to Azure ML datastore ===",
        )

        success = upload_startup_script_to_datastore(
            COMPUTE_INSTANCE_STARTUP_SH, startup_script_datastore_path
        )

        if success:
            log("AZURE-ML", "")
            log("AZURE-ML", "=== SETUP COMPLETE ===")
            log(
                "AZURE-ML",
                f"Startup script uploaded to: {startup_script_datastore_path}",
            )
            log("AZURE-ML", "")
            log("AZURE-ML", "Next steps:")
            log("AZURE-ML", "  1. Check/upload golden image:")
            log(
                "AZURE-ML",
                "     uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --check-image",
            )
            log(
                "AZURE-ML",
                "     uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --upload-image",
            )
            log("AZURE-ML", "")
            log("AZURE-ML", "  2. Run benchmark:")
            log(
                "AZURE-ML",
                "     uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --workers 1",
            )
            return 0
        else:
            log("AZURE-ML", "ERROR: Setup failed")
            return 1

    # Handle --check-image: verify golden image exists in blob storage
    if getattr(args, "check_image", False):
        log("AZURE-ML", "=== CHECK IMAGE: Verifying golden image in blob storage ===")
        try:
            storage_account, storage_key, blob_container = get_azure_ml_storage_info()
            info = check_golden_image_in_blob(storage_account, storage_key, blob_container)

            if info["exists"]:
                log("AZURE-ML", "")
                log("AZURE-ML", "=== GOLDEN IMAGE FOUND ===")
                log("AZURE-ML", f"Total size: {info['total_size'] / (1024**3):.2f} GB")
                log("AZURE-ML", f"Files: {info['found_files']}")
                log("AZURE-ML", "")
                log("AZURE-ML", "Ready to run benchmark:")
                log(
                    "AZURE-ML",
                    "  uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --workers 1",
                )
                return 0
            else:
                log("AZURE-ML", "")
                log("AZURE-ML", "=== GOLDEN IMAGE NOT FOUND ===")
                if info["files"]:
                    log("AZURE-ML", f"Found partial files: {info['found_files']}")
                log("AZURE-ML", "")
                log("AZURE-ML", "To upload golden image:")
                log(
                    "AZURE-ML",
                    "  1. Prepare locally: clone WindowsAgentArena and run ./scripts/run.sh --prepare-image true",
                )
                log(
                    "AZURE-ML",
                    "  2. Upload: uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --upload-image",
                )
                log("AZURE-ML", "")
                log("AZURE-ML", "Alternative (no upload, slower first run):")
                log(
                    "AZURE-ML",
                    "  uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --upload-placeholder",
                )
                return 1
        except RuntimeError as e:
            log("AZURE-ML", f"ERROR: {e}")
            return 1

    # Handle --upload-image: upload golden image to blob storage
    if getattr(args, "upload_image", False):
        log("AZURE-ML", "=== UPLOAD IMAGE: Uploading golden image to blob storage ===")

        # Determine source path (--image-source required since submodule was removed)
        source_path = getattr(args, "image_source", None)
        if not source_path:
            log("AZURE-ML", "ERROR: --image-source is required (no default path)")
            return 1

        success = upload_golden_image_to_blob(source_path)
        if success:
            log("AZURE-ML", "")
            log("AZURE-ML", "=== UPLOAD COMPLETE ===")
            log("AZURE-ML", "Ready to run benchmark:")
            log(
                "AZURE-ML",
                "  uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --workers 1",
            )
            return 0
        else:
            log("AZURE-ML", "ERROR: Upload failed")
            return 1

    # Handle --upload-placeholder: create placeholder for VERSION=11e approach
    if getattr(args, "upload_placeholder", False):
        log(
            "AZURE-ML",
            "=== UPLOAD PLACEHOLDER: Creating placeholder for VERSION=11e ===",
        )

        success = upload_placeholder_to_blob()
        if success:
            log("AZURE-ML", "")
            log("AZURE-ML", "=== PLACEHOLDER CREATED ===")
            log(
                "AZURE-ML",
                "Ready to run benchmark (Windows will auto-download on first run):",
            )
            log(
                "AZURE-ML",
                "  uv run python -m openadapt_evals.benchmarks.vm_cli run-azure-ml --workers 1",
            )
            return 0
        else:
            log("AZURE-ML", "ERROR: Placeholder creation failed")
            return 1

    # Handle --upload-image-from-vm: upload golden image from Azure VM to blob
    if getattr(args, "upload_image_from_vm", False):
        log("AZURE-ML", "=== UPLOAD FROM VM: Uploading golden image from Azure VM ===")

        success = upload_golden_image_from_vm()
        if success:
            return 0
        else:
            log("AZURE-ML", "ERROR: Upload from VM failed")
            return 1

    # Handle --cost-summary: show cost summary
    if getattr(args, "cost_summary", False):
        show_azure_ml_cost_summary()
        return 0

    # Handle --list-resources: list all resources
    if getattr(args, "list_resources", False):
        show_azure_ml_resources()
        return 0

    # Handle --teardown: delete all resources
    if getattr(args, "teardown", False):
        confirm = getattr(args, "confirm", False)
        keep_image = getattr(args, "keep_image", False)
        teardown_azure_ml_resources(confirm=confirm, keep_image=keep_image)
        return 0

    # Direct benchmark execution via run_azure.py is no longer available
    # (vendor/WindowsAgentArena submodule was removed). Use pool commands instead.
    log("AZURE-ML", "ERROR: Direct Azure ML benchmark execution is no longer available.")
    log("AZURE-ML", "The vendor/WindowsAgentArena submodule has been removed.")
    log("AZURE-ML", "")
    log("AZURE-ML", "Use pool-based execution instead:")
    log(
        "AZURE-ML",
        "  uv run python -m openadapt_evals.benchmarks.vm_cli pool-create --workers N",
    )
    log("AZURE-ML", "  uv run python -m openadapt_evals.benchmarks.vm_cli pool-run --tasks N")
    return 1


def get_azure_ml_dedicated_quota(subscription_id: str, location: str) -> dict:
    """Get Azure ML Dedicated quota using REST API.

    IMPORTANT: Azure ML uses "Dedicated" quota (BatchAI), NOT regular VM quota!
    - VM quota (az vm list-usage): For regular Azure VMs
    - ML Dedicated quota: For Azure ML compute instances

    These are DIFFERENT quotas! Having 10 vCPUs VM quota doesn't mean you can
    create ML compute instances - you need ML Dedicated quota.

    Returns dict with:
        - quota: Current limit (vCPUs)
        - usage: Current usage (vCPUs)
        - available: quota - usage
        - error: Error message if any
    """
    # Get access token
    token_result = subprocess.run(
        ["az", "account", "get-access-token", "--query", "accessToken", "-o", "tsv"],
        capture_output=True,
        text=True,
    )
    if token_result.returncode != 0:
        return {
            "error": f"Failed to get access token: {token_result.stderr}",
            "quota": 0,
            "usage": 0,
            "available": 0,
        }

    token = token_result.stdout.strip()

    # Azure ML Dedicated quota API endpoint
    # Resource name for dedicated quota is "standardDDSv4Family" (no spaces, camelCase)
    url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.MachineLearningServices/locations/{location}/usages?api-version=2024-04-01"

    import urllib.error
    import urllib.request

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        return {
            "error": f"HTTP {e.code}: {e.reason}",
            "quota": 0,
            "usage": 0,
            "available": 0,
        }
    except Exception as e:
        return {"error": str(e), "quota": 0, "usage": 0, "available": 0}

    # Find Dedicated quota in response
    # Look for entries with "Dedicated" in the name
    result = {"quota": 0, "usage": 0, "available": 0, "error": None, "details": []}

    for usage in data.get("value", []):
        name = usage.get("name", {})
        local_name = name.get("localizedValue", "")

        # Azure ML dedicated quota shows as "Dedicated <family> Family vCPUs"
        if "Dedicated" in local_name or "dedicated" in local_name.lower():
            current = usage.get("currentValue", 0)
            limit = usage.get("limit", 0)
            result["details"].append(
                {
                    "name": local_name,
                    "usage": current,
                    "quota": limit,
                    "available": limit - current,
                }
            )

            # Sum up total dedicated quota
            result["quota"] += limit
            result["usage"] += current

    result["available"] = result["quota"] - result["usage"]
    return result


# ARM-based VM sizes that won't run x86 Docker images
ARM_VM_SIZES = [
    "Standard_D8pds_v5",
    "Standard_D4pds_v5",
    "Standard_D16pds_v5",
    "Standard_D32pds_v5",
    "Standard_D8plds_v5",
    "Standard_D4plds_v5",
    # Add more ARM sizes as needed - any size with 'p' before 'ds' or 'ls'
]


def is_arm_vm_size(vm_size: str) -> bool:
    """Check if a VM size is ARM-based (won't run x86 Docker images)."""
    # ARM VMs have 'p' after the number in the D series (e.g., D8pds_v5, D8plds_v5)
    # Pattern: Standard_D{N}p{optional-l}{type}_v{version}
    # The 'p' indicates ARM processor (Ampere Altra)
    import re

    return bool(re.search(r"_D\d+pl?[ld]?s_v\d+", vm_size, re.IGNORECASE))


def cmd_azure_ml_quota(args):
    """Check Azure ML quota and help request increases.

    IMPORTANT: This checks BOTH:
    1. VM quota (az vm list-usage) - for regular Azure VMs
    2. Azure ML Dedicated quota - for ML compute instances (DIFFERENT!)

    Azure ML compute instances require "Dedicated" quota, not VM quota.
    """
    init_logging()

    from openadapt_evals.config import settings

    subscription_id = settings.azure_subscription_id
    location = getattr(args, "location", None) or "centralus"  # Default to Central US workspace

    # Try to get workspace location from settings
    workspace_name = getattr(settings, "azure_ml_workspace_name", "openadapt-ml-central")
    if "central" in workspace_name.lower():
        location = "centralus"

    log("QUOTA", "=" * 60)
    log("QUOTA", "AZURE ML QUOTA CHECK")
    log("QUOTA", "=" * 60)
    log("QUOTA", "")
    log("QUOTA", f"Workspace: {workspace_name}")
    log("QUOTA", f"Region:    {location}")
    log("QUOTA", "")

    # =========================================================================
    # SECTION 1: Azure ML Dedicated Quota (what actually matters for ML)
    # =========================================================================
    log("QUOTA", "=" * 60)
    log("QUOTA", "1. AZURE ML DEDICATED QUOTA (for ML compute instances)")
    log("QUOTA", "=" * 60)
    log("QUOTA", "")
    log("QUOTA", "NOTE: Azure ML compute uses 'Dedicated' quota, NOT VM quota!")
    log("QUOTA", "      Even with 10 vCPU VM quota, you need Dedicated quota.")
    log("QUOTA", "")

    ml_quota = get_azure_ml_dedicated_quota(subscription_id, location)

    if ml_quota.get("error"):
        log("QUOTA", f"ERROR: {ml_quota['error']}")
    else:
        log(
            "QUOTA",
            f"Total Dedicated: {ml_quota['usage']}/{ml_quota['quota']} vCPUs used",
        )
        log("QUOTA", f"Available:       {ml_quota['available']} vCPUs")
        log("QUOTA", "")

        if ml_quota.get("details"):
            log("QUOTA", "Breakdown by family:")
            for d in ml_quota["details"]:
                status = "OK" if d["available"] >= 8 else "LOW"
                log("QUOTA", f"  {d['name']}: {d['usage']}/{d['quota']} [{status}]")

        # Check if we have enough for WAA (need 8 vCPUs for D8ds_v4)
        if ml_quota["available"] >= 8:
            log("QUOTA", "")
            log("QUOTA", ">>> You have sufficient ML Dedicated quota for WAA (8+ vCPUs)")
        else:
            log("QUOTA", "")
            log(
                "QUOTA",
                ">>> INSUFFICIENT ML Dedicated quota! Need 8 vCPUs, have "
                + str(ml_quota["available"]),
            )
            log("QUOTA", "")
            log("QUOTA", "Request quota increase at:")
            ml_quota_url = f"https://ml.azure.com/quota/{subscription_id}/{location}"
            log("QUOTA", f"  {ml_quota_url}")

    # =========================================================================
    # SECTION 2: Regular VM Quota (for reference)
    # =========================================================================
    log("QUOTA", "")
    log("QUOTA", "=" * 60)
    log("QUOTA", "2. VM FAMILY QUOTA (for regular VMs, NOT ML compute)")
    log("QUOTA", "=" * 60)
    log("QUOTA", "")

    result = subprocess.run(
        ["az", "vm", "list-usage", "--location", location, "-o", "json"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("QUOTA", f"ERROR: Failed to get VM quota: {result.stderr}")
    else:
        usages = json.loads(result.stdout)

        # Find relevant VM families for WAA
        relevant_families = [
            ("Standard DDSv4 Family", "D8ds_v4", 300, 8, False),  # x86, 300GB temp
            ("Standard DDSv5 Family", "D8ds_v5", 300, 8, False),  # x86, 300GB temp
            ("Standard DPDSv5 Family", "D8pds_v5", 300, 8, True),  # ARM! Won't work
            ("Standard DSv4 Family", "D8s_v4", 0, 8, False),  # No local SSD
            ("Standard D Family", "D4_v3", 100, 4, False),  # 100GB temp
        ]

        log("QUOTA", "VM Family Quotas (300GB temp storage required for WAA):")
        log("QUOTA", "-" * 60)

        for family_name, vm_example, temp_gb, vcpus_needed, is_arm in relevant_families:
            for usage in usages:
                if usage["name"]["localizedValue"] == family_name:
                    current = usage["currentValue"]
                    limit = usage["limit"]

                    if is_arm:
                        status = "ARM!"
                        note = "ARM-based, won't run x86 Docker"
                    elif limit >= vcpus_needed:
                        status = "OK"
                        note = f"{temp_gb}GB temp" if temp_gb > 0 else "no local SSD"
                    else:
                        status = "LOW"
                        note = f"{temp_gb}GB temp" if temp_gb > 0 else "no local SSD"

                    log("QUOTA", f"  [{status:4}] {family_name}")
                    log(
                        "QUOTA",
                        f"         {current}/{limit} vCPUs - {vm_example} ({note})",
                    )
                    break

    # =========================================================================
    # SECTION 3: Recommendations
    # =========================================================================
    log("QUOTA", "")
    log("QUOTA", "=" * 60)
    log("QUOTA", "RECOMMENDATIONS")
    log("QUOTA", "=" * 60)
    log("QUOTA", "")
    log("QUOTA", "For WAA evaluation:")
    log("QUOTA", "  1. Use D8ds_v4 (x86, 300GB temp storage)")
    log("QUOTA", "  2. Ensure workspace region = compute region")
    log("QUOTA", f"     Current workspace: {workspace_name} ({location})")
    log("QUOTA", "  3. Request ML Dedicated quota (not VM quota!) at:")
    log("QUOTA", f"     https://ml.azure.com/quota/{subscription_id}/{location}")
    log("QUOTA", "")
    log("QUOTA", "AVOID:")
    log("QUOTA", "  - D8pds_v5 (ARM-based, won't run x86 Docker/QEMU)")
    log("QUOTA", "  - D4ds_v4 (only 150GB temp, not enough for WAA)")
    log("QUOTA", "  - D4_v3 (only 100GB temp)")
    log("QUOTA", "")

    # Open browser if requested
    if getattr(args, "open", True):
        ml_quota_url = f"https://ml.azure.com/quota/{subscription_id}/{location}"
        log("QUOTA", f"Opening ML quota page: {ml_quota_url}")
        webbrowser.open(ml_quota_url)

    return 0


def cmd_azure_ml_quota_request(args):
    """Request Azure quota increase via CLI automation.

    Uses the `az quota` CLI extension to programmatically request quota increases.
    Small requests (e.g., 8 vCPUs) are usually auto-approved instantly.
    """
    init_logging()

    from openadapt_evals.config import settings

    subscription_id = settings.azure_subscription_id
    location = getattr(args, "location", "eastus")
    family = getattr(args, "family", "standardDPDSv5Family")
    vcpus = getattr(args, "vcpus", 8)

    if not subscription_id:
        log("QUOTA", "ERROR: AZURE_SUBSCRIPTION_ID not set in .env")
        return 1

    log("QUOTA", "=" * 60)
    log("QUOTA", "AZURE QUOTA REQUEST (AUTOMATED)")
    log("QUOTA", "=" * 60)
    log("QUOTA", "")
    log("QUOTA", f"Family:       {family}")
    log("QUOTA", f"vCPUs:        {vcpus}")
    log("QUOTA", f"Location:     {location}")
    log("QUOTA", f"Subscription: {subscription_id}")
    log("QUOTA", "")

    # Build scope for the quota API
    scope = f"/subscriptions/{subscription_id}/providers/Microsoft.Compute/locations/{location}"

    # Check if az quota extension is available
    log("QUOTA", "Checking az quota extension...")
    result = subprocess.run(
        ["az", "extension", "show", "--name", "quota", "-o", "json"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log("QUOTA", "Installing az quota extension...")
        install_result = subprocess.run(
            ["az", "extension", "add", "--name", "quota", "-y"],
            capture_output=True,
            text=True,
        )
        if install_result.returncode != 0:
            log(
                "QUOTA",
                f"ERROR: Failed to install quota extension: {install_result.stderr}",
            )
            return 1
        log("QUOTA", "Extension installed successfully")
    else:
        log("QUOTA", "Extension already installed")

    # Check current quota first
    log("QUOTA", "")
    log("QUOTA", "Checking current quota...")

    current_result = subprocess.run(
        [
            "az",
            "quota",
            "show",
            "--resource-name",
            family,
            "--scope",
            scope,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    import json

    current_limit = 0
    if current_result.returncode == 0:
        try:
            quota_data = json.loads(current_result.stdout)
            current_limit = quota_data.get("properties", {}).get("limit", {}).get("value", 0)
            log("QUOTA", f"Current limit: {current_limit} vCPUs")
        except (json.JSONDecodeError, KeyError):
            log("QUOTA", "Could not parse current quota (may be 0)")
    else:
        log("QUOTA", f"Could not check current quota: {current_result.stderr[:200]}")

    if current_limit >= vcpus:
        log("QUOTA", "")
        log(
            "QUOTA",
            f"Current quota ({current_limit}) is already >= requested ({vcpus})",
        )
        log("QUOTA", "No increase needed!")
        return 0

    # Request quota increase
    log("QUOTA", "")
    log("QUOTA", f"Requesting quota increase to {vcpus} vCPUs...")

    request_result = subprocess.run(
        [
            "az",
            "quota",
            "create",
            "--resource-name",
            family,
            "--scope",
            scope,
            "--limit-object",
            f"value={vcpus}",
            "--resource-type",
            "dedicated",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    if request_result.returncode != 0:
        log("QUOTA", f"ERROR: Quota request failed: {request_result.stderr}")
        log("QUOTA", "")
        log("QUOTA", "This may happen if:")
        log("QUOTA", "  - The subscription doesn't have permission for this quota")
        log("QUOTA", "  - The resource name is incorrect")
        log("QUOTA", "  - Azure requires manual approval for this region/family")
        log("QUOTA", "")
        log("QUOTA", "Try requesting via Azure Portal instead:")
        quota_url = (
            f"https://portal.azure.com/#view/Microsoft_Azure_Capacity/QuotaMenuBlade/"
            f"~/myQuotas/provider/Microsoft.Compute/location/{location}"
        )
        log("QUOTA", f"  {quota_url}")
        return 1

    # Parse response
    try:
        response = json.loads(request_result.stdout)
        status = response.get("properties", {}).get("provisioningState", "Unknown")
        log("QUOTA", "")
        log("QUOTA", f"Request submitted! Status: {status}")

        if status.lower() in ["succeeded", "approved"]:
            log("QUOTA", "Quota increase approved immediately!")
        else:
            log("QUOTA", "Request is being processed. Check status with:")
            log("QUOTA", f'  az quota request list --scope "{scope}"')
    except json.JSONDecodeError:
        log("QUOTA", "Request submitted (response was not JSON)")
        log("QUOTA", f"stdout: {request_result.stdout[:500]}")

    log("QUOTA", "")
    log("QUOTA", "=" * 60)

    return 0


def get_quota_status(location: str, family: str, target_vcpus: int) -> dict:
    """Get quota status for a VM family.

    Args:
        location: Azure region (e.g., 'eastus')
        family: VM family name (e.g., 'Standard DDSv4 Family')
        target_vcpus: Target vCPU count to check against

    Returns:
        dict with keys: family, current, limit, sufficient, error
    """
    result = subprocess.run(
        ["az", "vm", "list-usage", "--location", location, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return {"error": result.stderr, "sufficient": False}

    try:
        usages = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse JSON: {e}", "sufficient": False}

    for usage in usages:
        if usage["name"]["localizedValue"] == family:
            limit = usage["limit"]
            current = usage["currentValue"]
            return {
                "family": family,
                "current": current,
                "limit": limit,
                "sufficient": limit >= target_vcpus,
                "error": None,
            }

    return {"error": f"Family '{family}' not found", "sufficient": False}


def cmd_azure_ml_quota_wait(args):
    """Wait for Azure quota approval with polling.

    Polls Azure quota API until the specified VM family has sufficient vCPUs,
    then optionally runs the evaluation automatically.
    """
    init_logging()

    location = getattr(args, "location", "eastus")
    family = getattr(args, "family", "Standard DDSv4 Family")
    target = getattr(args, "target", 8)
    interval = getattr(args, "interval", 60)
    timeout = getattr(args, "timeout", 86400)  # 24 hours default
    auto_run = getattr(args, "auto_run", False)
    quiet = getattr(args, "quiet", False)

    log("QUOTA-WAIT", "=" * 60)
    log("QUOTA-WAIT", "AZURE QUOTA WAIT")
    log("QUOTA-WAIT", "=" * 60)
    log("QUOTA-WAIT", "")
    log("QUOTA-WAIT", f"VM Family:    {family}")
    log("QUOTA-WAIT", f"Target vCPUs: {target}")
    log("QUOTA-WAIT", f"Location:     {location}")
    log("QUOTA-WAIT", f"Interval:     {interval}s")
    log("QUOTA-WAIT", f"Timeout:      {timeout / 3600:.1f}h")
    log("QUOTA-WAIT", f"Auto-run:     {auto_run}")
    log("QUOTA-WAIT", "")

    start_time = time.time()
    check_count = 0

    while True:
        check_count += 1
        elapsed = time.time() - start_time

        # Check timeout
        if elapsed > timeout:
            log("QUOTA-WAIT", f"Timeout after {elapsed / 3600:.1f} hours")
            log("QUOTA-WAIT", "Quota was not approved in time.")
            return 1

        # Check quota status
        status = get_quota_status(location, family, target)

        if status.get("error"):
            log("QUOTA-WAIT", f"Error checking quota: {status['error']}")
            log("QUOTA-WAIT", "Will retry...")
        elif status["sufficient"]:
            log("QUOTA-WAIT", "")
            log("QUOTA-WAIT", "=" * 60)
            log("QUOTA-WAIT", "QUOTA APPROVED!")
            log("QUOTA-WAIT", "=" * 60)
            log("QUOTA-WAIT", f"Family: {status['family']}")
            log("QUOTA-WAIT", f"Limit:  {status['limit']} vCPUs (target was {target})")
            log(
                "QUOTA-WAIT",
                f"Waited: {elapsed / 60:.1f} minutes ({check_count} checks)",
            )
            log("QUOTA-WAIT", "")

            if auto_run:
                log("QUOTA-WAIT", "Starting evaluation automatically...")
                log("QUOTA-WAIT", "")
                return cmd_run_azure_ml_auto(args)

            log("QUOTA-WAIT", "You can now run:")
            log(
                "QUOTA-WAIT",
                "  uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-auto",
            )
            return 0
        else:
            # Quota not sufficient yet
            if not quiet:
                remaining = timeout - elapsed
                log(
                    "QUOTA-WAIT",
                    f"Check #{check_count}: {status.get('limit', 0)}/{target} vCPUs "
                    f"(elapsed: {elapsed / 60:.0f}m, remaining: {remaining / 3600:.1f}h)",
                )

        # Wait before next check
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("QUOTA-WAIT", "")
            log("QUOTA-WAIT", "Interrupted by user (Ctrl+C)")
            log(
                "QUOTA-WAIT",
                f"Waited {elapsed / 60:.1f} minutes ({check_count} checks)",
            )
            return 130  # Standard exit code for Ctrl+C


def find_best_region_for_vm(
    vm_size: str,
    min_vcpus: int = 8,
    preferred_regions: list = None,
    check_ml_quota: bool = True,
) -> dict:
    """Find the best region for a VM size based on availability and quota.

    IMPORTANT: For Azure ML, we need to check ML Dedicated quota, not VM quota!
    - VM quota: For regular Azure VMs (az vm list-usage)
    - ML Dedicated quota: For Azure ML compute instances (REST API)

    Args:
        vm_size: VM size to search for (e.g., "Standard_D8ds_v4")
        min_vcpus: Minimum vCPUs required (default: 8 for D8ds_v4)
        preferred_regions: List of regions to check (default: US regions)
        check_ml_quota: If True, check ML Dedicated quota; if False, check VM quota

    Returns dict with:
        - region: Best region found (or None)
        - vm_size: VM size requested
        - quota: Current quota in that region
        - ml_quota: ML Dedicated quota (if check_ml_quota=True)
        - available: List of all available regions
        - error: Error message if any
        - warning: Warning message (e.g., ARM VM selected)
    """
    from openadapt_evals.config import settings

    if preferred_regions is None:
        # Check these regions in order of preference
        # Central US first since that's where our workspace is
        preferred_regions = [
            "centralus",
            "eastus",
            "eastus2",
            "westus2",
            "westus",
            "northcentralus",
            "southcentralus",
            "westeurope",
            "northeurope",
        ]

    # Check for ARM VM warning
    warning = None
    if is_arm_vm_size(vm_size):
        warning = f"WARNING: {vm_size} is ARM-based and won't run x86 Docker images (WAA needs x86)"

    # Map VM sizes to quota families
    vm_to_family = {
        "Standard_D8ds_v4": "Standard DDSv4 Family",
        "Standard_D8ds_v5": "Standard DDSv5 Family",
        "Standard_D8pds_v5": "Standard DPDSv5 Family",
        "Standard_D4ds_v4": "Standard DDSv4 Family",
        "Standard_D4ds_v5": "Standard DDSv5 Family",
    }

    family_name = vm_to_family.get(vm_size, f"Standard {vm_size.split('_')[1][0]}* Family")
    available_regions = []
    subscription_id = settings.azure_subscription_id

    for region in preferred_regions:
        try:
            # Check if VM is available (no restrictions)
            result = subprocess.run(
                [
                    "az",
                    "vm",
                    "list-skus",
                    "--location",
                    region,
                    "--resource-type",
                    "virtualMachines",
                    "--query",
                    f"[?name=='{vm_size}'].restrictions[0].reasonCode",
                    "-o",
                    "tsv",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            restriction = result.stdout.strip()

            if restriction and "NotAvailable" in restriction:
                continue  # VM restricted in this region

            # Check appropriate quota type
            vm_quota = 0
            ml_dedicated_quota = 0

            # Always check VM quota for reference
            quota_result = subprocess.run(
                [
                    "az",
                    "vm",
                    "list-usage",
                    "--location",
                    region,
                    "--query",
                    f"[?contains(localName, '{family_name.split()[1]}')].limit | [0]",
                    "-o",
                    "tsv",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            vm_quota = int(quota_result.stdout.strip() or 0)

            # Check ML Dedicated quota if requested
            if check_ml_quota:
                ml_result = get_azure_ml_dedicated_quota(subscription_id, region)
                if not ml_result.get("error"):
                    ml_dedicated_quota = ml_result.get("available", 0)

            # Use ML quota for sufficiency check if checking ML, otherwise use VM quota
            effective_quota = ml_dedicated_quota if check_ml_quota else vm_quota

            available_regions.append(
                {
                    "region": region,
                    "restriction": restriction or "None",
                    "vm_quota": vm_quota,
                    "ml_quota": ml_dedicated_quota,
                    "quota": effective_quota,  # For backward compatibility
                    "sufficient": effective_quota >= min_vcpus,
                }
            )

            # If we found a region with sufficient quota and no restrictions, use it
            if not restriction and effective_quota >= min_vcpus:
                return {
                    "region": region,
                    "vm_size": vm_size,
                    "quota": effective_quota,
                    "vm_quota": vm_quota,
                    "ml_quota": ml_dedicated_quota,
                    "family": family_name,
                    "available": available_regions,
                    "error": None,
                    "warning": warning,
                }

        except Exception:
            continue

    # No ideal region found - return best available
    if available_regions:
        # Sort by: sufficient quota first, then highest quota
        available_regions.sort(key=lambda x: (x["sufficient"], x["quota"]), reverse=True)
        best = available_regions[0]

        quota_type = "ML Dedicated" if check_ml_quota else "VM"
        error_msg = (
            f"Best region {best['region']} has {quota_type} quota {best['quota']} < {min_vcpus}"
        )

        return {
            "region": best["region"],
            "vm_size": vm_size,
            "quota": best["quota"],
            "vm_quota": best.get("vm_quota", 0),
            "ml_quota": best.get("ml_quota", 0),
            "family": family_name,
            "available": available_regions,
            "error": None if best["sufficient"] else error_msg,
            "warning": warning,
        }

    return {
        "region": None,
        "vm_size": vm_size,
        "quota": 0,
        "vm_quota": 0,
        "ml_quota": 0,
        "family": family_name,
        "available": [],
        "error": f"No available regions found for {vm_size}",
        "warning": warning,
    }


def cmd_azure_ml_find_region(args):
    """Find best region for running Azure ML WAA evaluation.

    IMPORTANT: This checks Azure ML Dedicated quota, not VM quota!
    Compute instances require "Dedicated" quota from the ML workspace.
    """
    init_logging()

    vm_size = getattr(args, "vm_size", "Standard_D8ds_v4")
    min_vcpus = getattr(args, "vcpus", 8)
    check_ml = not getattr(args, "vm_quota", False)  # Default to ML quota

    log("FIND-REGION", "=" * 60)
    log("FIND-REGION", "FINDING BEST REGION FOR AZURE ML")
    log("FIND-REGION", "=" * 60)
    log("FIND-REGION", "")
    log("FIND-REGION", f"VM Size:      {vm_size}")
    log("FIND-REGION", f"Min vCPUs:    {min_vcpus}")
    log("FIND-REGION", f"Quota Type:   {'ML Dedicated' if check_ml else 'VM'}")
    log("FIND-REGION", "")

    # Warn if ARM VM selected
    if is_arm_vm_size(vm_size):
        log("FIND-REGION", "!!! WARNING !!!")
        log("FIND-REGION", f"{vm_size} is ARM-based and WILL NOT run x86 Docker images!")
        log(
            "FIND-REGION",
            "WAA requires x86 architecture. Use D8ds_v4 or D8ds_v5 instead.",
        )
        log("FIND-REGION", "")

    log("FIND-REGION", "Scanning regions (checking ML Dedicated quota)...")
    log("FIND-REGION", "")

    result = find_best_region_for_vm(vm_size, min_vcpus, check_ml_quota=check_ml)

    log("FIND-REGION", "RESULTS:")
    log("FIND-REGION", "-" * 70)
    log(
        "FIND-REGION",
        f"{'Region':<20} {'VM Quota':>10} {'ML Quota':>10} {'Status':<15}",
    )
    log("FIND-REGION", "-" * 70)

    for r in result.get("available", []):
        if r["restriction"] != "None":
            status = "RESTRICTED"
        elif r.get("ml_quota", 0) >= min_vcpus if check_ml else r.get("vm_quota", 0) >= min_vcpus:
            status = "OK"
        else:
            status = "LOW QUOTA"

        vm_q = r.get("vm_quota", r.get("quota", 0))
        ml_q = r.get("ml_quota", 0)
        log("FIND-REGION", f"  {r['region']:<20} {vm_q:>10} {ml_q:>10} [{status}]")

    log("FIND-REGION", "")

    if result["error"]:
        log("FIND-REGION", f"ERROR: {result['error']}")
        log("FIND-REGION", "")
        log("FIND-REGION", "To request quota in the best available region:")
        if result["region"]:
            log(
                "FIND-REGION",
                f"  uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-quota-request --location {result['region']}",
            )
        return 1

    log("FIND-REGION", f"BEST REGION: {result['region']}")
    log("FIND-REGION", f"  VM:     {result['vm_size']}")
    log("FIND-REGION", f"  Quota:  {result['quota']} vCPUs")
    log("FIND-REGION", f"  Family: {result['family']}")
    log("FIND-REGION", "")
    log("FIND-REGION", "To run evaluation in this region:")
    log(
        "FIND-REGION",
        f"  uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-auto --location {result['region']}",
    )

    return 0


def cmd_azure_ml_status(args):
    """Show status of Azure ML jobs and compute instances."""
    init_logging()

    try:
        from azure.ai.ml import MLClient
        from azure.identity import DefaultAzureCredential

        from openadapt_evals.config import settings
    except ImportError:
        log("AZURE-ML", "ERROR: Azure ML SDK not installed. Run: uv add azure-ai-ml")
        return 1

    ml_client = MLClient(
        DefaultAzureCredential(),
        settings.azure_subscription_id,
        settings.azure_ml_resource_group,
        settings.azure_ml_workspace_name,
    )

    log("AZURE-ML", "=" * 60)
    log("AZURE-ML", "AZURE ML STATUS")
    log("AZURE-ML", "=" * 60)

    # List compute instances
    log("AZURE-ML", "")
    log("AZURE-ML", "COMPUTE INSTANCES:")
    computes = list(ml_client.compute.list())
    waa_computes = [c for c in computes if c.name.startswith("w") and "Exp" in c.name]

    if not waa_computes:
        log("AZURE-ML", "  No WAA compute instances found")
    else:
        for c in waa_computes:
            log("AZURE-ML", f"  {c.name}: {c.state} ({c.size})")

    # List recent jobs
    log("AZURE-ML", "")
    log("AZURE-ML", "RECENT JOBS (last 10):")
    jobs = list(ml_client.jobs.list(max_results=10))

    if not jobs:
        log("AZURE-ML", "  No jobs found")
    else:
        for job in jobs:
            created = (
                job.creation_context.created_at.strftime("%m-%d %H:%M")
                if job.creation_context
                else "?"
            )
            log("AZURE-ML", f"  {job.name}: {job.status} ({created})")

    return 0


def cmd_azure_ml_vnc(args):
    """Set up VNC tunnel to Azure ML compute instance."""
    init_logging()

    try:
        from azure.ai.ml import MLClient
        from azure.identity import DefaultAzureCredential

        from openadapt_evals.config import settings
    except ImportError:
        log("AZURE-ML", "ERROR: Azure ML SDK not installed")
        return 1

    compute_name = args.compute
    local_port = getattr(args, "port", 8007)

    ml_client = MLClient(
        DefaultAzureCredential(),
        settings.azure_subscription_id,
        settings.azure_ml_resource_group,
        settings.azure_ml_workspace_name,
    )

    # Get workspace region
    workspace = ml_client.workspaces.get(settings.azure_ml_workspace_name)
    workspace_region = workspace.location if workspace else "centralus"
    log("AZURE-ML", f"Workspace region: {workspace_region}")

    if not compute_name:
        # Auto-detect running compute instance
        computes = list(ml_client.compute.list())
        running = [c for c in computes if c.state == "Running" and c.name.startswith("w")]

        if not running:
            log("AZURE-ML", "No running compute instances found")
            log("AZURE-ML", "Use --compute NAME to specify one")
            return 1

        compute_name = running[0].name
        log("AZURE-ML", f"Auto-detected compute: {compute_name}")

    # Build WebSocket URL for compute instance (use workspace region)
    compute_url = f"wss://{compute_name.lower()}.{workspace_region}.instances.azureml.ms"

    # Find Azure CLI Python path for the proxy script
    az_cli_paths = [
        "/opt/homebrew/Cellar/azure-cli/2.70.0/libexec/bin/python",
        "/opt/homebrew/Cellar/azure-cli/2.69.0/libexec/bin/python",
        "/usr/local/Cellar/azure-cli/2.70.0/libexec/bin/python",
    ]
    az_python = None
    for p in az_cli_paths:
        if Path(p).exists():
            az_python = p
            break

    if not az_python:
        log("AZURE-ML", "ERROR: Azure CLI Python not found. Install Azure CLI.")
        return 1

    proxy_script = (
        Path.home()
        / ".azure"
        / "cliextensions"
        / "ml"
        / "azext_mlv2"
        / "manual"
        / "custom"
        / "_ssh_connector.py"
    )
    if not proxy_script.exists():
        log("AZURE-ML", f"ERROR: SSH connector not found at {proxy_script}")
        log("AZURE-ML", "Install Azure ML CLI extension: az extension add -n ml")
        return 1

    # Kill existing tunnels on the port
    subprocess.run(["pkill", "-f", f"{local_port}:localhost:8006"], capture_output=True)

    # Start SSH tunnel
    proxy_cmd = f"{az_python} {proxy_script} {compute_url} --is-compute"
    ssh_cmd = [
        "ssh",
        "-L",
        f"{local_port}:localhost:8006",
        "-N",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"ProxyCommand={proxy_cmd}",
        f"azureuser@{compute_url}",
    ]

    log(
        "AZURE-ML",
        f"Starting VNC tunnel: localhost:{local_port} -> {compute_name}:8006",
    )

    tunnel_proc = subprocess.Popen(
        ssh_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(3)

    if tunnel_proc.poll() is not None:
        log("AZURE-ML", "ERROR: Tunnel failed to start")
        return 1

    log("AZURE-ML", f"VNC tunnel started (PID: {tunnel_proc.pid})")
    log("AZURE-ML", f"Access VNC at: http://localhost:{local_port}")

    if getattr(args, "open", False):
        webbrowser.open(f"http://localhost:{local_port}")

    if getattr(args, "wait", False):
        log("AZURE-ML", "Press Ctrl+C to stop tunnel...")
        try:
            tunnel_proc.wait()
        except KeyboardInterrupt:
            tunnel_proc.terminate()
            log("AZURE-ML", "Tunnel stopped")

    return 0


def cmd_azure_ml_monitor(args):
    """Monitor Azure ML jobs with auto VNC setup."""
    init_logging()

    try:
        from datetime import datetime, timezone

        from azure.ai.ml import MLClient
        from azure.identity import DefaultAzureCredential

        from openadapt_evals.config import settings
    except ImportError:
        log("AZURE-ML", "ERROR: Azure ML SDK not installed")
        return 1

    ml_client = MLClient(
        DefaultAzureCredential(),
        settings.azure_subscription_id,
        settings.azure_ml_resource_group,
        settings.azure_ml_workspace_name,
    )

    job_name = getattr(args, "job", None)
    poll_interval = getattr(args, "interval", 30)
    auto_vnc = getattr(args, "vnc", True)

    # Find the job to monitor
    if not job_name:
        # Get most recent running job
        jobs = list(ml_client.jobs.list(max_results=5))
        running_jobs = [j for j in jobs if j.status in ["Running", "Queued", "Starting"]]

        if not running_jobs:
            log("AZURE-ML", "No running jobs found")
            # Show recent jobs
            if jobs:
                log("AZURE-ML", "")
                log("AZURE-ML", "Recent jobs:")
                for j in jobs[:5]:
                    log("AZURE-ML", f"  {j.name}: {j.status}")
            return 1

        job_name = running_jobs[0].name
        log("AZURE-ML", f"Monitoring job: {job_name}")

    # Set up VNC if requested
    vnc_port = 8007

    if auto_vnc:
        # Find compute instance for this job
        job = ml_client.jobs.get(job_name)
        compute_name = job.compute if hasattr(job, "compute") else None

        if compute_name:
            # Set up VNC tunnel (reuse cmd_azure_ml_vnc logic)
            class VncArgs:
                compute = compute_name
                port = vnc_port
                open = True
                wait = False

            cmd_azure_ml_vnc(VncArgs())
            log("AZURE-ML", f"VNC available at: http://localhost:{vnc_port}")

    # Monitor loop
    log("AZURE-ML", "")
    log("AZURE-ML", f"Monitoring job: {job_name}")
    log("AZURE-ML", f"Poll interval: {poll_interval}s")
    log("AZURE-ML", "Press Ctrl+C to stop monitoring")
    log("AZURE-ML", "")

    try:
        while True:
            job = ml_client.jobs.get(job_name)
            now = datetime.now(timezone.utc)
            created = job.creation_context.created_at if job.creation_context else now
            elapsed = (now - created).total_seconds() / 60

            log(
                "AZURE-ML",
                f"[{datetime.now().strftime('%H:%M:%S')}] Status: {job.status}, Elapsed: {elapsed:.0f} min",
            )

            if job.status in ["Completed", "Failed", "Canceled"]:
                log("AZURE-ML", "")
                log("AZURE-ML", f"Job finished: {job.status}")
                if job.status == "Failed" and hasattr(job, "error") and job.error:
                    log("AZURE-ML", f"Error: {job.error}")
                break

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        log("AZURE-ML", "")
        log("AZURE-ML", "Monitoring stopped")

    return 0


def cmd_azure_ml_logs(args):
    """Stream logs from Azure ML job in real-time.

    Downloads and tails user_logs/std_log.txt from Azure blob storage.
    This provides actual stdout from the job container.
    """
    init_logging()
    import tempfile
    import time

    from openadapt_evals.config import settings

    job_name = getattr(args, "job", None)
    follow = getattr(args, "follow", True)
    poll_interval = getattr(args, "interval", 5)

    # If no job specified, find the most recent one
    if not job_name:
        log("AZURE-ML-LOGS", "Finding most recent job...")
        result = subprocess.run(
            [
                "az",
                "ml",
                "job",
                "list",
                "-g",
                settings.azure_ml_resource_group,
                "-w",
                settings.azure_ml_workspace_name,
                "--query",
                "[0].name",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            log("AZURE-ML-LOGS", "ERROR: No jobs found or failed to list jobs")
            return 1
        job_name = result.stdout.strip()

    log("AZURE-ML-LOGS", f"Streaming logs for job: {job_name}")

    # Get job status and Web View URL
    result = subprocess.run(
        [
            "az",
            "ml",
            "job",
            "show",
            "--name",
            job_name,
            "-g",
            settings.azure_ml_resource_group,
            "-w",
            settings.azure_ml_workspace_name,
            "--query",
            "{status:status,url:services.Studio.endpoint}",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        import json

        try:
            job_info = json.loads(result.stdout)
            log("AZURE-ML-LOGS", f"Status: {job_info.get('status', 'Unknown')}")
            if job_info.get("url"):
                log("AZURE-ML-LOGS", f"Web View: {job_info['url']}")
        except json.JSONDecodeError:
            pass

    # Get storage account name from datastore
    result = subprocess.run(
        [
            "az",
            "ml",
            "datastore",
            "show",
            "--name",
            "workspaceartifactstore",
            "-g",
            settings.azure_ml_resource_group,
            "-w",
            settings.azure_ml_workspace_name,
            "--query",
            "account_name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        log("AZURE-ML-LOGS", "ERROR: Could not get storage account name")
        return 1
    storage_account = result.stdout.strip()

    # Get storage account key
    result = subprocess.run(
        [
            "az",
            "storage",
            "account",
            "keys",
            "list",
            "--account-name",
            storage_account,
            "-g",
            settings.azure_ml_resource_group,
            "--query",
            "[0].value",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        log("AZURE-ML-LOGS", "ERROR: Could not get storage account key")
        return 1
    account_key = result.stdout.strip()

    # Blob path for stdout logs
    blob_name = f"ExperimentRun/dcid.{job_name}/user_logs/std_log.txt"
    container_name = "azureml"

    if follow:
        log("AZURE-ML-LOGS", f"Polling every {poll_interval}s (Ctrl+C to stop)")
    log("AZURE-ML-LOGS", "")
    log("AZURE-ML-LOGS", "=" * 60)

    last_size = 0

    try:
        while True:
            # Download blob to temp file
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt") as f:
                temp_file = f.name

            result = subprocess.run(
                [
                    "az",
                    "storage",
                    "blob",
                    "download",
                    "--account-name",
                    storage_account,
                    "--container-name",
                    container_name,
                    "--name",
                    blob_name,
                    "--account-key",
                    account_key,
                    "--file",
                    temp_file,
                    "--no-progress",
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                # Read and print new content
                try:
                    with open(temp_file, "r") as f:
                        content = f.read()
                        if len(content) > last_size:
                            # Print only new content
                            new_content = content[last_size:]
                            print(new_content, end="", flush=True)
                            last_size = len(content)
                except Exception as e:
                    log("AZURE-ML-LOGS", f"Error reading logs: {e}")

            # Clean up temp file
            try:
                os.unlink(temp_file)
            except Exception:
                pass

            if not follow:
                break

            # Check if job is still running
            result = subprocess.run(
                [
                    "az",
                    "ml",
                    "job",
                    "show",
                    "--name",
                    job_name,
                    "-g",
                    settings.azure_ml_resource_group,
                    "-w",
                    settings.azure_ml_workspace_name,
                    "--query",
                    "status",
                    "-o",
                    "tsv",
                ],
                capture_output=True,
                text=True,
            )
            status = result.stdout.strip() if result.returncode == 0 else "Unknown"
            if status in ["Completed", "Failed", "Canceled"]:
                log("AZURE-ML-LOGS", "")
                log("AZURE-ML-LOGS", f"Job {status}")
                break

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        log("AZURE-ML-LOGS", "")
        log("AZURE-ML-LOGS", "Streaming stopped")

    return 0


def cmd_azure_ml_stream_logs(args):
    """Stream logs from Azure ML job using Azure Storage SDK.

    This command uses the Azure Storage Python SDK with account key authentication
    to fetch logs written to ./logs/ by run_entry.py. Works for RUNNING jobs.

    Files fetched from ExperimentRun/dcid.{job_name}/logs/:
    - job.log - Plain text log (human-readable)
    - events.jsonl - Structured events (JSON lines)
    - progress.json - Current progress state

    Usage:
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-stream --job JOB_NAME
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-stream  # Most recent job
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-stream --follow  # Real-time streaming
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-stream --progress  # Show progress only
    """
    init_logging()

    from openadapt_evals.config import settings

    job_name = getattr(args, "job", None)
    follow = getattr(args, "follow", True)
    poll_interval = getattr(args, "interval", 5)
    show_progress = getattr(args, "progress", False)
    show_events = getattr(args, "events", False)
    auto_teardown = getattr(args, "auto_teardown", False)

    # Get Azure credentials
    subscription_id = settings.azure_subscription_id
    resource_group = settings.azure_ml_resource_group
    workspace_name = settings.azure_ml_workspace_name

    if not all([subscription_id, resource_group, workspace_name]):
        log("STREAM", "ERROR: Missing Azure ML configuration in .env")
        log(
            "STREAM",
            "Required: AZURE_SUBSCRIPTION_ID, AZURE_ML_RESOURCE_GROUP, AZURE_ML_WORKSPACE_NAME",
        )
        return 1

    # Initialize Azure ML client for job info
    try:
        from azure.ai.ml import MLClient
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.storage import StorageManagementClient
        from azure.storage.blob import BlobServiceClient

        credential = DefaultAzureCredential()
        ml_client = MLClient(
            credential=credential,
            subscription_id=subscription_id,
            resource_group_name=resource_group,
            workspace_name=workspace_name,
        )
    except ImportError as e:
        log("STREAM", f"ERROR: Missing SDK: {e}")
        log(
            "STREAM",
            "Install with: pip install azure-ai-ml azure-identity azure-mgmt-storage azure-storage-blob",
        )
        return 1
    except Exception as e:
        log("STREAM", f"ERROR: Failed to initialize Azure ML client: {e}")
        return 1

    # Get storage account name from workspace
    try:
        ws = ml_client.workspaces.get(workspace_name)
        # Storage account is a full resource ID, extract the name
        storage_account = ws.storage_account.split("/")[-1]
        container_name = "azureml"  # Default artifact container
        log("STREAM", f"Storage account: {storage_account}")
    except Exception as e:
        log("STREAM", f"ERROR: Failed to get workspace storage: {e}")
        return 1

    # Get storage account key for blob access
    try:
        storage_client = StorageManagementClient(credential, subscription_id)
        keys = storage_client.storage_accounts.list_keys(resource_group, storage_account)
        storage_key = keys.keys[0].value
        blob_service = BlobServiceClient(
            f"https://{storage_account}.blob.core.windows.net", credential=storage_key
        )
        container_client = blob_service.get_container_client(container_name)
        log("STREAM", "Blob storage connected")
    except Exception as e:
        log("STREAM", f"ERROR: Failed to get storage account key: {e}")
        return 1

    # If no job specified, find the most recent one
    if not job_name:
        log("STREAM", "Finding most recent job...")
        try:
            jobs = list(ml_client.jobs.list(max_results=10))
            if not jobs:
                log("STREAM", "ERROR: No jobs found")
                return 1
            job_name = jobs[0].name
            log("STREAM", f"Using most recent job: {job_name}")
        except Exception as e:
            log("STREAM", f"ERROR: Failed to list jobs: {e}")
            return 1

    # Get job info
    try:
        job = ml_client.jobs.get(job_name)
        log("STREAM", f"Job: {job_name}")
        log("STREAM", f"Status: {job.status}")
        if hasattr(job, "services") and job.services and "Studio" in job.services:
            log("STREAM", f"Web View: {job.services['Studio'].endpoint}")
    except Exception as e:
        log("STREAM", f"ERROR: Failed to get job info: {e}")
        return 1

    log("STREAM", "")
    log("STREAM", "=" * 60)
    if follow:
        log("STREAM", f"Streaming logs (polling every {poll_interval}s, Ctrl+C to stop)")
    log("STREAM", "=" * 60)
    log("STREAM", "")

    # Helper function to download a blob using Python SDK
    def download_blob(blob_name: str, local_path: str) -> bool:
        """Download a blob using Azure Storage SDK with account key auth."""
        try:
            blob_client = container_client.get_blob_client(blob_name)
            with open(local_path, "wb") as f:
                data = blob_client.download_blob()
                f.write(data.readall())
            return True
        except Exception:
            return False

    # Track what we've already shown
    last_log_size = 0
    last_event_count = 0
    last_progress = None
    blob_prefix = f"ExperimentRun/dcid.{job_name}/logs"

    try:
        while True:
            # Create temp directory for downloaded logs
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)

                # Download log files using az storage CLI
                log_file = temp_path / "job.log"
                progress_file = temp_path / "progress.json"
                events_file = temp_path / "events.jsonl"

                # Download each file (suppress warnings)
                job_log_ok = download_blob(f"{blob_prefix}/job.log", str(log_file))
                progress_ok = download_blob(f"{blob_prefix}/progress.json", str(progress_file))
                events_ok = download_blob(f"{blob_prefix}/events.jsonl", str(events_file))

                # Show progress if requested
                if show_progress and progress_ok and progress_file.exists():
                    try:
                        with open(progress_file) as f:
                            progress = json.load(f)
                        if progress != last_progress:
                            # Progress bar
                            pct = progress.get("percent", 0)
                            filled = int(pct / 2)
                            bar = "=" * filled + "-" * (50 - filled)
                            log("PROGRESS", f"[{bar}] {pct}%")
                            log("PROGRESS", f"Phase: {progress.get('phase', 'unknown')}")
                            log(
                                "PROGRESS",
                                f"Status: {progress.get('status', 'unknown')}",
                            )
                            if progress.get("messages"):
                                log(
                                    "PROGRESS",
                                    f"Last: {progress['messages'][-1].get('text', '')}",
                                )
                            last_progress = progress.copy()
                            log("STREAM", "")
                    except Exception:
                        pass  # Progress file may be partially written

                # Show events if requested
                if show_events and events_ok and events_file.exists():
                    try:
                        with open(events_file) as f:
                            lines = f.readlines()
                        new_events = lines[last_event_count:]
                        for line in new_events:
                            try:
                                event = json.loads(line.strip())
                                log(
                                    "EVENT",
                                    f"{event['type']}: {json.dumps(event.get('data', {}))}",
                                )
                            except Exception:
                                pass
                        last_event_count = len(lines)
                    except Exception:
                        pass

                # Show log content (default)
                if job_log_ok and log_file.exists() and not (show_progress and not show_events):
                    try:
                        with open(log_file) as f:
                            content = f.read()
                        if len(content) > last_log_size:
                            # Print only new content
                            new_content = content[last_log_size:]
                            print(new_content, end="", flush=True)
                            last_log_size = len(content)
                    except Exception:
                        pass

                # If no logs available yet
                if not job_log_ok and not progress_ok:
                    log("STREAM", "Waiting for logs to appear...")

            # Check if job is still running
            try:
                job = ml_client.jobs.get(job_name)
                status = job.status
            except Exception:
                status = "Unknown"

            if not follow:
                break

            if status in ["Completed", "Failed", "Canceled"]:
                log("STREAM", "")
                log("STREAM", f"Job {status}")

                # Auto-teardown if requested
                if auto_teardown:
                    log("STREAM", "")
                    log(
                        "STREAM",
                        "Auto-teardown enabled, cleaning up compute instances...",
                    )
                    # Use the teardown command with force flag
                    teardown_args = type(
                        "Args", (), {"force": True, "delete_resource_group": False}
                    )()
                    cmd_azure_ml_teardown(teardown_args)

                break

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        log("STREAM", "")
        log("STREAM", "Streaming stopped")

    return 0


def cmd_azure_ml_progress(args):
    """Show current progress of an Azure ML job.

    This fetches the progress.json file written by run_entry.py and displays
    a summary of the job's current state.

    Usage:
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-progress --job JOB_NAME
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-progress  # Most recent job
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-progress --watch  # Poll continuously
    """
    init_logging()

    from openadapt_evals.config import settings

    job_name = getattr(args, "job", None)
    watch = getattr(args, "watch", False)
    poll_interval = getattr(args, "interval", 10)

    # Get Azure credentials
    subscription_id = settings.azure_subscription_id
    resource_group = settings.azure_ml_resource_group
    workspace_name = settings.azure_ml_workspace_name

    if not all([subscription_id, resource_group, workspace_name]):
        log("PROGRESS", "ERROR: Missing Azure ML configuration in .env")
        return 1

    # Initialize Azure ML client
    try:
        from azure.ai.ml import MLClient
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        ml_client = MLClient(
            credential=credential,
            subscription_id=subscription_id,
            resource_group_name=resource_group,
            workspace_name=workspace_name,
        )
    except ImportError:
        log("PROGRESS", "ERROR: Azure ML SDK not installed")
        return 1
    except Exception as e:
        log("PROGRESS", f"ERROR: Failed to initialize Azure ML client: {e}")
        return 1

    # If no job specified, find the most recent one
    if not job_name:
        try:
            jobs = list(ml_client.jobs.list(max_results=5))
            if not jobs:
                log("PROGRESS", "ERROR: No jobs found")
                return 1
            job_name = jobs[0].name
        except Exception as e:
            log("PROGRESS", f"ERROR: Failed to list jobs: {e}")
            return 1

    def show_progress():
        """Fetch and display progress."""
        try:
            job = ml_client.jobs.get(job_name)

            # Clear screen for watch mode
            if watch:
                print("\033[H\033[J", end="")

            print("=" * 60)
            print(f"Job: {job_name}")
            print(f"Status: {job.status}")
            print("=" * 60)

            # Try to get progress.json from job outputs
            # This requires the job to have written to ./logs/progress.json
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    ml_client.jobs.download(
                        name=job_name,
                        download_path=temp_dir,
                        all=True,
                    )

                    # Find progress.json
                    for root, dirs, files in os.walk(temp_dir):
                        if "progress.json" in files:
                            with open(os.path.join(root, "progress.json")) as f:
                                progress = json.load(f)

                            print(f"\nPhase: {progress.get('phase', 'unknown')}")
                            print(f"Progress: {progress.get('percent', 0)}%")

                            # Progress bar
                            pct = progress.get("percent", 0)
                            filled = int(pct / 2)
                            bar = "=" * filled + "-" * (50 - filled)
                            print(f"[{bar}] {pct}%")

                            print(f"Status: {progress.get('status', 'unknown')}")
                            print(f"Last Update: {progress.get('last_update', 'N/A')}")

                            if progress.get("messages"):
                                print("\nRecent Messages:")
                                for msg in progress["messages"][-5:]:
                                    print(f"  {msg.get('time', '')} - {msg.get('text', '')}")
                            return
                except Exception:
                    # If can't download, just show job status
                    pass

            print("\nProgress file not available yet.")
            print("(Job may still be initializing or progress.json not written)")

        except Exception as e:
            log("PROGRESS", f"Error: {e}")

    try:
        if watch:
            log("PROGRESS", "Watching progress (Ctrl+C to stop)")
            while True:
                show_progress()

                # Check if job finished
                try:
                    job = ml_client.jobs.get(job_name)
                    if job.status in ["Completed", "Failed", "Canceled"]:
                        print(f"\nJob {job.status}")
                        break
                except Exception:
                    pass

                time.sleep(poll_interval)
        else:
            show_progress()
    except KeyboardInterrupt:
        print("\nStopped")

    return 0


def cmd_azure_ml_cancel(args):
    """Cancel a running Azure ML job.

    Usage:
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-cancel --job JOB_NAME
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-cancel  # Cancels most recent running job
    """
    init_logging()
    from openadapt_evals.config import settings

    job_name = getattr(args, "job", None)

    # If no job specified, find the most recent running one
    if not job_name:
        log("AZURE-ML", "Finding most recent running job...")
        result = subprocess.run(
            [
                "az",
                "ml",
                "job",
                "list",
                "-g",
                settings.azure_ml_resource_group,
                "-w",
                settings.azure_ml_workspace_name,
                "--query",
                "[?status=='Running'].name",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            log("AZURE-ML", "No running jobs found")
            return 0
        job_name = result.stdout.strip().split("\n")[0]
        log("AZURE-ML", f"Found running job: {job_name}")

    # Cancel the job
    log("AZURE-ML", f"Canceling job: {job_name}")
    result = subprocess.run(
        [
            "az",
            "ml",
            "job",
            "cancel",
            "--name",
            job_name,
            "-g",
            settings.azure_ml_resource_group,
            "-w",
            settings.azure_ml_workspace_name,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        log("AZURE-ML", f"Job {job_name} canceled successfully")
    else:
        log("AZURE-ML", f"Failed to cancel job: {result.stderr}")
        return 1

    return 0


def cmd_azure_ml_delete_compute(args):
    """Delete an Azure ML compute instance.

    Usage:
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-delete-compute --name COMPUTE_NAME
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-delete-compute --all  # Deletes all instances
    """
    init_logging()

    compute_name = getattr(args, "name", None)
    delete_all = getattr(args, "all", False)
    confirm = getattr(args, "yes", False)

    if delete_all:
        # List all compute instances
        instances = list_azure_ml_compute_instances()
        if not instances:
            log("AZURE-ML", "No compute instances found")
            return 0

        log("AZURE-ML", f"Found {len(instances)} compute instance(s):")
        for inst in instances:
            log("AZURE-ML", f"  - {inst['name']} ({inst['state']})")

        if not confirm:
            log("AZURE-ML", "")
            log("AZURE-ML", "Use --yes to confirm deletion of all instances")
            return 1

        # Delete all instances
        for inst in instances:
            log("AZURE-ML", f"Deleting {inst['name']}...")
            if delete_azure_ml_compute_instance(inst["name"]):
                log("AZURE-ML", f"  Deleted {inst['name']}")
            else:
                log("AZURE-ML", f"  Failed to delete {inst['name']}")

        return 0

    if not compute_name:
        # Find compute instances
        instances = list_azure_ml_compute_instances()
        if not instances:
            log("AZURE-ML", "No compute instances found")
            return 0

        if len(instances) == 1:
            compute_name = instances[0]["name"]
            log("AZURE-ML", f"Found single compute instance: {compute_name}")
        else:
            log("AZURE-ML", f"Found {len(instances)} compute instances:")
            for inst in instances:
                log("AZURE-ML", f"  - {inst['name']} ({inst['state']})")
            log("AZURE-ML", "")
            log("AZURE-ML", "Specify --name or --all")
            return 1

    if not confirm:
        log("AZURE-ML", f"Will delete compute instance: {compute_name}")
        log("AZURE-ML", "Use --yes to confirm")
        return 1

    # Delete the compute instance
    log("AZURE-ML", f"Deleting compute instance: {compute_name}")
    if delete_azure_ml_compute_instance(compute_name):
        log("AZURE-ML", f"Compute instance {compute_name} deleted successfully")
    else:
        log("AZURE-ML", f"Failed to delete compute instance {compute_name}")
        return 1

    return 0


def cmd_azure_ml_cleanup(args):
    """Clean up Azure ML resources (cancel jobs + delete compute instances).

    This is a convenience command that combines cancel + delete.

    Usage:
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-cleanup --yes
    """
    init_logging()

    confirm = getattr(args, "yes", False)

    if not confirm:
        log("AZURE-ML", "This will:")
        log("AZURE-ML", "  1. Cancel all running jobs")
        log("AZURE-ML", "  2. Delete all compute instances")
        log("AZURE-ML", "")
        log("AZURE-ML", "Use --yes to confirm")
        return 1

    # Cancel all running jobs
    log("AZURE-ML", "Canceling all running jobs...")
    args_cancel = type("Args", (), {"job": None})()
    cmd_azure_ml_cancel(args_cancel)

    # Delete all compute instances
    log("AZURE-ML", "")
    log("AZURE-ML", "Deleting all compute instances...")
    args_delete = type("Args", (), {"name": None, "all": True, "yes": True})()
    cmd_azure_ml_delete_compute(args_delete)

    log("AZURE-ML", "")
    log("AZURE-ML", "Cleanup complete")

    return 0


# Azure ML pricing (per hour) for common VM sizes
AZURE_ML_VM_PRICING = {
    "Standard_D8ds_v4": 0.45,
    "Standard_D8ds_v5": 0.45,
    "Standard_D4ds_v4": 0.23,
    "Standard_D4ds_v5": 0.23,
    "Standard_D16ds_v4": 0.91,
    "Standard_D16ds_v5": 0.91,
    "Standard_NC6": 0.90,
    "Standard_NC12": 1.80,
    "Standard_NC24": 3.60,
}


def get_compute_instance_details() -> list[dict]:
    """Get detailed info for all Azure ML compute instances including creation time.

    Returns:
        List of dicts with compute instance details (name, state, vmSize, createdOn)
    """
    from openadapt_evals.config import settings

    workspace = settings.azure_ml_workspace_name
    resource_group = settings.azure_ml_resource_group

    result = subprocess.run(
        [
            "az",
            "ml",
            "compute",
            "show-all",
            "--workspace-name",
            workspace,
            "--resource-group",
            resource_group,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # Fallback to list command with more detailed query
        result = subprocess.run(
            [
                "az",
                "ml",
                "compute",
                "list",
                "--workspace-name",
                workspace,
                "--resource-group",
                resource_group,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []

    try:
        all_compute = json.loads(result.stdout) if result.stdout.strip() else []
        # Filter to only compute instances
        instances = [c for c in all_compute if c.get("type") == "computeinstance"]
        return instances
    except json.JSONDecodeError:
        return []


def get_compute_instance_creation_time(compute_name: str) -> Optional[datetime]:
    """Get the creation time of a compute instance.

    Args:
        compute_name: Name of the compute instance

    Returns:
        datetime of creation or None if not found
    """
    from openadapt_evals.config import settings

    workspace = settings.azure_ml_workspace_name
    resource_group = settings.azure_ml_resource_group

    result = subprocess.run(
        [
            "az",
            "ml",
            "compute",
            "show",
            "--name",
            compute_name,
            "--workspace-name",
            workspace,
            "--resource-group",
            resource_group,
            "--query",
            "created_on",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        # Parse ISO format timestamp
        created_str = result.stdout.strip()
        # Handle both formats: with and without microseconds
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ]:
            try:
                return datetime.strptime(created_str.replace("Z", "+00:00"), fmt)
            except ValueError:
                continue
        return None
    except Exception:
        return None


def cmd_azure_ml_cost(args):
    """Show estimated cost of running Azure ML compute instances.

    Usage:
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-cost
    """
    init_logging()

    log("COST", "Azure ML Compute Instances:")
    log("COST", "=" * 60)

    # Get all compute instances
    instances = list_azure_ml_compute_instances()

    if not instances:
        log("COST", "No compute instances found")
        return 0

    total_cost = 0.0
    now = datetime.now()

    for inst in instances:
        name = inst.get("name", "unknown")
        state = inst.get("state", "unknown")
        vm_size = inst.get("vmSize", "unknown")

        # Get hourly rate
        hourly_rate = AZURE_ML_VM_PRICING.get(vm_size, 0.45)  # Default to $0.45/hr

        # Get creation time
        created_on = get_compute_instance_creation_time(name)

        if created_on:
            # Calculate uptime
            if created_on.tzinfo:
                # Make now timezone-aware for comparison
                from datetime import timezone

                now_tz = datetime.now(timezone.utc)
                uptime_seconds = (now_tz - created_on).total_seconds()
            else:
                uptime_seconds = (now - created_on).total_seconds()

            uptime_hours = uptime_seconds / 3600
            uptime_minutes = int((uptime_seconds % 3600) / 60)
            cost = uptime_hours * hourly_rate

            # Only count cost if running
            if state.lower() in ["running", "starting"]:
                total_cost += cost

            log("COST", f"  {name}")
            log("COST", f"    Status: {state}")
            log("COST", f"    Size: {vm_size} (${hourly_rate:.2f}/hr)")
            log("COST", f"    Created: {created_on.strftime('%Y-%m-%d %H:%M:%S')}")
            log("COST", f"    Uptime: {int(uptime_hours)}h {uptime_minutes}m")
            log("COST", f"    Cost: ${cost:.2f}")
        else:
            log("COST", f"  {name}")
            log("COST", f"    Status: {state}")
            log("COST", f"    Size: {vm_size} (${hourly_rate:.2f}/hr)")
            log("COST", "    Created: (unknown)")

    log("COST", "=" * 60)
    log("COST", f"Total Running Cost: ${total_cost:.2f}")

    return 0


def cmd_azure_ml_teardown(args):
    """Tear down Azure ML resources (cancel jobs, delete compute, optionally delete resource group).

    Usage:
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-teardown
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-teardown --force  # Skip confirmation
        uv run python -m openadapt_evals.benchmarks.vm_cli azure-ml-teardown --delete-resource-group
    """
    init_logging()
    from openadapt_evals.config import settings

    force = getattr(args, "force", False)
    delete_rg = getattr(args, "delete_resource_group", False)

    # Calculate current cost before teardown
    log("TEARDOWN", "Calculating current costs...")
    instances = list_azure_ml_compute_instances()
    total_cost = 0.0

    for inst in instances:
        name = inst.get("name", "unknown")
        vm_size = inst.get("vmSize", "unknown")
        state = inst.get("state", "unknown")
        hourly_rate = AZURE_ML_VM_PRICING.get(vm_size, 0.45)

        created_on = get_compute_instance_creation_time(name)
        if created_on and state.lower() in ["running", "starting"]:
            from datetime import timezone

            now_tz = datetime.now(timezone.utc)
            if created_on.tzinfo:
                uptime_hours = (now_tz - created_on).total_seconds() / 3600
            else:
                uptime_hours = (datetime.now() - created_on).total_seconds() / 3600
            total_cost += uptime_hours * hourly_rate

    # Find running jobs
    log("TEARDOWN", "Finding running jobs...")
    result = subprocess.run(
        [
            "az",
            "ml",
            "job",
            "list",
            "-g",
            settings.azure_ml_resource_group,
            "-w",
            settings.azure_ml_workspace_name,
            "--query",
            "[?status=='Running'].name",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
    )
    running_jobs = (
        result.stdout.strip().split("\n")
        if result.returncode == 0 and result.stdout.strip()
        else []
    )
    running_jobs = [j for j in running_jobs if j]  # Filter empty strings

    if running_jobs:
        log(
            "TEARDOWN",
            f"Found {len(running_jobs)} running job(s): {', '.join(running_jobs)}",
        )
    else:
        log("TEARDOWN", "No running jobs found")

    # Find compute instances
    log("TEARDOWN", "")
    log("TEARDOWN", "Finding compute instances...")
    if instances:
        log(
            "TEARDOWN",
            f"Found {len(instances)} compute instance(s): {', '.join(i['name'] for i in instances)}",
        )
    else:
        log("TEARDOWN", "No compute instances found")

    # Confirm if not force
    if not force and (running_jobs or instances):
        log("TEARDOWN", "")
        log("TEARDOWN", "This will:")
        if running_jobs:
            log("TEARDOWN", f"  - Cancel {len(running_jobs)} running job(s)")
        if instances:
            log("TEARDOWN", f"  - Delete {len(instances)} compute instance(s)")
        if delete_rg:
            log(
                "TEARDOWN",
                f"  - Delete resource group: {settings.azure_ml_resource_group}",
            )
        log("TEARDOWN", "")

        try:
            confirm = input("Proceed? [y/N]: ").strip().lower()
            if confirm != "y":
                log("TEARDOWN", "Aborted")
                return 1
        except (KeyboardInterrupt, EOFError):
            log("TEARDOWN", "Aborted")
            return 1

    # Cancel running jobs
    for job_name in running_jobs:
        log("TEARDOWN", f"Canceling job {job_name}...")
        result = subprocess.run(
            [
                "az",
                "ml",
                "job",
                "cancel",
                "--name",
                job_name,
                "-g",
                settings.azure_ml_resource_group,
                "-w",
                settings.azure_ml_workspace_name,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log("TEARDOWN", f"Canceled job {job_name}")
        else:
            log("TEARDOWN", f"Failed to cancel job {job_name}: {result.stderr}")

    # Delete compute instances
    log("TEARDOWN", "")
    for inst in instances:
        name = inst.get("name", "unknown")
        log("TEARDOWN", f"Deleting compute instance {name}...")
        if delete_azure_ml_compute_instance(name):
            log("TEARDOWN", f"Deleted compute instance {name}")
        else:
            log("TEARDOWN", f"Failed to delete compute instance {name}")

    # Delete resource group if requested
    if delete_rg:
        log("TEARDOWN", "")
        log(
            "TEARDOWN",
            f"Deleting resource group: {settings.azure_ml_resource_group}...",
        )
        result = subprocess.run(
            [
                "az",
                "group",
                "delete",
                "--name",
                settings.azure_ml_resource_group,
                "--yes",
                "--no-wait",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log("TEARDOWN", "Resource group deletion initiated (async)")
        else:
            log("TEARDOWN", f"Failed to delete resource group: {result.stderr}")

    log("TEARDOWN", "")
    log("TEARDOWN", "Cleanup complete!")
    log("TEARDOWN", f"Total cost for this session: ${total_cost:.2f}")

    return 0


def cmd_view_pool(args):
    """Generate HTML viewer for WAA pool benchmark results.

    Parses log files from pool_run_* directories and generates an interactive
    HTML viewer with summary stats, per-worker breakdown, and task list.
    """
    import webbrowser

    from openadapt_evals.benchmarks.pool_viewer import generate_pool_results_viewer

    results_dir = Path(args.results_dir) if args.results_dir else Path("benchmark_results")

    # Find pool run directory
    if args.run_name:
        pool_dir = results_dir / args.run_name
        if not pool_dir.exists():
            # Try with pool_run_ prefix
            pool_dir = results_dir / f"pool_run_{args.run_name}"
    else:
        # Find most recent pool_run_* directory
        pool_dirs = sorted(results_dir.glob("pool_run_*"), reverse=True)
        if not pool_dirs:
            print("No pool_run_* directories found in benchmark_results/")
            print("Run 'pool-run' to generate benchmark results")
            return 1
        pool_dir = pool_dirs[0]

    if not pool_dir.exists():
        print(f"Directory not found: {pool_dir}")
        return 1

    # Check for log files
    log_files = list(pool_dir.glob("waa-pool-*.log"))
    if not log_files:
        print(f"No waa-pool-*.log files found in {pool_dir}")
        return 1

    print(f"Generating viewer for: {pool_dir}")
    print(f"Found {len(log_files)} log file(s)")

    # Generate viewer
    output_path = pool_dir / "results.html"
    generate_pool_results_viewer(pool_dir, output_path)

    print(f"Generated: {output_path}")

    # Open in browser
    if not args.no_open:
        print("Opening in browser...")
        webbrowser.open(f"file://{output_path.absolute()}")

    return 0


def cmd_tail_output(args):
    """List or tail background task output files."""
    task_dir = Path("/private/tmp/claude-501/-Users-abrichr-oa-src-openadapt-evals/tasks/")

    if not task_dir.exists():
        print(f"Task directory not found: {task_dir}")
        return 1

    if args.list:
        # List recent task output files
        output_files = sorted(
            task_dir.glob("*/output.txt"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not output_files:
            print("No task output files found")
            return 0

        print(f"Recent tasks in {task_dir}:")
        print("-" * 60)
        for f in output_files[:20]:  # Show last 20
            size = f.stat().st_size
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            task_id = f.parent.name
            print(f"  {task_id:40} {mtime}  {size:>10} bytes")
        return 0

    if args.task:
        # Tail specific task output
        output_file = task_dir / args.task / "output.txt"
        if not output_file.exists():
            print(f"Output file not found: {output_file}")
            return 1

        with open(output_file) as f:
            lines = f.readlines()

        # Show last N lines
        lines_to_show = lines[-args.lines :] if len(lines) > args.lines else lines
        print(f"Last {len(lines_to_show)} lines from {args.task}:")
        print("-" * 60)
        print("".join(lines_to_show), end="")
        return 0

    print("Use --list to list tasks or --task <id> to tail specific task")
    return 1


def cmd_resources(args):
    """Check Azure resources and update RESOURCES.md.

    This command:
    1. Queries Azure for running VMs and compute instances
    2. Updates RESOURCES.md with current status
    3. Warns about running resources and their costs

    Use this after session start or context compaction to avoid
    losing track of deployed resources.
    """
    from openadapt_evals.infrastructure.resource_tracker import (
        check_resources,
        update_resources_file,
    )

    status = check_resources()

    # Update RESOURCES.md
    try:
        update_resources_file(status)
    except Exception as e:
        print(f"Warning: Could not update RESOURCES.md: {e}")

    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
        return 0

    # Print human-readable status
    print("=" * 60)
    print("AZURE RESOURCE STATUS")
    print("=" * 60)
    print(f"Timestamp: {status['timestamp']}")
    print()

    if status["has_running_resources"]:
        print("WARNING: Running resources detected!")
        print(f"Estimated cost: ${status['total_running_cost_per_hour']:.2f}/hour")
        print()
        for warning in status["warnings"]:
            print(f"  - {warning}")
        print()
    elif status.get("query_failures"):
        # "Could not ask Azure" must never print as "nothing is running".
        print("RESOURCE STATE UNKNOWN: one or more Azure queries did not run.")
        print("Resources may still be billing.")
        for failure in status["query_failures"]:
            print(f"  - {failure}")
        print()
    else:
        print("No running resources detected.")
        print()

    # Show VMs
    if status["vms"]:
        print("Virtual Machines:")
        for vm in status["vms"]:
            state = "RUNNING" if vm["is_running"] else vm["state"]
            print(f"  {vm['name']}: {state} ({vm['size']}) - ${vm['hourly_rate']:.2f}/hr")
            if vm.get("ip"):
                print(f"    IP: {vm['ip']}")
        print()

    # Show compute instances
    if status["compute_instances"]:
        print("Azure ML Compute Instances:")
        for ci in status["compute_instances"]:
            state = "RUNNING" if ci["is_running"] else ci["state"]
            print(f"  {ci['name']}: {state} ({ci['size']}) - ${ci['hourly_rate']:.2f}/hr")
        print()

    # Show commands
    if status["has_running_resources"]:
        print("To stop billing:")
        print("  uv run python -m openadapt_evals.benchmarks.vm_cli deallocate")
        print()
        print("To delete all resources:")
        print("  uv run python -m openadapt_evals.benchmarks.vm_cli delete -y")
    else:
        print("All resources are stopped. No billing in progress.")

    print("=" * 60)
    return 0


# =============================================================================
# GPU Training Commands
# =============================================================================


def _get_gpu_vm_manager(cloud: str):
    """Get VM manager for GPU training."""
    if cloud == "azure":
        from openadapt_evals.infrastructure.azure_vm import AzureVMManager
        return AzureVMManager()
    elif cloud == "aws":
        from openadapt_evals.infrastructure.aws_vm import AWSVMManager
        return AWSVMManager()
    raise ValueError(f"Unknown cloud: {cloud}")


def cmd_gpu_setup(args):
    """Provision a GPU VM and install verl-agent for RL training."""
    import time
    from pathlib import Path

    from openadapt_evals.infrastructure.azure_vm import ssh_run

    cloud = getattr(args, "cloud", "azure")
    vm = _get_gpu_vm_manager(cloud)
    username = vm.ssh_username
    gpu_vm_name = "verl-train-00"

    if args.gpu_ip:
        ip = args.gpu_ip
        print(f"Using existing GPU VM: {ip}")
    else:
        print("Finding available GPU VM size...")
        vm_size, region, cost = vm.find_available_size_and_region(gpu=True)
        print(f"Selected: {vm_size} (${cost:.2f}/hr) in {region}")

        if args.dry_run:
            print(f"[DRY RUN] Would provision {vm_size} in {region}")
            return 0

        print(f"Creating GPU VM '{gpu_vm_name}'...")
        info = vm.create_vm(name=gpu_vm_name, region=region, size=vm_size)
        ip = info.get("publicIpAddress") or vm.get_vm_ip(gpu_vm_name)
        vm.set_auto_shutdown(gpu_vm_name, hours=6)

        # Wait for SSH
        print("Waiting for SSH...")
        for _ in range(30):
            try:
                result = ssh_run(ip, "echo ready", username=username, stream=False)
                if result.returncode == 0:
                    break
            except Exception:
                pass
            time.sleep(10)
        else:
            print(f"ERROR: SSH not ready after 5 minutes: {ip}")
            return 1

    # Upload and run setup script
    setup_script = Path(__file__).parent.parent.parent / "scripts" / "setup_gpu_training.sh"
    if not setup_script.exists():
        print(f"ERROR: Setup script not found: {setup_script}")
        return 1

    import subprocess

    from openadapt_evals.infrastructure.azure_vm import SSH_OPTS

    print("Uploading setup script...")
    subprocess.run(
        ["scp", *SSH_OPTS, str(setup_script), f"{username}@{ip}:/tmp/setup_gpu_training.sh"],
        check=True,
    )

    print("Running setup (this may take 15-30 minutes)...")
    result = ssh_run(ip, "bash /tmp/setup_gpu_training.sh", username=username, stream=True)
    if result.returncode != 0:
        print(f"ERROR: Setup failed with exit code {result.returncode}")
        return 1

    print(f"\nGPU VM ready at: {ip}")
    print(f"SSH: ssh {username}@{ip}")
    return 0


def cmd_gpu_train(args):
    """Launch verl-agent training on a GPU VM."""
    import time
    from pathlib import Path

    from openadapt_evals.infrastructure.azure_vm import ssh_run

    cloud = getattr(args, "cloud", "azure")
    vm = _get_gpu_vm_manager(cloud)
    username = vm.ssh_username
    gpu_vm_name = "verl-train-00"

    if args.gpu_ip:
        ip = args.gpu_ip
        print(f"Using existing GPU VM: {ip}")
    else:
        # Provision GPU VM
        print("Finding available GPU VM size...")
        vm_size, region, cost = vm.find_available_size_and_region(gpu=True)
        print(f"Selected: {vm_size} (${cost:.2f}/hr) in {region}")
        print(f"Creating GPU VM '{gpu_vm_name}'...")
        info = vm.create_vm(name=gpu_vm_name, region=region, size=vm_size)
        ip = info.get("publicIpAddress") or vm.get_vm_ip(gpu_vm_name)
        vm.set_auto_shutdown(gpu_vm_name, hours=6)

        # Wait for SSH
        for _ in range(30):
            try:
                result = ssh_run(ip, "echo ready", username=username, stream=False)
                if result.returncode == 0:
                    break
            except Exception:
                pass
            time.sleep(10)

    # Setup if needed
    if not args.skip_setup:
        setup_script = Path(__file__).parent.parent.parent / "scripts" / "setup_gpu_training.sh"
        if setup_script.exists():
            import subprocess

            from openadapt_evals.infrastructure.azure_vm import SSH_OPTS
            subprocess.run(
                ["scp", *SSH_OPTS, str(setup_script), f"{username}@{ip}:/tmp/setup_gpu_training.sh"],
                check=True,
            )
            result = ssh_run(ip, "bash /tmp/setup_gpu_training.sh", username=username, stream=True)
            if result.returncode != 0:
                print("ERROR: Setup failed")
                return 1

    # Delegate to the E2E script's launch_training() which handles:
    # - env registry registration
    # - training data preparation
    # - training config generation
    # - training launch
    # This avoids duplicating the Hydra overrides in two places.
    import importlib.util
    _script_path = Path(__file__).parent.parent.parent / "scripts" / "train_verl_e2e.py"
    _spec = importlib.util.spec_from_file_location("train_verl_e2e", _script_path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    launch_training = _mod.launch_training

    print(f"Launching {args.algorithm} training on {args.n_gpus} GPU(s)...")
    print(f"Model: {args.model}")
    print(f"WAA server: {args.waa_server}")
    print(f"Evaluate server: {args.evaluate_server}")
    print(f"Task: {args.task_id}")

    try:
        exit_code = launch_training(
            ip=ip,
            waa_server=args.waa_server,
            task_id=args.task_id,
            algorithm=args.algorithm,
            model=args.model,
            n_gpus=args.n_gpus,
            epochs=args.epochs,
            username=username,
            evaluate_url=args.evaluate_server,
        )
        return exit_code
    finally:
        if args.cleanup and not args.gpu_ip:
            print(f"Deallocating GPU VM '{gpu_vm_name}'...")
            vm.deallocate_vm(gpu_vm_name)


# =============================================================================
# Main
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="WAA Benchmark CLI v2 - Minimal working CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full setup workflow (vanilla WAA)
  %(prog)s create          # Create Azure VM
  %(prog)s pull            # Pull vanilla WAA image
  %(prog)s start           # Start container + Windows
  %(prog)s probe --wait    # Wait for WAA server
  %(prog)s run --num-tasks 1 --agent navi   # Run benchmark
  %(prog)s deallocate      # Stop billing

  # Monitor in separate terminal
  %(prog)s logs --docker   # Docker container logs
  %(prog)s vnc             # View Windows desktop

  # Cleanup
  %(prog)s delete
""",
    )

    parser.add_argument(
        "--resource-group",
        default=None,
        help="Azure resource group (default: from AZURE_RESOURCE_GROUP env var or 'openadapt-agents')",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create Azure VM")
    p_create.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker VMs to create for parallel evaluation (default: 1)",
    )
    p_create.add_argument(
        "--auto-shutdown-hours",
        type=int,
        default=4,
        help="Auto-shutdown VM after N hours (0 to disable, default: 4)",
    )
    p_create.set_defaults(func=cmd_create)

    # delete
    p_delete = subparsers.add_parser("delete", help="Delete VM and all resources")
    p_delete.set_defaults(func=cmd_delete)

    # Shared --cloud argument for pool commands
    _cloud_kwargs = {
        "choices": ["azure", "aws"],
        "default": None,
        "help": "Cloud provider (default: from config, usually azure)",
    }

    # pool-status
    p_pool_status = subparsers.add_parser(
        "pool-status", help="Show status of all VMs in the current pool"
    )
    p_pool_status.add_argument("--cloud", **_cloud_kwargs)
    p_pool_status.add_argument(
        "--probe",
        action="store_true",
        help="Check WAA readiness on each VM",
    )
    p_pool_status.set_defaults(func=cmd_pool_status)

    # delete-pool
    p_delete_pool = subparsers.add_parser("delete-pool", help="Delete all VMs in the current pool")
    p_delete_pool.add_argument("--cloud", **_cloud_kwargs)
    p_delete_pool.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p_delete_pool.set_defaults(func=cmd_delete_pool)

    # pool-create
    p_pool_create = subparsers.add_parser(
        "pool-create", help="Create a pool of VMs for parallel WAA evaluation"
    )
    p_pool_create.add_argument("--cloud", **_cloud_kwargs)
    p_pool_create.add_argument(
        "--workers",
        "-n",
        type=int,
        default=3,
        help="Number of worker VMs to create (default: 3)",
    )
    p_pool_create.add_argument(
        "--auto-shutdown-hours",
        type=int,
        default=4,
        help="Auto-shutdown VMs after N hours (0 to disable, default: 4)",
    )
    p_pool_create.add_argument(
        "--use-acr",
        action="store_true",
        help="Pull waa-auto from ACR instead of building on VM (faster)",
    )
    p_pool_create.add_argument(
        "--image",
        help="Azure Managed Image ID to create VMs from (skips Docker setup)",
    )
    p_pool_create.set_defaults(func=cmd_pool_create)

    # pool-wait
    p_pool_wait = subparsers.add_parser(
        "pool-wait", help="Wait for all pool workers to have WAA ready"
    )
    p_pool_wait.add_argument("--cloud", **_cloud_kwargs)
    p_pool_wait.add_argument(
        "--timeout", "-t", type=int, default=30, help="Timeout in minutes (default: 30)"
    )
    p_pool_wait.add_argument("--qualification-dir", required=True, type=Path)
    p_pool_wait.set_defaults(func=cmd_pool_wait)

    # pool-run
    p_pool_run = subparsers.add_parser(
        "pool-run", help="Run WAA benchmark tasks distributed across pool workers"
    )
    p_pool_run.add_argument("--cloud", **_cloud_kwargs)
    p_pool_run.add_argument(
        "--tasks",
        "-n",
        type=int,
        default=10,
        help="Number of tasks to run (default: 10, use 154 for full benchmark)",
    )
    p_pool_run.add_argument("--agent", default="navi", help="Agent type (default: navi)")
    p_pool_run.add_argument(
        "--model", default="gpt-4o-mini", help="Model name (default: gpt-4o-mini)"
    )
    p_pool_run.add_argument("--api-key", help="OpenAI API key (default: from .env)")
    p_pool_run.add_argument("--qualification-dir", required=True, type=Path)
    p_pool_run.set_defaults(func=cmd_pool_run)

    # pool-cleanup
    p_pool_cleanup = subparsers.add_parser(
        "pool-cleanup", help="Clean up orphaned pool resources (VMs, NICs, IPs, disks)"
    )
    p_pool_cleanup.add_argument("--cloud", **_cloud_kwargs)
    p_pool_cleanup.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p_pool_cleanup.set_defaults(func=cmd_pool_cleanup)

    # pool-auto (end-to-end: create → wait → run)
    p_pool_auto = subparsers.add_parser(
        "pool-auto",
        help="Fully automated: create VMs → wait for WAA → run benchmark",
    )
    p_pool_auto.add_argument("--cloud", **_cloud_kwargs)
    p_pool_auto.add_argument(
        "--workers", "-w", type=int, default=1, help="Number of worker VMs (default: 1)"
    )
    p_pool_auto.add_argument(
        "--tasks", "-n", type=int,
        help="Number of tasks to run (default: one per worker)",
    )
    p_pool_auto.add_argument("--agent", default="navi", help="Agent type (default: navi)")
    p_pool_auto.add_argument(
        "--model", default="gpt-4o-mini", help="Model name (default: gpt-4o-mini)"
    )
    p_pool_auto.add_argument("--api-key", help="OpenAI API key (default: from .env)")
    p_pool_auto.add_argument(
        "--timeout", "-t", type=int, default=45,
        help="Timeout in minutes for WAA readiness (default: 45)",
    )
    p_pool_auto.add_argument(
        "--auto-shutdown-hours", type=int, default=4,
        help="Auto-shutdown VMs after N hours (default: 4)",
    )
    p_pool_auto.add_argument(
        "--qualification-dir",
        required=True,
        type=Path,
        help="Directory with <worker>.identity.json and <worker>.egress.json",
    )
    p_pool_auto.add_argument("--run-evidence-dir", required=True, type=Path)
    p_pool_auto.set_defaults(func=cmd_pool_auto)

    # pool-pause
    p_pool_pause = subparsers.add_parser(
        "pool-pause",
        help="Deallocate pool VMs (stops compute billing, keeps disks ~$0.25/day)",
    )
    p_pool_pause.add_argument("--cloud", **_cloud_kwargs)
    p_pool_pause.set_defaults(func=cmd_pool_pause)

    # pool-resume
    p_pool_resume = subparsers.add_parser(
        "pool-resume",
        help="Resume a paused pool (start VMs, wait for WAA ~5 min)",
    )
    p_pool_resume.add_argument("--cloud", **_cloud_kwargs)
    p_pool_resume.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=10,
        help="Timeout in minutes for WAA readiness (default: 10)",
    )
    p_pool_resume.set_defaults(func=cmd_pool_resume)

    # pool-reset
    p_pool_reset = subparsers.add_parser(
        "pool-reset",
        help="Restore one Windows worker from an exact verified baseline",
    )
    p_pool_reset.add_argument("--cloud", **_cloud_kwargs)
    p_pool_reset.add_argument("--worker", required=True)
    p_pool_reset.add_argument("--run-id", required=True)
    p_pool_reset.add_argument("--worker-identity", required=True, type=Path)
    p_pool_reset.set_defaults(func=cmd_pool_reset)

    # pool-egress
    p_pool_egress = subparsers.add_parser(
        "pool-egress",
        help="Apply an exact host-enforced egress policy to one worker",
    )
    p_pool_egress.add_argument("--cloud", **_cloud_kwargs)
    p_pool_egress.add_argument("--worker", required=True)
    p_pool_egress.add_argument("--policy", required=True, type=Path)
    p_pool_egress.add_argument("--worker-identity", required=True, type=Path)
    p_pool_egress.add_argument("--reset-proof", required=True, type=Path)
    p_pool_egress.set_defaults(func=cmd_pool_egress)

    # pool-start
    p_pool_start = subparsers.add_parser(
        "pool-start",
        help="Start one Windows worker through a fresh one-use run gate",
    )
    p_pool_start.add_argument("--cloud", **_cloud_kwargs)
    p_pool_start.add_argument("--worker", required=True)
    p_pool_start.add_argument("--worker-identity", required=True, type=Path)
    p_pool_start.add_argument("--policy", required=True, type=Path)
    p_pool_start.add_argument("--reset-proof", required=True, type=Path)
    p_pool_start.add_argument("--egress-proof", required=True, type=Path)
    p_pool_start.set_defaults(func=cmd_pool_start)

    # pool-logs
    p_pool_logs = subparsers.add_parser(
        "pool-logs",
        help="Stream logs from all pool workers (interleaved with prefixes)",
    )
    p_pool_logs.set_defaults(func=cmd_pool_logs)

    # pool-vnc
    p_pool_vnc = subparsers.add_parser(
        "pool-vnc", help="Open VNC to pool workers (view Windows desktop)"
    )
    p_pool_vnc.add_argument("--worker", help="Worker name to connect to (e.g., waa-pool-00)")
    p_pool_vnc.add_argument("--all", action="store_true", help="Set up tunnels to all workers")
    p_pool_vnc.set_defaults(func=cmd_pool_vnc)

    # pool-exec
    p_pool_exec = subparsers.add_parser("pool-exec", help="Execute command on pool workers")
    p_pool_exec.add_argument("--cmd", required=True, help="Command to run")
    p_pool_exec.add_argument("--docker", action="store_true", help="Run inside Docker container")
    p_pool_exec.add_argument("--worker", help="Run on specific worker only")
    p_pool_exec.set_defaults(func=cmd_pool_exec)

    # status
    p_status = subparsers.add_parser("status", help="Show VM status")
    p_status.set_defaults(func=cmd_status)

    # build
    p_build = subparsers.add_parser("build", help="Build WAA image from waa_deploy/Dockerfile")
    p_build.add_argument(
        "--local",
        action="store_true",
        help="Build locally using Docker for Mac/Linux instead of on the VM",
    )
    p_build.add_argument(
        "--push",
        action="store_true",
        help="Push image to Azure Container Registry after building",
    )
    p_build.add_argument(
        "--acr",
        default="openadaptacr",
        help="ACR name for pushing (default: openadaptacr)",
    )
    p_build.set_defaults(func=cmd_build)

    # build-status
    p_build_status = subparsers.add_parser(
        "build-status", help="Check status of Docker build on remote VM"
    )
    p_build_status.add_argument(
        "--lines", "-n", type=int, default=30, help="Number of log lines to show"
    )
    p_build_status.set_defaults(func=cmd_build_status)

    # push-acr
    p_push_acr = subparsers.add_parser(
        "push-acr", help="Push Docker image to Azure Container Registry"
    )
    p_push_acr.add_argument(
        "--local",
        action="store_true",
        help="Push from local Docker instead of VM",
    )
    p_push_acr.add_argument(
        "--acr",
        default="openadaptacr",
        help="ACR name (default: openadaptacr)",
    )
    p_push_acr.add_argument(
        "--image",
        default="waa-auto:latest",
        help="Image to push (default: waa-auto:latest)",
    )
    p_push_acr.set_defaults(func=cmd_push_acr)

    # image-create
    p_image_create = subparsers.add_parser(
        "image-create", help="Create golden image from existing pool VM (skips Docker setup on future creates)"
    )
    p_image_create.add_argument(
        "--name", help="Image name (default: waa-golden-YYYYMMDD-HHMMSS)"
    )
    p_image_create.add_argument(
        "--worker", help="Worker VM name to use as source (default: first worker)"
    )
    p_image_create.set_defaults(func=cmd_image_create)

    # image-list
    p_image_list = subparsers.add_parser(
        "image-list", help="List available golden images"
    )
    p_image_list.set_defaults(func=cmd_image_list)

    # image-delete
    p_image_delete = subparsers.add_parser(
        "image-delete", help="Delete a golden image"
    )
    p_image_delete.add_argument("name", help="Image name to delete")
    p_image_delete.set_defaults(func=cmd_image_delete)

    # start
    p_start = subparsers.add_parser("start", help="Start WAA container")
    p_start.add_argument(
        "--fresh", action="store_true", help="Clean storage for fresh Windows install"
    )
    p_start.add_argument("--no-vnc", action="store_true", help="Don't auto-launch VNC viewer")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop and remove WAA container")
    p_stop.add_argument("--clean", action="store_true", help="Also clean Windows storage")
    p_stop.set_defaults(func=cmd_stop)

    # probe
    p_probe = subparsers.add_parser("probe", help="Check if WAA server is ready")
    p_probe.add_argument("--wait", action="store_true", help="Wait until ready")
    p_probe.add_argument(
        "--timeout", type=int, default=1800, help="Timeout in seconds (default: 1800)"
    )
    p_probe.set_defaults(func=cmd_probe)

    # test-golden-image
    p_test_golden = subparsers.add_parser(
        "test-golden-image", help="Test that golden image boots and WAA responds"
    )
    p_test_golden.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Max wait time in seconds (default: 180)",
    )
    p_test_golden.set_defaults(func=cmd_test_golden_image)

    # test-blob-access
    p_test_blob = subparsers.add_parser(
        "test-blob-access", help="Test Azure blob storage access for golden image"
    )
    p_test_blob.set_defaults(func=cmd_test_blob_access)

    # test-api-key
    p_test_api = subparsers.add_parser("test-api-key", help="Test that OpenAI API key is valid")
    p_test_api.add_argument("--api-key", help="OpenAI API key (or set OPENAI_API_KEY in .env)")
    p_test_api.set_defaults(func=cmd_test_api_key)

    # test-waa-tasks
    p_test_tasks = subparsers.add_parser("test-waa-tasks", help="Test WAA task list accessibility")
    p_test_tasks.set_defaults(func=cmd_test_waa_tasks)

    # test-all
    p_test_all = subparsers.add_parser(
        "test-all", help="Run all pre-flight tests before Azure ML benchmark"
    )
    p_test_all.add_argument("--api-key", help="OpenAI API key (or set OPENAI_API_KEY in .env)")
    p_test_all.set_defaults(func=cmd_test_all)

    # smoke-test-aws
    p_smoke_aws = subparsers.add_parser(
        "smoke-test-aws",
        help="Smoke-test AWS VM backend (credentials, AMI, VPC, lifecycle)",
    )
    p_smoke_aws.add_argument(
        "--full",
        action="store_true",
        help="Run full lifecycle (create/stop/start/delete a real EC2 instance)",
    )
    p_smoke_aws.add_argument(
        "--region",
        default=None,
        help="AWS region override (default: from config or us-east-1)",
    )
    p_smoke_aws.add_argument(
        "--instance-type",
        default="m5a.xlarge",
        help="EC2 instance type for lifecycle test (default: m5a.xlarge, $0.17/hr)",
    )
    p_smoke_aws.set_defaults(func=cmd_smoke_test_aws)

    # run
    p_run = subparsers.add_parser("run", help="Run benchmark tasks (uses vanilla WAA navi agent)")
    p_run.add_argument(
        "--num-tasks",
        type=int,
        default=None,
        help="Number of tasks to run (default: all; ignored if --task specified)",
    )
    p_run.add_argument("--task", help="Specific task ID to run")
    p_run.add_argument(
        "--domain",
        default="all",
        help="Domain filter (e.g., 'notepad', 'chrome', 'all')",
    )
    p_run.add_argument("--model", default="gpt-4o", help="Model for navi agent (default: gpt-4o)")
    p_run.add_argument("--api-key", help="OpenAI API key (or set OPENAI_API_KEY in .env)")
    p_run.add_argument("--no-download", action="store_true", help="Skip downloading results")
    p_run.add_argument(
        "--worker-id",
        type=int,
        default=0,
        help="Worker ID for parallel execution (0-indexed)",
    )
    p_run.add_argument(
        "--num-workers",
        type=int,
        default=1,
        help="Total number of parallel workers",
    )
    p_run.set_defaults(func=cmd_run)

    # download
    p_download = subparsers.add_parser("download", help="Download benchmark results from VM")
    p_download.set_defaults(func=cmd_download)

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="Analyze benchmark results")
    p_analyze.add_argument(
        "--results-dir",
        help="Results directory (default: most recent in benchmark_results/)",
    )
    p_analyze.set_defaults(func=cmd_analyze)

    # tasks
    p_tasks = subparsers.add_parser("tasks", help="List available WAA benchmark tasks")
    p_tasks.add_argument("--verbose", "-v", action="store_true", help="Show all task IDs")
    p_tasks.set_defaults(func=cmd_tasks)

    # deallocate
    p_dealloc = subparsers.add_parser("deallocate", help="Stop VM (preserves disk)")
    p_dealloc.set_defaults(func=cmd_deallocate)

    # vm-start
    p_vmstart = subparsers.add_parser("vm-start", help="Start a deallocated VM")
    p_vmstart.set_defaults(func=cmd_vm_start)

    # windows-restart
    p_winrestart = subparsers.add_parser(
        "windows-restart",
        help="Restart Windows inside QEMU via monitor (hard reset)",
    )
    p_winrestart.add_argument(
        "--vm-ip", default=None, help="VM IP (default: auto-detect from Azure)"
    )
    p_winrestart.add_argument(
        "--server",
        default="http://localhost:5001",
        help="WAA server URL for readiness probe (default: http://localhost:5001)",
    )
    p_winrestart.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds to wait for WAA server after reset (default: 300)",
    )
    p_winrestart.set_defaults(func=cmd_windows_restart)

    # logs
    p_logs = subparsers.add_parser("logs", help="Show WAA status and logs")
    p_logs.add_argument(
        "--follow", "-f", action="store_true", help="Stream docker logs continuously"
    )
    p_logs.add_argument("--tail", "-n", type=int, help="Number of log lines to show (default: 20)")
    p_logs.add_argument(
        "--run",
        action="store_true",
        help="Show run command output instead of container logs",
    )
    p_logs.add_argument(
        "--progress",
        "-p",
        action="store_true",
        help="Show benchmark progress and estimated completion time",
    )
    p_logs.set_defaults(func=cmd_logs)

    # exec
    p_exec = subparsers.add_parser("exec", help="Run command on VM host")
    p_exec.add_argument("--cmd", required=True, help="Command to run")
    p_exec.set_defaults(func=cmd_exec)

    # docker-exec
    p_dexec = subparsers.add_parser("docker-exec", help="Run command inside winarena container")
    p_dexec.add_argument("--cmd", required=True, help="Command to run")
    p_dexec.set_defaults(func=cmd_docker_exec)

    # vnc
    p_vnc = subparsers.add_parser("vnc", help="Open VNC to view Windows desktop via SSH tunnel")
    p_vnc.set_defaults(func=cmd_vnc)

    # run-azure-ml
    p_azure_ml = subparsers.add_parser(
        "run-azure-ml",
        help="Run WAA benchmark on Azure ML (parallel compute instances)",
    )
    p_azure_ml.add_argument(
        "--setup",
        action="store_true",
        help="Setup mode: upload startup script to Azure ML datastore (run once before first benchmark)",
    )
    p_azure_ml.add_argument(
        "--check-image",
        action="store_true",
        help="Check if golden image exists in Azure blob storage",
    )
    p_azure_ml.add_argument(
        "--upload-image",
        action="store_true",
        help="Upload golden image from local storage to Azure blob storage (requires --prepare-image first)",
    )
    p_azure_ml.add_argument(
        "--image-source",
        help="Source path for golden image upload (required for --upload-image)",
    )
    p_azure_ml.add_argument(
        "--upload-placeholder",
        action="store_true",
        help="Upload minimal placeholder for VERSION=11e auto-download (alternative to full golden image)",
    )
    p_azure_ml.add_argument(
        "--upload-image-from-vm",
        action="store_true",
        help="Upload golden image from Azure VM to blob storage (for macOS users without local KVM)",
    )
    p_azure_ml.add_argument(
        "--ci-startup-script-path",
        default="Users/openadapt/compute-instance-startup.sh",
        help="Path to startup script in Azure ML datastore (default: Users/openadapt/compute-instance-startup.sh)",
    )
    p_azure_ml.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel compute instances (default: 1)",
    )
    p_azure_ml.add_argument(
        "--exp-name",
        help="Experiment name (default: auto-generated timestamp)",
    )
    p_azure_ml.add_argument(
        "--image",
        default="windowsarena/winarena:latest",
        help="Docker image (default: windowsarena/winarena:latest)",
    )
    p_azure_ml.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAI model name (default: gpt-4o-mini)",
    )
    p_azure_ml.add_argument(
        "--agent",
        default="navi",
        help="Agent to use (default: navi)",
    )
    # Cost tracking and resource management flags
    p_azure_ml.add_argument(
        "--cost-summary",
        action="store_true",
        help="Show Azure ML cost summary (compute instances, blob storage)",
    )
    p_azure_ml.add_argument(
        "--list-resources",
        action="store_true",
        help="List all Azure ML resources (compute instances, blob files, startup scripts)",
    )
    p_azure_ml.add_argument(
        "--teardown",
        action="store_true",
        help="Delete all Azure ML resources to stop costs (dry run by default)",
    )
    p_azure_ml.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm resource deletion (required with --teardown)",
    )
    p_azure_ml.add_argument(
        "--keep-image",
        action="store_true",
        help="Keep golden image when tearing down (avoids 30GB re-upload)",
    )
    p_azure_ml.set_defaults(func=cmd_run_azure_ml)

    # run-azure-ml-auto - Fully automated workflow
    p_azure_ml_auto = subparsers.add_parser(
        "run-azure-ml-auto",
        help="Fully automated Azure ML workflow (VM setup + golden image + benchmark)",
        description="""
Fully automated, unattended Azure ML workflow that handles everything:
  1. Create/start Azure VM if needed
  2. Start Windows container with VERSION=11e
  3. Wait for Windows installation and WAA server to become ready
  4. Upload golden image from VM to blob storage (if needed)
  5. Upload startup script to Azure ML datastore (if needed)
  6. Run Azure ML benchmark

All steps are idempotent - skips steps that are already complete.

Example:
  %(prog)s --workers 4         # Run with 4 workers
  %(prog)s --skip-upload       # Skip golden image upload
  %(prog)s --skip-benchmark    # Setup only, don't run benchmark
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_azure_ml_auto.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel Azure ML compute instances (default: 1)",
    )
    p_azure_ml_auto.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Total setup timeout in minutes (default: 45)",
    )
    p_azure_ml_auto.add_argument(
        "--probe-timeout",
        type=int,
        default=1800,
        help="WAA server probe timeout in seconds (default: 1800 = 30 min)",
    )
    p_azure_ml_auto.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip golden image upload (use if image already in blob storage)",
    )
    p_azure_ml_auto.add_argument(
        "--skip-benchmark",
        action="store_true",
        help="Setup only - VM + golden image, don't run benchmark",
    )
    p_azure_ml_auto.add_argument(
        "--exp-name",
        help="Experiment name (default: auto-generated timestamp)",
    )
    p_azure_ml_auto.add_argument(
        "--image",
        default="windowsarena/winarena:latest",
        help="Docker image (default: windowsarena/winarena:latest)",
    )
    p_azure_ml_auto.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAI model name (default: gpt-4o-mini)",
    )
    p_azure_ml_auto.add_argument(
        "--agent",
        default="navi",
        help="Agent to use (default: navi)",
    )
    p_azure_ml_auto.set_defaults(func=cmd_run_azure_ml_auto)

    # azure-ml-quota - Check quota and help request increases
    p_azure_quota = subparsers.add_parser(
        "azure-ml-quota",
        help="Check Azure ML quota and help request increases",
    )
    p_azure_quota.add_argument(
        "--location",
        help="Azure region (default: centralus for openadapt-ml-central workspace)",
    )
    p_azure_quota.add_argument(
        "--no-open",
        dest="open",
        action="store_false",
        help="Don't open browser automatically",
    )
    p_azure_quota.set_defaults(func=cmd_azure_ml_quota)

    # azure-ml-quota-request - Request quota increase via CLI
    p_azure_quota_req = subparsers.add_parser(
        "azure-ml-quota-request",
        help="Request Azure quota increase via CLI automation",
    )
    p_azure_quota_req.add_argument(
        "--family",
        default="standardDPDSv5Family",
        help="VM family resource name (default: standardDPDSv5Family)",
    )
    p_azure_quota_req.add_argument(
        "--vcpus",
        type=int,
        default=8,
        help="Number of vCPUs to request (default: 8)",
    )
    p_azure_quota_req.add_argument(
        "--location",
        default="eastus",
        help="Azure region (default: eastus)",
    )
    p_azure_quota_req.set_defaults(func=cmd_azure_ml_quota_request)

    # azure-ml-quota-wait - Wait for quota approval with polling
    p_azure_quota_wait = subparsers.add_parser(
        "azure-ml-quota-wait",
        help="Wait for Azure quota approval with polling",
    )
    p_azure_quota_wait.add_argument(
        "--family",
        default="Standard DDSv4 Family",
        help="VM family name (default: Standard DDSv4 Family)",
    )
    p_azure_quota_wait.add_argument(
        "--target",
        type=int,
        default=8,
        help="Target vCPU quota (default: 8)",
    )
    p_azure_quota_wait.add_argument(
        "--location",
        default="eastus",
        help="Azure region (default: eastus)",
    )
    p_azure_quota_wait.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Poll interval in seconds (default: 60)",
    )
    p_azure_quota_wait.add_argument(
        "--timeout",
        type=int,
        default=86400,
        help="Max wait time in seconds (default: 86400 = 24h)",
    )
    p_azure_quota_wait.add_argument(
        "--auto-run",
        action="store_true",
        help="Run evaluation when quota is ready",
    )
    p_azure_quota_wait.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    # Pass-through args for auto-run
    p_azure_quota_wait.add_argument(
        "--tasks",
        type=int,
        default=10,
        help="Number of tasks for auto-run (default: 10)",
    )
    p_azure_quota_wait.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAI model for auto-run (default: gpt-4o-mini)",
    )
    p_azure_quota_wait.set_defaults(func=cmd_azure_ml_quota_wait)

    # azure-ml-find-region - Find best region for a VM size
    p_azure_find_region = subparsers.add_parser(
        "azure-ml-find-region",
        help="Find best region for running Azure ML WAA evaluation",
    )
    p_azure_find_region.add_argument(
        "--vm-size",
        default="Standard_D8ds_v4",
        help="VM size to search for (default: Standard_D8ds_v4)",
    )
    p_azure_find_region.add_argument(
        "--vcpus",
        type=int,
        default=8,
        help="Minimum vCPUs required (default: 8)",
    )
    p_azure_find_region.add_argument(
        "--vm-quota",
        action="store_true",
        help="Check VM quota instead of ML Dedicated quota (default: ML Dedicated)",
    )
    p_azure_find_region.set_defaults(func=cmd_azure_ml_find_region)

    # azure-ml-status - Show Azure ML jobs and compute instances
    p_azure_status = subparsers.add_parser(
        "azure-ml-status",
        help="Show status of Azure ML jobs and compute instances",
    )
    p_azure_status.set_defaults(func=cmd_azure_ml_status)

    # azure-ml-vnc - Set up VNC tunnel to compute instance
    p_azure_vnc = subparsers.add_parser(
        "azure-ml-vnc",
        help="Set up VNC tunnel to Azure ML compute instance",
    )
    p_azure_vnc.add_argument(
        "--compute",
        help="Compute instance name (auto-detects if not specified)",
    )
    p_azure_vnc.add_argument(
        "--port",
        type=int,
        default=8007,
        help="Local port for VNC tunnel (default: 8007)",
    )
    p_azure_vnc.add_argument(
        "--open",
        action="store_true",
        help="Open browser to VNC URL",
    )
    p_azure_vnc.add_argument(
        "--wait",
        action="store_true",
        help="Wait for Ctrl+C (keeps tunnel open)",
    )
    p_azure_vnc.set_defaults(func=cmd_azure_ml_vnc)

    # azure-ml-monitor - Monitor jobs with auto VNC
    p_azure_monitor = subparsers.add_parser(
        "azure-ml-monitor",
        help="Monitor Azure ML jobs with auto VNC setup",
    )
    p_azure_monitor.add_argument(
        "--job",
        help="Job name to monitor (auto-detects running job if not specified)",
    )
    p_azure_monitor.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Poll interval in seconds (default: 30)",
    )
    p_azure_monitor.add_argument(
        "--no-vnc",
        dest="vnc",
        action="store_false",
        help="Disable auto VNC tunnel setup",
    )
    p_azure_monitor.set_defaults(func=cmd_azure_ml_monitor)

    # azure-ml-logs - Stream logs from Azure ML job
    p_azure_logs = subparsers.add_parser(
        "azure-ml-logs",
        help="Stream logs from Azure ML job in real-time",
    )
    p_azure_logs.add_argument(
        "--job",
        help="Job name to stream logs from (auto-detects most recent if not specified)",
    )
    p_azure_logs.add_argument(
        "--no-follow",
        dest="follow",
        action="store_false",
        help="Don't follow logs (exit after current output)",
    )
    p_azure_logs.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Poll interval in seconds when following (default: 5)",
    )
    p_azure_logs.set_defaults(func=cmd_azure_ml_logs)

    # azure-ml-stream - Stream logs via SDK (recommended)
    p_azure_stream = subparsers.add_parser(
        "azure-ml-stream",
        help="Stream job logs via Azure ML SDK (recommended)",
        description="""
Stream logs from Azure ML job using the ./logs/ folder.

This command uses the Azure ML SDK to fetch logs written to ./logs/
by run_entry.py. It's more reliable than direct blob access and
doesn't require storage account keys.

Files fetched:
  - ./logs/job.log     - Plain text log (human-readable)
  - ./logs/events.jsonl - Structured events (JSON lines)
  - ./logs/progress.json - Current progress state

Examples:
  azure-ml-stream                    # Most recent job
  azure-ml-stream --job JOB_NAME     # Specific job
  azure-ml-stream --follow           # Real-time streaming (default)
  azure-ml-stream --no-follow        # Fetch once and exit
  azure-ml-stream --progress         # Show progress bar only
  azure-ml-stream --events           # Show structured events
""",
    )
    p_azure_stream.add_argument(
        "--job",
        help="Job name (auto-detects most recent if not specified)",
    )
    p_azure_stream.add_argument(
        "--no-follow",
        dest="follow",
        action="store_false",
        help="Don't follow logs (exit after fetching)",
    )
    p_azure_stream.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Poll interval in seconds (default: 5)",
    )
    p_azure_stream.add_argument(
        "--progress",
        action="store_true",
        help="Show progress bar only (from progress.json)",
    )
    p_azure_stream.add_argument(
        "--events",
        action="store_true",
        help="Show structured events (from events.jsonl)",
    )
    p_azure_stream.add_argument(
        "--auto-teardown",
        action="store_true",
        help="Automatically delete compute instance when job completes/fails",
    )
    p_azure_stream.set_defaults(func=cmd_azure_ml_stream_logs)

    # azure-ml-progress - Show job progress summary
    p_azure_progress = subparsers.add_parser(
        "azure-ml-progress",
        help="Show current progress of an Azure ML job",
        description="""
Show progress of an Azure ML job from progress.json.

This fetches the progress.json file written by run_entry.py and displays
a visual summary including:
  - Current phase (init, setup, vm_startup, benchmark)
  - Progress percentage with progress bar
  - Recent log messages
  - Job status

Examples:
  azure-ml-progress                  # Most recent job
  azure-ml-progress --job JOB_NAME   # Specific job
  azure-ml-progress --watch          # Poll continuously
""",
    )
    p_azure_progress.add_argument(
        "--job",
        help="Job name (auto-detects most recent if not specified)",
    )
    p_azure_progress.add_argument(
        "--watch",
        action="store_true",
        help="Poll continuously (Ctrl+C to stop)",
    )
    p_azure_progress.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Poll interval in seconds for --watch (default: 10)",
    )
    p_azure_progress.set_defaults(func=cmd_azure_ml_progress)

    # azure-ml-cancel - Cancel a running Azure ML job
    p_azure_cancel = subparsers.add_parser(
        "azure-ml-cancel",
        help="Cancel a running Azure ML job",
    )
    p_azure_cancel.add_argument(
        "--job",
        help="Job name to cancel (default: most recent running job)",
    )
    p_azure_cancel.set_defaults(func=cmd_azure_ml_cancel)

    # azure-ml-delete-compute - Delete Azure ML compute instances
    p_azure_delete_compute = subparsers.add_parser(
        "azure-ml-delete-compute",
        help="Delete Azure ML compute instance(s)",
    )
    p_azure_delete_compute.add_argument(
        "--name",
        help="Compute instance name to delete",
    )
    p_azure_delete_compute.add_argument(
        "--all",
        action="store_true",
        help="Delete all compute instances",
    )
    p_azure_delete_compute.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Confirm deletion without prompting",
    )
    p_azure_delete_compute.set_defaults(func=cmd_azure_ml_delete_compute)

    # azure-ml-cleanup - Cancel jobs and delete compute instances
    p_azure_cleanup = subparsers.add_parser(
        "azure-ml-cleanup",
        help="Clean up Azure ML resources (cancel jobs + delete compute)",
    )
    p_azure_cleanup.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Confirm cleanup without prompting",
    )
    p_azure_cleanup.set_defaults(func=cmd_azure_ml_cleanup)

    # azure-ml-cost - Show compute instance costs
    p_azure_cost = subparsers.add_parser(
        "azure-ml-cost",
        help="Show estimated cost of Azure ML compute instances",
        description="""
Show estimated cost of running Azure ML compute instances.

Calculates cost based on:
  - VM size (Standard_D8ds_v4 = $0.45/hr)
  - Uptime from creation time
  - Current state (running/stopped)

Examples:
  azure-ml-cost                 # Show all compute instances and costs
""",
    )
    p_azure_cost.set_defaults(func=cmd_azure_ml_cost)

    # azure-ml-teardown - Full teardown with cost summary
    p_azure_teardown = subparsers.add_parser(
        "azure-ml-teardown",
        help="Tear down Azure ML resources with cost summary",
        description="""
Tear down Azure ML resources (cancel jobs, delete compute instances).

This command:
  1. Shows all running jobs and compute instances
  2. Calculates total cost
  3. Cancels all running jobs
  4. Deletes all compute instances
  5. Optionally deletes the entire resource group

Examples:
  azure-ml-teardown                    # Interactive teardown
  azure-ml-teardown --force            # Skip confirmation
  azure-ml-teardown --delete-resource-group  # Also delete resource group
""",
    )
    p_azure_teardown.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompts",
    )
    p_azure_teardown.add_argument(
        "--delete-resource-group",
        action="store_true",
        help="Also delete the entire resource group (DESTRUCTIVE)",
    )
    p_azure_teardown.set_defaults(func=cmd_azure_ml_teardown)

    # tail-output
    p_tail = subparsers.add_parser(
        "tail-output",
        help="List or tail background task output files",
    )
    p_tail.add_argument(
        "--list",
        action="store_true",
        help="List recent task output files",
    )
    p_tail.add_argument(
        "--task",
        help="Task ID to tail (shows last N lines)",
    )
    p_tail.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Number of lines to show (default: 50)",
    )
    p_tail.set_defaults(func=cmd_tail_output)

    # resources - Check Azure resource status
    p_resources = subparsers.add_parser(
        "resources",
        help="Check Azure resources and update RESOURCES.md",
    )
    p_resources.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    p_resources.set_defaults(func=cmd_resources)

    # view-pool - Generate HTML viewer for pool benchmark results
    p_view_pool = subparsers.add_parser(
        "view-pool",
        help="Generate HTML viewer for WAA pool benchmark results",
        description="""
Generate an interactive HTML viewer for WAA pool benchmark results.

Parses log files from pool_run_* directories to extract task results and
generates a standalone HTML viewer with:
  - Summary stats (total tasks, success rate, avg time per task)
  - Per-worker breakdown
  - Task list with pass/fail status
  - Domain breakdown (success rate per domain)
  - Filters for domain and status

Examples:
  view-pool                     # View most recent pool_run_* results
  view-pool --run-name pool_run_20260204  # View specific run
  view-pool --no-open           # Generate HTML without opening browser
""",
    )
    p_view_pool.add_argument(
        "--run-name",
        help="Name of pool run directory (e.g., pool_run_20260204)",
    )
    p_view_pool.add_argument(
        "--results-dir",
        help="Base results directory (default: benchmark_results/)",
    )
    p_view_pool.add_argument(
        "--no-open",
        action="store_true",
        help="Don't auto-open browser",
    )
    p_view_pool.set_defaults(func=cmd_view_pool)

    # --- GPU Training Commands ---

    p_gpu_setup = subparsers.add_parser(
        "gpu-setup",
        help="Provision a GPU VM and install verl-agent for RL training",
    )
    p_gpu_setup.add_argument(
        "--cloud", choices=["azure", "aws"], default="azure",
        help="Cloud provider (default: azure)",
    )
    p_gpu_setup.add_argument(
        "--gpu-ip", type=str, default=None,
        help="Use an existing GPU VM (skip provisioning)",
    )
    p_gpu_setup.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without doing it",
    )
    p_gpu_setup.set_defaults(func=cmd_gpu_setup)

    p_gpu_train = subparsers.add_parser(
        "gpu-train",
        help="Launch verl-agent training on a GPU VM",
    )
    p_gpu_train.add_argument(
        "--cloud", choices=["azure", "aws"], default="azure",
        help="Cloud provider (default: azure)",
    )
    p_gpu_train.add_argument(
        "--gpu-ip", type=str, default=None,
        help="Use an existing GPU VM (skip provisioning)",
    )
    p_gpu_train.add_argument(
        "--waa-server", type=str, default="http://localhost:5000",
        help="WAA Flask API URL (screenshots, actions) (default: http://localhost:5000)",
    )
    p_gpu_train.add_argument(
        "--evaluate-server", type=str, default="http://localhost:5001",
        help="Evaluate server URL (setup, evaluate) (default: http://localhost:5001)",
    )
    p_gpu_train.add_argument(
        "--task-id", type=str, required=True,
        help="WAA task UUID to train on",
    )
    p_gpu_train.add_argument(
        "--algorithm", choices=["gigpo", "grpo", "ppo"], default="gigpo",
        help="RL algorithm (default: gigpo)",
    )
    p_gpu_train.add_argument(
        "--model", type=str, default="Qwen/Qwen2.5-VL-3B-Instruct",
        help="Model to train",
    )
    p_gpu_train.add_argument(
        "--n-gpus", type=int, default=1,
        help="Number of GPUs (default: 1, use 4 for g5.12xlarge)",
    )
    p_gpu_train.add_argument(
        "--epochs", type=int, default=100,
        help="Training epochs (default: 100)",
    )
    p_gpu_train.add_argument(
        "--skip-setup", action="store_true",
        help="Skip setup (VM already configured)",
    )
    p_gpu_train.add_argument(
        "--cleanup", action="store_true",
        help="Deallocate GPU VM after training",
    )
    p_gpu_train.set_defaults(func=cmd_gpu_train)

    args = parser.parse_args()

    # Allow --resource-group to override the module-level constant
    global RESOURCE_GROUP
    if args.resource_group is not None:
        RESOURCE_GROUP = args.resource_group

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
