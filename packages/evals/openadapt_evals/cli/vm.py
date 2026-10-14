"""VM management commands for oa evals.

This module provides Azure VM lifecycle commands:
- setup: Create and configure VM with WAA
- status: Show VM status
- start/stop/deallocate: Control VM state
- delete: Remove VM and resources
- probe: Check WAA server status
- logs: View container logs
- diag: Diagnostic info
- ssh/vnc: Interactive access
- exec: Run commands
- monitor: Start monitoring dashboard

Usage:
    oa evals vm setup           # Full setup
    oa evals vm status          # Check status
    oa evals vm probe           # Check WAA server
    oa evals vm logs            # View logs
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

# Default VM configuration
DEFAULT_VM_NAME = "waa-eval-vm"
DEFAULT_RESOURCE_GROUP = "openadapt-agents"
DEFAULT_VM_SIZE = "Standard_D8ds_v5"
DEFAULT_LOCATION = "eastus"


def _run_az(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    """Run Azure CLI command."""
    full_cmd = ["az"] + cmd
    logger.debug(f"Running: {' '.join(full_cmd)}")
    return subprocess.run(
        full_cmd,
        capture_output=capture,
        text=True,
    )


def _get_vm_ip(vm_name: str = DEFAULT_VM_NAME, resource_group: str = DEFAULT_RESOURCE_GROUP) -> str | None:
    """Get VM public IP address."""
    result = _run_az([
        "vm", "show",
        "-n", vm_name,
        "-g", resource_group,
        "-d",
        "--query", "publicIps",
        "-o", "tsv",
    ])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _get_vm_status(vm_name: str = DEFAULT_VM_NAME, resource_group: str = DEFAULT_RESOURCE_GROUP) -> dict | None:
    """Get VM status."""
    result = _run_az([
        "vm", "show",
        "-n", vm_name,
        "-g", resource_group,
        "-d",
        "-o", "json",
    ])
    if result.returncode == 0:
        return json.loads(result.stdout)
    return None


def cmd_setup(args: argparse.Namespace) -> int:
    """Full VM setup with WAA."""
    vm_name = getattr(args, "vm_name", DEFAULT_VM_NAME)
    resource_group = getattr(args, "resource_group", DEFAULT_RESOURCE_GROUP)
    vm_size = getattr(args, "vm_size", DEFAULT_VM_SIZE)
    location = getattr(args, "location", DEFAULT_LOCATION)

    print(f"Setting up Azure VM '{vm_name}' with WAA...")
    print(f"  Resource group: {resource_group}")
    print(f"  VM size: {vm_size}")
    print(f"  Location: {location}")

    # Check if VM already exists
    status = _get_vm_status(vm_name, resource_group)
    if status:
        power_state = status.get("powerState", "unknown")
        print(f"  VM already exists (state: {power_state})")
        if power_state != "VM running":
            print("  Starting VM...")
            _run_az(["vm", "start", "-n", vm_name, "-g", resource_group])
        return 0

    # Create VM
    print("  Creating VM (this may take a few minutes)...")
    result = _run_az([
        "vm", "create",
        "-n", vm_name,
        "-g", resource_group,
        "--image", "Ubuntu2204",
        "--size", vm_size,
        "--location", location,
        "--admin-username", "azureuser",
        "--generate-ssh-keys",
        "--public-ip-sku", "Standard",
    ])

    if result.returncode != 0:
        print(f"ERROR: Failed to create VM: {result.stderr}")
        return 1

    print("  VM created successfully!")

    # Get IP
    ip = _get_vm_ip(vm_name, resource_group)
    if ip:
        print(f"  Public IP: {ip}")

    # Install Docker and setup WAA
    print("  Installing Docker and WAA (this may take 10-15 minutes)...")
    setup_script = """
set -e
sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER
sudo docker pull windowsarena/winarena:latest
echo "WAA image pulled successfully"
"""

    result = _run_az([
        "vm", "run-command", "invoke",
        "-n", vm_name,
        "-g", resource_group,
        "--command-id", "RunShellScript",
        "--scripts", setup_script,
    ])

    if result.returncode != 0:
        print(f"WARNING: Setup script may have failed: {result.stderr}")
    else:
        print("  Docker and WAA installed!")

    print("\nSetup complete! Next steps:")
    print("  oa evals vm status          # Check VM status")
    print("  oa evals vm probe --wait    # Wait for WAA server")
    print("  oa evals run --agent gpt-4o # Run evaluation")

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show VM status."""
    status = _get_vm_status()
    if not status:
        print("VM not found or not accessible")
        return 1

    print(f"VM: {status.get('name', 'unknown')}")
    print(f"  State: {status.get('powerState', 'unknown')}")
    print(f"  Size: {status.get('hardwareProfile', {}).get('vmSize', 'unknown')}")
    print(f"  Location: {status.get('location', 'unknown')}")

    ip = _get_vm_ip()
    if ip:
        print(f"  Public IP: {ip}")

    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Start deallocated VM."""
    print("Starting VM...")
    result = _run_az(["vm", "start", "-n", DEFAULT_VM_NAME, "-g", DEFAULT_RESOURCE_GROUP])
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        return 1
    print("VM started!")
    return cmd_status(args)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop VM."""
    print("Stopping VM...")
    result = _run_az(["vm", "stop", "-n", DEFAULT_VM_NAME, "-g", DEFAULT_RESOURCE_GROUP])
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        return 1
    print("VM stopped!")
    return 0


def cmd_deallocate(args: argparse.Namespace) -> int:
    """Deallocate VM (stops billing)."""
    print("Deallocating VM (this stops billing)...")
    result = _run_az(["vm", "deallocate", "-n", DEFAULT_VM_NAME, "-g", DEFAULT_RESOURCE_GROUP])
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        return 1
    print("VM deallocated! Billing stopped.")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete VM and resources."""
    if not getattr(args, "yes", False):
        print(f"This will DELETE VM '{DEFAULT_VM_NAME}' and all associated resources.")
        response = input("Are you sure? (y/N): ")
        if response.lower() != "y":
            print("Cancelled.")
            return 0

    print("Deleting VM...")
    result = _run_az([
        "vm", "delete",
        "-n", DEFAULT_VM_NAME,
        "-g", DEFAULT_RESOURCE_GROUP,
        "--yes",
        "--force-deletion", "true",
    ])
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        return 1
    print("VM deleted!")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Check WAA server status."""
    ip = _get_vm_ip()
    if not ip:
        print("VM not found or no public IP")
        return 1

    import urllib.error
    import urllib.request

    url = f"http://{ip}:5000/probe"
    wait = getattr(args, "wait", False)
    timeout = getattr(args, "timeout", 300)

    if wait:
        print(f"Waiting for WAA server at {url}...")
        import time
        start = time.time()
        while time.time() - start < timeout:
            try:
                response = urllib.request.urlopen(url, timeout=5)
                data = json.loads(response.read())
                print(f"WAA server ready! Status: {data}")
                return 0
            except (urllib.error.URLError, json.JSONDecodeError):
                print(".", end="", flush=True)
                time.sleep(5)
        print(f"\nTimeout after {timeout}s")
        return 1

    try:
        response = urllib.request.urlopen(url, timeout=10)
        data = json.loads(response.read())
        print(f"WAA server status: {data}")
        return 0
    except urllib.error.URLError as e:
        print(f"WAA server not reachable: {e}")
        return 1


def cmd_logs(args: argparse.Namespace) -> int:
    """View container logs."""
    lines = getattr(args, "lines", 100)
    follow = getattr(args, "follow", False)

    cmd = f"docker logs winarena --tail {lines}"
    if follow:
        cmd += " -f"

    ip = _get_vm_ip()
    if not ip:
        print("VM not found")
        return 1

    print(f"Fetching logs from {ip}...")
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"azureuser@{ip}", cmd],
        capture_output=not follow,
        text=True,
    )

    if not follow:
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

    return result.returncode


def cmd_diag(args: argparse.Namespace) -> int:
    """Show VM diagnostic info."""
    ip = _get_vm_ip()
    if not ip:
        print("VM not found")
        return 1

    print(f"Running diagnostics on {ip}...")

    diag_cmd = """
echo "=== Disk Usage ==="
df -h /mnt /var/lib/docker 2>/dev/null || df -h
echo ""
echo "=== Docker Status ==="
docker ps -a
echo ""
echo "=== Docker Images ==="
docker images
echo ""
echo "=== Memory ==="
free -h
"""

    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"azureuser@{ip}", diag_cmd],
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result.returncode


def cmd_ssh(args: argparse.Namespace) -> int:
    """Open SSH session to VM."""
    ip = _get_vm_ip()
    if not ip:
        print("VM not found")
        return 1

    print(f"Connecting to {ip}...")
    return subprocess.call(["ssh", "-o", "StrictHostKeyChecking=no", f"azureuser@{ip}"])


def cmd_vnc(args: argparse.Namespace) -> int:
    """Open VNC viewer."""
    ip = _get_vm_ip()
    if not ip:
        print("VM not found")
        return 1

    # Start SSH tunnel for VNC
    print(f"Starting SSH tunnel to {ip}:8006...")
    print("VNC will be available at http://localhost:8006")
    print("Press Ctrl+C to stop the tunnel")

    try:
        subprocess.call([
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-L", "8006:localhost:8006",
            f"azureuser@{ip}",
            "-N",
        ])
    except KeyboardInterrupt:
        print("\nTunnel closed.")

    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    """Run command on VM."""
    cmd = getattr(args, "cmd", None)
    if not cmd:
        print("ERROR: --cmd is required")
        return 1

    ip = _get_vm_ip()
    if not ip:
        print("VM not found")
        return 1

    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"azureuser@{ip}", cmd],
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result.returncode


def cmd_monitor(args: argparse.Namespace) -> int:
    """Start monitoring dashboard."""
    print("Starting monitoring dashboard...")
    print("This feature requires the full infrastructure setup.")
    print("For now, use individual commands:")
    print("  oa evals vm status   # Check VM status")
    print("  oa evals vm probe    # Check WAA server")
    print("  oa evals vm logs     # View logs")
    print("  oa evals vm vnc      # Open VNC viewer")

    # Show quick status
    return cmd_status(args)
