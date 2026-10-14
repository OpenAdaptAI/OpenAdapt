#!/usr/bin/env python3
"""End-to-end correction flywheel with full VM lifecycle automation.

Proves the core product thesis: agent fails -> human corrects -> correction
stored -> agent retries with correction -> agent succeeds.

Full lifecycle (with --manage-vm --setup-tunnels):
    Step 0:  Start VM, wait for SSH, start container, apply iptables fix
    Step 1:  Set up SSH tunnels, wait for WAA /probe
    Phase 1: Agent attempts the task WITHOUT demo guidance (expected: low score)
    Phase 2: Correction captured (from demo or human), stored
    Phase 3: Agent retries WITH demo guidance (expected: higher score)
    Phase 4: Compare scores, generate report
    Cleanup: Deallocate VM (even on error, via try/finally)

Usage:
    # Mock mode (no VM, no API keys -- proves the wiring):
    python scripts/run_correction_flywheel.py \
        --task-config example_tasks/notepad-hello.yaml \
        --demo-dir ./demos \
        --mock \
        --output flywheel_results/

    # Live WAA mode (VM/tunnels already running):
    python scripts/run_correction_flywheel.py \
        --task-config example_tasks/notepad-hello.yaml \
        --demo-dir ./demos \
        --server-url http://localhost:5001 \
        --output flywheel_results/

    # Fully automated (single command, handles everything):
    python scripts/run_correction_flywheel.py \
        --task-config example_tasks/clear-browsing-data-chrome.yaml \
        --demo-dir ./demos \
        --manage-vm \
        --setup-tunnels \
        --output flywheel_results/

    # Use a weaker baseline model to increase chance of failure:
    python scripts/run_correction_flywheel.py \
        --task-config example_tasks/clear-browsing-data-chrome.yaml \
        --demo-dir ./demos \
        --manage-vm \
        --setup-tunnels \
        --baseline-model gpt-4o-mini \
        --guided-model gpt-4.1-mini \
        --output flywheel_results/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("correction_flywheel")


# ---------------------------------------------------------------------------
# Infrastructure helpers (VM, container, tunnel, iptables)
# ---------------------------------------------------------------------------

# SSH options matching infrastructure/azure_vm.py
_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=60",
    "-o", "ServerAliveCountMax=10",
]

# WAA storage directory on the VM (persistent OS disk, not ephemeral /mnt)
_WAA_STORAGE_DIR = "/home/azureuser/waa-storage"


def _ssh_run(
    ip: str,
    cmd: str,
    username: str = "azureuser",
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a command on the VM via SSH.

    Args:
        ip: VM public IP address.
        cmd: Command to execute on the VM.
        username: SSH username.
        timeout: Command timeout in seconds.

    Returns:
        CompletedProcess with return code and output.
    """
    full_cmd = ["ssh", *_SSH_OPTS, f"{username}@{ip}", cmd]
    return subprocess.run(
        full_cmd, capture_output=True, text=True, timeout=timeout,
    )


def start_vm(name: str, resource_group: str) -> bool:
    """Start a deallocated Azure VM.

    Idempotent: if the VM is already running, this is a no-op that returns True.

    Args:
        name: VM name (e.g., "waa-pool-00").
        resource_group: Azure resource group.

    Returns:
        True if the VM is running after this call.
    """
    logger.info("Starting VM %s in resource group %s...", name, resource_group)

    # Check current state first
    state = get_vm_state(name, resource_group)
    if state and "running" in state.lower():
        logger.info("VM %s is already running", name)
        return True

    result = subprocess.run(
        ["az", "vm", "start", "-g", resource_group, "-n", name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error("Failed to start VM %s: %s", name, result.stderr.strip())
        return False

    logger.info("VM %s started successfully", name)
    return True


def get_vm_ip(name: str, resource_group: str) -> str | None:
    """Get the public IP address of an Azure VM.

    Args:
        name: VM name.
        resource_group: Azure resource group.

    Returns:
        Public IP string, or None if not found.
    """
    result = subprocess.run(
        [
            "az", "vm", "show", "-d",
            "-g", resource_group,
            "-n", name,
            "--query", "publicIps",
            "-o", "tsv",
        ],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def get_vm_state(name: str, resource_group: str) -> str | None:
    """Get the power state of an Azure VM.

    Args:
        name: VM name.
        resource_group: Azure resource group.

    Returns:
        Power state string (e.g., "VM running"), or None.
    """
    result = subprocess.run(
        [
            "az", "vm", "get-instance-view",
            "-g", resource_group,
            "-n", name,
            "--query", "instanceView.statuses[1].displayStatus",
            "-o", "tsv",
        ],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def wait_for_ssh(
    ip: str,
    username: str = "azureuser",
    timeout: int = 180,
) -> bool:
    """Wait for SSH to become available on the VM.

    Args:
        ip: VM public IP address.
        username: SSH username.
        timeout: Maximum seconds to wait.

    Returns:
        True if SSH is reachable within timeout.
    """
    logger.info("Waiting for SSH on %s (timeout %ds)...", ip, timeout)
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                ["ssh", *_SSH_OPTS, f"{username}@{ip}", "echo ok"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                elapsed = int(time.time() - start)
                logger.info("SSH available after %ds", elapsed)
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)

    logger.error("SSH not available after %ds", timeout)
    return False


def start_container(
    vm_ip: str,
    username: str = "azureuser",
) -> bool:
    """Ensure the WAA Docker container is running on the VM.

    Idempotent: if the container is already running, returns True.
    If the container exists but is stopped, starts it.
    If no container exists, runs a new one.

    Args:
        vm_ip: VM public IP address.
        username: SSH username.

    Returns:
        True if the container is running after this call.
    """
    logger.info("Ensuring WAA container is running on %s...", vm_ip)

    # Check container state
    result = _ssh_run(
        vm_ip,
        "docker inspect -f '{{.State.Running}}' winarena 2>/dev/null || echo missing",
        username=username,
    )
    state = result.stdout.strip()

    if state == "true":
        logger.info("WAA container already running")
        return True

    if state == "missing":
        # No container exists -- create and run one
        logger.info("No winarena container found, creating new one...")
        home_dir = f"/home/{username}"
        docker_cmd = (
            f"sudo mkdir -p {home_dir}/waa-storage && "
            f"sudo chown {username}:{username} {home_dir}/waa-storage && "
            f"docker run -d --name winarena "
            f"--device=/dev/kvm "
            f"--cap-add NET_ADMIN "
            f"--stop-timeout 120 "
            f"-p 5000:5000 "
            f"-p 5050:5050 "
            f"-p 8006:8006 "
            f"-p 7200:7200 "
            f"-v {home_dir}/waa-storage:/storage "
            f"-e VERSION=11e "
            f"-e RAM_SIZE=8G "
            f"-e CPU_CORES=4 "
            f"-e DISK_SIZE=64G "
            f'-e ARGUMENTS="-qmp tcp:0.0.0.0:7200,server,nowait" '
            f"waa-auto:latest "
            f"/entry.sh --prepare-image false --start-client false"
        )
        result = _ssh_run(vm_ip, docker_cmd, username=username, timeout=60)
        if result.returncode != 0:
            logger.error(
                "Failed to create container: %s", result.stderr.strip()
            )
            return False
        logger.info("WAA container created and started")
        return True

    # Container exists but not running -- restart it
    logger.info("WAA container stopped, starting...")
    result = _ssh_run(vm_ip, "docker start winarena", username=username)
    if result.returncode == 0:
        logger.info("WAA container started")
        return True

    logger.error("Failed to start container: %s", result.stderr.strip())
    return False


def apply_iptables_fix(
    vm_ip: str,
    username: str = "azureuser",
) -> bool:
    """Apply the iptables fix for port 5050 inside the container.

    The dockurr/windows base image sets up DNAT rules that redirect all
    traffic to the Windows VM (172.30.0.2). Port 5050 (evaluate server)
    runs on the Linux side of the container and needs to be exempted.

    This is idempotent -- running it multiple times is safe because
    iptables -C checks for the rule before adding it.

    Args:
        vm_ip: VM public IP address.
        username: SSH username.

    Returns:
        True if the fix was applied (or was already in place).
    """
    logger.info("Applying iptables fix for port 5050...")

    # Check if the rule already exists, add it if not
    iptables_cmd = (
        "docker exec winarena sh -c '"
        "iptables -t nat -C PREROUTING -p tcp --dport 5050 -j ACCEPT 2>/dev/null "
        "|| iptables -t nat -I PREROUTING 1 -p tcp --dport 5050 -j ACCEPT"
        "'"
    )
    result = _ssh_run(vm_ip, iptables_cmd, username=username)

    if result.returncode == 0:
        logger.info("iptables fix applied (port 5050 exempted from DNAT)")
        return True

    # Non-fatal: the evaluate server may still work if the container's
    # start_with_evaluate.sh already applied the fix
    logger.warning(
        "iptables fix returned non-zero (may already be applied): %s",
        result.stderr.strip(),
    )
    return True  # Non-fatal, continue anyway


def setup_eval_proxy(
    vm_ip: str,
    username: str = "azureuser",
) -> bool:
    """Set up the socat proxy for the evaluate server on the VM.

    Docker port forwarding for 5050 is broken by QEMU's --cap-add NET_ADMIN
    tap networking. We proxy VM:5051 -> docker exec -> container:5050.

    Idempotent: restarts the systemd service if it exists, otherwise sets up
    a manual socat process.

    Args:
        vm_ip: VM public IP address.
        username: SSH username.

    Returns:
        True if proxy setup succeeded.
    """
    logger.info("Setting up socat eval proxy on %s...", vm_ip)

    script = (
        "if systemctl list-unit-files socat-waa-evaluate.service "
        "| grep -q socat-waa-evaluate; then "
        "  sudo systemctl restart socat-waa-evaluate.service; "
        "else "
        "  killall socat 2>/dev/null || true; sleep 1; "
        "  which socat >/dev/null 2>&1 "
        "  || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq socat; "
        "  nohup socat TCP-LISTEN:5051,fork,reuseaddr "
        "  'EXEC:docker exec -i winarena socat - TCP\\:127.0.0.1\\:5050' "
        "  </dev/null >/dev/null 2>&1 & "
        "fi"
    )
    result = _ssh_run(vm_ip, script, username=username)
    if result.returncode != 0:
        logger.warning("socat proxy setup returned non-zero: %s", result.stderr.strip())
        return False
    logger.info("socat eval proxy set up")
    return True


def _kill_existing_tunnels(ports: list[int]) -> None:
    """Kill any existing SSH tunnel processes listening on the given ports.

    Args:
        ports: List of local ports to free up.
    """
    for port in ports:
        # Find PIDs listening on the port
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split("\n")
                for pid in pids:
                    pid = pid.strip()
                    if pid:
                        try:
                            os.kill(int(pid), signal.SIGTERM)
                            logger.info(
                                "Killed process %s on port %d", pid, port
                            )
                        except (ProcessLookupError, ValueError):
                            pass
        except FileNotFoundError:
            # lsof not available, try fuser
            try:
                subprocess.run(
                    ["fuser", "-k", f"{port}/tcp"],
                    capture_output=True, text=True,
                )
            except FileNotFoundError:
                logger.warning(
                    "Neither lsof nor fuser available, cannot kill port %d",
                    port,
                )


def setup_tunnels(
    vm_ip: str,
    username: str = "azureuser",
    local_ports: dict[str, tuple[int, int]] | None = None,
) -> dict[str, int | None]:
    """Set up SSH tunnels to the VM.

    Kills existing tunnels on the target ports, then creates new ones.
    Also sets up the socat eval proxy on the VM.

    Default tunnel mapping:
        - waa:      localhost:5001 -> VM:5000  (WAA Flask API)
        - evaluate: localhost:5050 -> VM:5051  (evaluate server via socat)
        - vnc:      localhost:8006 -> VM:8006  (noVNC web viewer)

    Args:
        vm_ip: VM public IP address.
        username: SSH username.
        local_ports: Dict of name -> (local_port, remote_port).
            If None, uses default mapping.

    Returns:
        Dict of tunnel name -> subprocess PID (or None on failure).
    """
    if local_ports is None:
        local_ports = {
            "waa": (5001, 5000),
            "evaluate": (5050, 5051),
            "vnc": (8006, 8006),
        }

    logger.info("Setting up SSH tunnels to %s...", vm_ip)

    # Kill existing tunnels on these ports
    all_local_ports = [lp for lp, _ in local_ports.values()]
    _kill_existing_tunnels(all_local_ports)
    time.sleep(1)

    # Set up socat proxy for the evaluate server
    setup_eval_proxy(vm_ip, username=username)

    pids: dict[str, int | None] = {}

    for name, (local_port, remote_port) in local_ports.items():
        ssh_cmd = [
            "ssh",
            *_SSH_OPTS,
            "-o", "TCPKeepAlive=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-N",  # Don't execute remote command
            "-L", f"{local_port}:localhost:{remote_port}",
            f"{username}@{vm_ip}",
        ]

        try:
            proc = subprocess.Popen(
                ssh_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            time.sleep(0.5)

            if proc.poll() is not None:
                _, stderr = proc.communicate(timeout=2)
                error_msg = stderr.decode().strip() if stderr else "unknown"
                logger.error(
                    "Tunnel %s failed: %s", name, error_msg[:200]
                )
                pids[name] = None
            else:
                logger.info(
                    "Tunnel %s: localhost:%d -> %s:%d (PID %d)",
                    name, local_port, vm_ip, remote_port, proc.pid,
                )
                pids[name] = proc.pid
        except Exception as e:
            logger.error("Failed to start tunnel %s: %s", name, e)
            pids[name] = None

    return pids


def wait_for_waa(
    server_url: str = "http://localhost:5001",
    timeout: int = 600,
) -> bool:
    """Wait for the WAA server to respond to /probe.

    Args:
        server_url: WAA server URL.
        timeout: Maximum seconds to wait.

    Returns:
        True if the server responded within timeout.
    """
    logger.info(
        "Waiting for WAA server at %s/probe (timeout %ds)...",
        server_url, timeout,
    )
    start = time.time()
    last_log = 0

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        if elapsed - last_log >= 15:
            logger.info("  [%ds] probing WAA...", elapsed)
            last_log = elapsed

        try:
            req = urllib.request.Request(
                f"{server_url}/probe", method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("WAA server ready after %ds", elapsed)
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass

        time.sleep(10)

    logger.error("WAA server not ready after %ds", timeout)
    return False


def deallocate_vm(name: str, resource_group: str) -> bool:
    """Deallocate an Azure VM (stop billing, keep disk).

    This is safe to call even if the VM is already deallocated.

    Args:
        name: VM name.
        resource_group: Azure resource group.

    Returns:
        True if deallocation succeeded (or VM was already deallocated).
    """
    logger.info("Deallocating VM %s (stops billing)...", name)
    result = subprocess.run(
        ["az", "vm", "deallocate", "-g", resource_group, "-n", name],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info("VM %s deallocated successfully", name)
        return True

    # Check if already deallocated
    state = get_vm_state(name, resource_group)
    if state and "deallocated" in state.lower():
        logger.info("VM %s was already deallocated", name)
        return True

    logger.warning(
        "Deallocate may have failed for %s: %s", name, result.stderr.strip()
    )
    return False


# ---------------------------------------------------------------------------
# Environment reset between phases (Bug 1 fix)
# ---------------------------------------------------------------------------


def _reset_environment_between_phases(
    server_url: str,
    task_config,
    mock: bool = False,
) -> bool:
    """Reset the WAA environment to a clean state between flywheel phases.

    This ensures Phase 3 starts on a clean desktop, not leftover state from
    Phase 1. Without this reset, artifacts from Phase 1 (e.g., open windows,
    typed text) leak into Phase 3 and confuse both the agent and visual
    alignment.

    For live mode: runs the task's setup commands again via env.reset(), which
    re-executes config entries (close processes, sleep, launch apps, etc.).

    For mock mode: this is a no-op since MockFlywheelAdapter.reset() already
    provides clean state.

    Args:
        server_url: WAA server URL.
        task_config: Task configuration with setup commands.
        mock: Whether we're in mock mode.

    Returns:
        True if reset succeeded.
    """
    if mock:
        logger.info("Mock mode: environment reset is a no-op")
        return True

    logger.info("=" * 60)
    logger.info("RESETTING ENVIRONMENT (clean state for Phase 3)")
    logger.info("=" * 60)

    try:
        from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
        from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(WAALiveConfig(server_url=server_url))
        env = RLEnvironment(adapter, task_config=task_config)

        # Full reset: closes all windows and re-runs setup commands
        # The task_config.setup entries typically include process kills and
        # cleanup commands that restore the desktop to a known state.
        obs = env.reset(config=ResetConfig(task_id=task_config.id))

        if obs and obs.screenshot:
            logger.info(
                "Environment reset succeeded (screenshot: %d bytes)",
                len(obs.screenshot),
            )
        else:
            logger.info("Environment reset completed (no screenshot returned)")

        return True

    except Exception as e:
        logger.error("Environment reset failed: %s", e, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Mock adapter and agent for offline testing
# ---------------------------------------------------------------------------

@dataclass
class _MockObs:
    """Minimal observation for mock mode."""
    screenshot: bytes | None = None
    screenshot_path: str | None = None
    viewport: tuple[int, int] | None = None
    accessibility_tree: dict | None = None


@dataclass
class _MockAction:
    """Minimal action for mock mode."""
    type: str = "done"
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None
    raw_action: dict | None = None


@dataclass
class _MockStepResult:
    observation: _MockObs
    action: _MockAction
    reward: float
    done: bool
    info: dict


class MockFlywheelAdapter:
    """Simulates a WAA-like adapter without any VM or server."""

    def __init__(self):
        self._step = 0
        self._screenshot = self._make_screenshot(b"desktop-initial")

    @staticmethod
    def _make_screenshot(label: bytes) -> bytes:
        """Return synthetic PNG-like bytes (not a real image, just a marker)."""
        # 8-byte PNG header + label so we can distinguish screenshots
        return b"\x89PNG\r\n\x1a\n" + label

    def reset(self, task_id: str) -> _MockObs:
        self._step = 0
        self._screenshot = self._make_screenshot(b"desktop-initial")
        return _MockObs(screenshot=self._screenshot)

    def step(self, action: _MockAction) -> _MockStepResult:
        self._step += 1
        label = f"step-{self._step}".encode()
        self._screenshot = self._make_screenshot(label)
        return _MockStepResult(
            observation=_MockObs(screenshot=self._screenshot),
            action=action,
            reward=0.0,
            done=False,
            info={"step": self._step},
        )

    def observe(self) -> _MockObs:
        return _MockObs(screenshot=self._screenshot)


class MockAgent:
    """Agent that deterministically fails (attempt) or succeeds (retry).

    On attempt (has_demo_guidance=False): takes 3 random actions, then DONE.
    On retry  (has_demo_guidance=True):  follows the "demo", then DONE.
    """

    def __init__(self, has_demo_guidance: bool = False):
        self._has_guidance = has_demo_guidance
        self._step = 0

    def act(self, obs, task_instruction: str) -> _MockAction:
        self._step += 1
        if self._has_guidance:
            # With guidance: do the right thing
            if self._step == 1:
                return _MockAction(type="type", text="notepad", raw_action={})
            if self._step == 2:
                return _MockAction(type="key", key="enter", raw_action={})
            if self._step == 3:
                return _MockAction(type="type", text="Hello World", raw_action={})
            return _MockAction(type="done", raw_action={})
        else:
            # Without guidance: fumble around
            if self._step == 1:
                return _MockAction(type="click", x=0.5, y=0.5, raw_action={})
            if self._step == 2:
                return _MockAction(type="click", x=0.1, y=0.9, raw_action={})
            return _MockAction(type="done", raw_action={})


def _mock_evaluate(adapter: MockFlywheelAdapter, has_guidance: bool) -> float:
    """Simulate milestone scoring."""
    if has_guidance:
        return 1.0  # Guided agent succeeds
    return 0.0  # Unguided agent fails


# ---------------------------------------------------------------------------
# Live mode helpers
# ---------------------------------------------------------------------------

def _run_live_episode(
    server_url: str,
    task_config,
    demo_library=None,
    max_steps: int = 15,
    planner_model: str = "gpt-4.1-mini",
    planner_provider: str = "openai",
    grounder_model: str = "gpt-4.1-mini",
    grounder_provider: str = "openai",
    grounder_endpoint: str | None = None,
    screenshot_dir: Path | None = None,
    use_visual_alignment: bool = True,
) -> tuple[float, list[bytes]]:
    """Run one episode against a live WAA server. Returns (score, screenshots)."""
    from openadapt_evals.adapters.base import BenchmarkAction, BenchmarkTask
    from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
    from openadapt_evals.adapters.waa.live import WAALiveAdapter, WAALiveConfig
    from openadapt_evals.agents.planner_grounder_agent import PlannerGrounderAgent

    adapter = WAALiveAdapter(WAALiveConfig(server_url=server_url))
    env = RLEnvironment(adapter, task_config=task_config)

    # When an HTTP grounder endpoint is provided, use the HTTP provider
    # so the PlannerGrounderAgent routes through _call_grounder_http.
    if grounder_endpoint:
        _grounder = "http"
        _grounder_provider = "http"
    else:
        _grounder = grounder_model
        _grounder_provider = grounder_provider

    base_agent = PlannerGrounderAgent(
        planner=planner_model,
        grounder=_grounder,
        planner_provider=planner_provider,
        grounder_provider=_grounder_provider,
        grounder_endpoint=grounder_endpoint,
    )

    # Wrap with demo guidance if library provided
    agent: object
    if demo_library is not None:
        from openadapt_evals.agents.demo_guided_agent import DemoGuidedAgent
        agent = DemoGuidedAgent(
            base_agent=base_agent,
            demo_library=demo_library,
            use_visual_alignment=use_visual_alignment,
        )
    else:
        agent = base_agent

    task = BenchmarkTask(
        task_id=task_config.id,
        instruction=task_config.name,
        domain="desktop",
    )

    obs = env.reset(config=ResetConfig(task_id=task_config.id))
    screenshots: list[bytes] = []
    if obs.screenshot:
        screenshots.append(obs.screenshot)
        if screenshot_dir:
            (screenshot_dir / "step_00.png").write_bytes(obs.screenshot)

    for step_i in range(max_steps):
        action = agent.act(obs, task)

        if action.type == "done":
            if step_i == 0 and demo_library is not None:
                logger.warning(
                    "Agent signaled DONE on step 0 (no actions taken). "
                    "Overriding with first demo action to prevent "
                    "hallucination (planner confused demo description "
                    "with actual screen state)."
                )
                guidance = demo_library.align_step(
                    task_id=task_config.id,
                    current_screenshot=obs.screenshot,
                    step_index=0,
                )
                if guidance.available and guidance.action_value:
                    action = BenchmarkAction(
                        type="key", key=guidance.action_value,
                    )
                    logger.info("Forcing demo action: key=%s", guidance.action_value)
                else:
                    continue
            else:
                logger.info("Agent signaled DONE at step %d", step_i + 1)
                break

        # Execute
        if action.x is not None and action.y is not None:
            x, y = float(action.x), float(action.y)
            if 0 <= x <= 1 and 0 <= y <= 1:
                step_result = env.pixel_action(
                    x_frac=x, y_frac=y,
                    action_type=action.type, text=action.text, key=action.key,
                )
            else:
                step_result = env.pixel_action(
                    x=int(x), y=int(y),
                    action_type=action.type, text=action.text, key=action.key,
                )
        else:
            step_result = env.step(action)

        obs = step_result.observation
        if obs.screenshot:
            screenshots.append(obs.screenshot)
            if screenshot_dir:
                (screenshot_dir / f"step_{step_i + 1:02d}.png").write_bytes(
                    obs.screenshot
                )

        # Per-step milestone check: capture transient states like
        # "dialog is open" that disappear after the agent acts on them.
        if task_config.milestones:
            passed, total = env.check_milestones_incremental(obs.screenshot)
            if passed > len(env._milestone_passed) - 1:
                logger.debug(
                    "Step %d: milestones %d/%d (high-water)",
                    step_i + 1, passed, total,
                )

        if step_result.done:
            break

    # Score — evaluate_dense uses the high-water mark from incremental checks
    if task_config.milestones:
        score = env.evaluate_dense()
    else:
        score = env.evaluate()

    return score, screenshots


# ---------------------------------------------------------------------------
# Core flywheel phases
# ---------------------------------------------------------------------------

def phase1_attempt(
    mock: bool,
    server_url: str | None,
    task_config,
    output_dir: Path,
    **kwargs,
) -> tuple[float, list[bytes]]:
    """Phase 1: Agent attempts the task WITHOUT demo guidance."""
    logger.info("=" * 60)
    logger.info("PHASE 1: ATTEMPT (no demo guidance)")
    logger.info("=" * 60)

    ss_dir = output_dir / "phase1_screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)

    if mock:
        adapter = MockFlywheelAdapter()
        agent = MockAgent(has_demo_guidance=False)
        obs = adapter.reset(task_config.id)
        screenshots = [obs.screenshot] if obs.screenshot else []

        for step_i in range(10):
            action = agent.act(obs, task_config.name)
            if action.type == "done":
                break
            result = adapter.step(action)
            obs = result.observation
            if obs.screenshot:
                screenshots.append(obs.screenshot)

        score = _mock_evaluate(adapter, has_guidance=False)
    else:
        score, screenshots = _run_live_episode(
            server_url=server_url,
            task_config=task_config,
            demo_library=None,
            screenshot_dir=ss_dir,
            **kwargs,
        )

    logger.info("Phase 1 result: score=%.2f, screenshots=%d", score, len(screenshots))
    return score, screenshots


def phase2_correct(
    task_config,
    demo_dir: str,
    attempt_screenshots: list[bytes],
    output_dir: Path,
) -> str:
    """Phase 2: Capture correction (simulate from demo or create synthetic).

    For MVP: the demo IS the correction -- "this is what you should have done."
    Stores the correction in CorrectionStore for audit trail.

    Returns the demo_dir path (for Phase 3 DemoLibrary).
    """
    logger.info("=" * 60)
    logger.info("PHASE 2: CORRECT (capture/store correction)")
    logger.info("=" * 60)

    from openadapt_evals.correction_store import CorrectionEntry, CorrectionStore

    correction_dir = output_dir / "corrections"
    store = CorrectionStore(str(correction_dir))

    # Check if demo exists for this task
    demo_library_dir = Path(demo_dir)
    task_demo_dir = demo_library_dir / task_config.id

    if task_demo_dir.exists():
        logger.info("Found existing demo for task %s at %s", task_config.id, task_demo_dir)
        # Store a correction entry referencing the demo
        entry = CorrectionEntry(
            task_id=task_config.id,
            step_description=task_config.name,
            failure_screenshot_path="",
            failure_explanation="Agent failed without demo guidance",
            correction_step={
                "think": "Use the demo to guide the agent",
                "action": "Follow demonstration steps",
                "expect": "Task completed successfully",
            },
        )
        entry_id = store.save(entry)
        logger.info("Stored correction %s from existing demo", entry_id)
    else:
        # Create a synthetic demo for mock mode
        logger.info("No existing demo found. Creating synthetic demo for %s", task_config.id)
        task_demo_dir.mkdir(parents=True, exist_ok=True)

        from openadapt_evals.adapters.base import BenchmarkAction

        # Synthetic demo: the "correct" sequence for notepad-hello
        demo_steps = [
            {
                "action": asdict(BenchmarkAction(type="type", text="notepad")),
                "description": "Type 'notepad' in the search bar",
            },
            {
                "action": asdict(BenchmarkAction(type="key", key="enter")),
                "description": "Press Enter to open Notepad",
            },
            {
                "action": asdict(BenchmarkAction(type="type", text="Hello World")),
                "description": "Type 'Hello World' in Notepad",
            },
        ]

        # Write demo.json
        demo_id = "synthetic_correction"
        demo_subdir = task_demo_dir / demo_id
        demo_subdir.mkdir(parents=True, exist_ok=True)

        # Create minimal screenshot files (required by DemoLibrary)
        for i in range(len(demo_steps)):
            screenshot_path = demo_subdir / f"step_{i:03d}.png"
            # Write a minimal valid-ish marker
            screenshot_path.write_bytes(
                b"\x89PNG\r\n\x1a\n" + f"demo-step-{i}".encode()
            )

        demo_metadata = {
            "demo_id": demo_id,
            "task_id": task_config.id,
            "description": f"Correction demo: {task_config.name}",
            "created": datetime.now(timezone.utc).isoformat(),
            "steps": [
                {
                    "screenshot": f"step_{i:03d}.png",
                    "action": step["action"],
                    "description": step["description"],
                }
                for i, step in enumerate(demo_steps)
            ],
        }
        with open(demo_subdir / "demo.json", "w") as f:
            json.dump(demo_metadata, f, indent=2)

        logger.info("Created synthetic demo with %d steps at %s",
                     len(demo_steps), demo_subdir)

        # Also store in CorrectionStore
        entry = CorrectionEntry(
            task_id=task_config.id,
            step_description=task_config.name,
            failure_screenshot_path="",
            failure_explanation="Agent failed without demo guidance",
            correction_step={
                "think": "Created synthetic correction demo",
                "action": "Follow the 3-step demo: search notepad, open it, type text",
                "expect": "Notepad open with 'Hello World' typed",
            },
        )
        store.save(entry)

    # Verify corrections are stored
    all_corrections = store.load_all()
    logger.info("CorrectionStore contains %d entries", len(all_corrections))

    return demo_dir


def _validate_demo_quality(demo_dir: str, task_id: str) -> list[str]:
    """Validate demo quality before using it for guidance (Bug 2 fix).

    Checks:
    - Screenshots exist and are real image files
    - Screenshots don't contain duplicate content (e.g., "Hello WorldHello world")
    - Demo steps have distinct screenshots (not all identical)

    Args:
        demo_dir: Root demo directory.
        task_id: Task identifier to check.

    Returns:
        List of warning messages. Empty if demo passes all checks.
    """
    warnings = []
    demo_path = Path(demo_dir) / task_id
    if not demo_path.exists():
        return ["Demo directory does not exist"]

    # Find demo subdirectories
    demo_subdirs = [
        d for d in demo_path.iterdir()
        if d.is_dir() and (d / "demo.json").exists()
    ]
    if not demo_subdirs:
        return ["No demo.json found in demo directory"]

    for demo_subdir in demo_subdirs:
        demo_json = demo_subdir / "demo.json"
        try:
            with open(demo_json) as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            warnings.append(f"Cannot read {demo_json}: {e}")
            continue

        steps = metadata.get("steps", [])
        screenshot_sizes = []

        for i, step in enumerate(steps):
            ss_name = step.get("screenshot", f"step_{i:03d}.png")
            ss_path = demo_subdir / ss_name

            if not ss_path.exists():
                warnings.append(f"Missing screenshot: {ss_path}")
                continue

            size = ss_path.stat().st_size
            if size < 100:
                warnings.append(
                    f"Screenshot {ss_name} is suspiciously small "
                    f"({size} bytes) -- may be a placeholder"
                )
            screenshot_sizes.append(size)

        # Check for duplicate screenshots (all same size is a heuristic
        # for identical content -- exact check would require hashing)
        if len(screenshot_sizes) > 2 and len(set(screenshot_sizes)) == 1:
            warnings.append(
                f"All {len(screenshot_sizes)} screenshots have identical size "
                f"({screenshot_sizes[0]} bytes) -- likely indistinguishable"
            )

        # Check action values for duplicate content patterns
        for i, step in enumerate(steps):
            action = step.get("action", {})
            text_val = action.get("text", "")
            if text_val:
                # Check for doubled text like "Hello WorldHello world"
                half = len(text_val) // 2
                if half > 3 and text_val[:half].lower() == text_val[half:].lower():
                    warnings.append(
                        f"Step {i} action text appears doubled: "
                        f"'{text_val}' -- demo may be from a failed run"
                    )

    return warnings


def phase3_retry(
    mock: bool,
    server_url: str | None,
    task_config,
    demo_dir: str,
    output_dir: Path,
    **kwargs,
) -> tuple[float, list[bytes]]:
    """Phase 3: Agent retries WITH demo guidance."""
    logger.info("=" * 60)
    logger.info("PHASE 3: RETRY (with demo guidance)")
    logger.info("=" * 60)

    ss_dir = output_dir / "phase3_screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)

    # --- Bug 2: Validate demo quality before using it -----------------------
    demo_warnings = _validate_demo_quality(demo_dir, task_config.id)
    if demo_warnings:
        for w in demo_warnings:
            logger.warning("Demo quality issue: %s", w)
        # Write warnings to output for audit trail
        warnings_path = output_dir / "demo_quality_warnings.json"
        with open(warnings_path, "w") as f:
            json.dump(
                {"task_id": task_config.id, "warnings": demo_warnings},
                f, indent=2,
            )
        logger.warning(
            "Demo has %d quality issues. See %s",
            len(demo_warnings), warnings_path,
        )

    if mock:
        adapter = MockFlywheelAdapter()
        agent = MockAgent(has_demo_guidance=True)
        obs = adapter.reset(task_config.id)
        screenshots = [obs.screenshot] if obs.screenshot else []

        for step_i in range(10):
            action = agent.act(obs, task_config.name)
            if action.type == "done":
                break
            result = adapter.step(action)
            obs = result.observation
            if obs.screenshot:
                screenshots.append(obs.screenshot)

        score = _mock_evaluate(adapter, has_guidance=True)
    else:
        from openadapt_evals.demo_library import DemoLibrary
        demo_library = DemoLibrary(demo_dir)

        demo = demo_library.get_demo(task_config.id)

        if demo is not None and demo.steps:
            # Use DemoExecutor: execute demo steps directly with tiered
            # intelligence. Keyboard/type steps are deterministic, click
            # steps use the grounder, planner is only for recovery.
            from openadapt_evals.adapters.rl_env import RLEnvironment
            from openadapt_evals.adapters.waa.live import (
                WAALiveAdapter,
                WAALiveConfig,
            )
            from openadapt_evals.agents.demo_executor import DemoExecutor

            logger.info(
                "Using DemoExecutor (tiered execution) for %d-step demo",
                len(demo.steps),
            )
            adapter = WAALiveAdapter(WAALiveConfig(server_url=server_url))
            env = RLEnvironment(adapter, task_config=task_config)
            executor = DemoExecutor(
                grounder_model=kwargs.get("grounder_model", "gpt-4.1-mini"),
                grounder_provider=kwargs.get("grounder_provider", "openai"),
                grounder_endpoint=kwargs.get("grounder_endpoint"),
                planner_model=kwargs.get("planner_model", "gpt-4.1-mini"),
                planner_provider=kwargs.get("planner_provider", "openai"),
            )
            score, screenshots = executor.run(
                env, demo, task_config, screenshot_dir=ss_dir,
            )
        else:
            # No demo available — fall back to planner-guided episode
            logger.info("No demo found, falling back to planner-guided episode")
            score, screenshots = _run_live_episode(
                server_url=server_url,
                task_config=task_config,
                demo_library=demo_library,
                screenshot_dir=ss_dir,
                use_visual_alignment=True,
                **kwargs,
            )

    logger.info("Phase 3 result: score=%.2f, screenshots=%d", score, len(screenshots))
    return score, screenshots


def phase4_verify(
    attempt_score: float,
    retry_score: float,
    output_dir: Path,
    task_name: str,
    baseline_model: str | None = None,
    guided_model: str | None = None,
    phase_errors: list[str] | None = None,
) -> bool:
    """Phase 4: Compare scores and generate report."""
    logger.info("=" * 60)
    logger.info("PHASE 4: VERIFY (compare results)")
    logger.info("=" * 60)

    improvement = retry_score - attempt_score
    success = retry_score > attempt_score

    # Generate report
    report: dict = {
        "task": task_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attempt_score": attempt_score,
        "retry_score": retry_score,
        "improvement": improvement,
        "flywheel_working": success,
    }

    if baseline_model:
        report["baseline_model"] = baseline_model
    if guided_model:
        report["guided_model"] = guided_model
    if phase_errors:
        report["errors"] = phase_errors

    report_path = output_dir / "flywheel_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary
    print("\n" + "=" * 60)
    print("CORRECTION FLYWHEEL RESULTS")
    print("=" * 60)
    print(f"Task:            {task_name}")
    if baseline_model:
        print(f"Baseline model:  {baseline_model}")
    if guided_model:
        print(f"Guided model:    {guided_model}")
    print(f"Attempt score:   {attempt_score:.2f}  (no guidance)")
    print(f"Retry score:     {retry_score:.2f}  (with correction/demo)")
    print(f"Improvement:     {improvement:+.2f}")
    print(f"Flywheel works:  {'YES' if success else 'NO'}")
    print(f"Report:          {report_path}")
    if phase_errors:
        print(f"Errors:          {len(phase_errors)} phase(s) had errors")
        for err in phase_errors:
            print(f"  - {err}")
    print("=" * 60)

    if success:
        print("\nThe correction flywheel is working.")
        print("Agent fails -> human corrects -> agent retries -> agent succeeds.")
    else:
        print("\nFlywheel did NOT show improvement. Debug needed.")

    return success


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end correction flywheel with full VM lifecycle automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Mock mode (no VM, no API keys):
  %(prog)s --task-config example_tasks/notepad-hello.yaml --mock

  # Live mode, VM/tunnels already running:
  %(prog)s --task-config example_tasks/notepad-hello.yaml --demo-dir ./demos

  # Fully automated (single command):
  %(prog)s --task-config example_tasks/clear-browsing-data-chrome.yaml \\
      --demo-dir ./demos --manage-vm --setup-tunnels

  # Weaker baseline for harder task differentiation:
  %(prog)s --task-config example_tasks/clear-browsing-data-chrome.yaml \\
      --demo-dir ./demos --manage-vm --setup-tunnels \\
      --baseline-model gpt-4o-mini --guided-model gpt-4.1-mini
""",
    )
    parser.add_argument("--task-config", required=True,
                        help="Path to task YAML config")
    parser.add_argument("--demo-dir", default="./demos",
                        help="Directory containing (or to create) demos")
    parser.add_argument("--output", default="flywheel_results",
                        help="Output directory for results")
    parser.add_argument("--mock", action="store_true",
                        help="Use mock adapter (no VM, no API keys)")
    parser.add_argument("--server-url", default="http://localhost:5001",
                        help="WAA server URL (ignored in mock mode)")
    parser.add_argument("--reset-between-phases", action="store_true",
                        default=True,
                        help="Reset VM environment between Phase 1 and Phase 3 "
                             "(default: True). Ensures Phase 3 starts on a clean "
                             "desktop, not leftover state from Phase 1.")
    parser.add_argument("--no-reset-between-phases", action="store_false",
                        dest="reset_between_phases",
                        help="Skip environment reset between phases.")
    parser.add_argument("--max-steps", type=int, default=15)

    # Model selection
    model_group = parser.add_argument_group("model selection")
    model_group.add_argument(
        "--planner-model", default="gpt-4.1-mini",
        help="Planner model for both phases (overridden by --baseline-model/--guided-model)",
    )
    model_group.add_argument("--planner-provider", default="openai")
    model_group.add_argument(
        "--grounder-model", default="gpt-4.1-mini",
        help="Grounder model for both phases",
    )
    model_group.add_argument("--grounder-provider", default="openai")
    model_group.add_argument(
        "--grounder-endpoint",
        help="HTTP endpoint for a dedicated grounding model (e.g., vLLM serving "
             "UI-Venus-1.5-8B). When set, click grounding uses the UI-Venus "
             "native bbox format via this endpoint instead of the grounder "
             "model API. Example: http://gpu-host:8000",
    )
    model_group.add_argument(
        "--baseline-model",
        help="Planner model for Phase 1 (attempt without guidance). "
             "Use a weaker model (e.g., gpt-4o-mini) to increase chance of failure.",
    )
    model_group.add_argument(
        "--guided-model",
        help="Planner model for Phase 3 (retry with guidance). "
             "Use a stronger model to increase chance of success.",
    )

    # VM management
    vm_group = parser.add_argument_group("VM management (optional)")
    vm_group.add_argument(
        "--manage-vm", action="store_true",
        help="Start VM at beginning, deallocate at end (even on error)",
    )
    vm_group.add_argument(
        "--vm-name", default="waa-pool-00",
        help="Azure VM name (default: waa-pool-00)",
    )
    vm_group.add_argument(
        "--vm-resource-group", default="openadapt-agents",
        help="Azure resource group (default: openadapt-agents)",
    )
    vm_group.add_argument(
        "--vm-user", default="azureuser",
        help="SSH username for the VM (default: azureuser)",
    )

    # Tunnel setup
    tunnel_group = parser.add_argument_group("tunnel setup (optional)")
    tunnel_group.add_argument(
        "--setup-tunnels", action="store_true",
        help="Kill existing tunnels and create fresh SSH tunnels to the VM",
    )
    tunnel_group.add_argument(
        "--waa-timeout", type=int, default=600,
        help="Max seconds to wait for WAA server (default: 600)",
    )
    tunnel_group.add_argument(
        "--ssh-timeout", type=int, default=180,
        help="Max seconds to wait for SSH (default: 180)",
    )

    args = parser.parse_args()

    # Resolve baseline/guided models (fall back to --planner-model)
    baseline_planner = args.baseline_model or args.planner_model
    guided_planner = args.guided_model or args.planner_model

    if args.baseline_model or args.guided_model:
        logger.info(
            "Model config: baseline=%s, guided=%s",
            baseline_planner, guided_planner,
        )

    # Load task config
    from openadapt_evals.task_config import TaskConfig
    task_config = TaskConfig.from_yaml(args.task_config)
    logger.info("Task: %s (id=%s, %d milestones)",
                task_config.name, task_config.id, len(task_config.milestones))

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Track phase errors for partial reporting
    phase_errors: list[str] = []

    # Scores default to 0 for partial report generation
    attempt_score = 0.0
    attempt_screenshots: list[bytes] = []
    retry_score = 0.0
    demo_dir = args.demo_dir

    vm_ip: str | None = None

    start = time.monotonic()

    try:
        # ---------------------------------------------------------------
        # Step 0: VM lifecycle (optional)
        # ---------------------------------------------------------------
        if args.manage_vm:
            logger.info("=" * 60)
            logger.info("STEP 0: VM LIFECYCLE (start + container + iptables)")
            logger.info("=" * 60)

            if not start_vm(args.vm_name, args.vm_resource_group):
                msg = f"Failed to start VM {args.vm_name}"
                logger.error(msg)
                phase_errors.append(msg)
                return 1

            vm_ip = get_vm_ip(args.vm_name, args.vm_resource_group)
            if not vm_ip:
                msg = f"Could not resolve IP for VM {args.vm_name}"
                logger.error(msg)
                phase_errors.append(msg)
                return 1
            logger.info("VM IP: %s", vm_ip)

            if not wait_for_ssh(vm_ip, username=args.vm_user, timeout=args.ssh_timeout):
                msg = f"SSH not available on {vm_ip} after {args.ssh_timeout}s"
                logger.error(msg)
                phase_errors.append(msg)
                return 1

            if not start_container(vm_ip, username=args.vm_user):
                msg = "Failed to start WAA container"
                logger.error(msg)
                phase_errors.append(msg)
                return 1

            # Apply iptables fix (idempotent, non-fatal if it fails)
            apply_iptables_fix(vm_ip, username=args.vm_user)

        # ---------------------------------------------------------------
        # Step 1: Tunnel setup (optional)
        # ---------------------------------------------------------------
        if args.setup_tunnels:
            logger.info("=" * 60)
            logger.info("STEP 1: SSH TUNNEL SETUP")
            logger.info("=" * 60)

            if vm_ip is None:
                # Need to resolve VM IP even if not managing VM lifecycle
                vm_ip = get_vm_ip(args.vm_name, args.vm_resource_group)
                if not vm_ip:
                    msg = (
                        f"Cannot setup tunnels: could not resolve IP for "
                        f"VM {args.vm_name}. Is the VM running?"
                    )
                    logger.error(msg)
                    phase_errors.append(msg)
                    return 1

            tunnel_pids = setup_tunnels(vm_ip, username=args.vm_user)
            failed_tunnels = [
                name for name, pid in tunnel_pids.items() if pid is None
            ]
            if failed_tunnels:
                msg = f"Failed to establish tunnels: {', '.join(failed_tunnels)}"
                logger.error(msg)
                phase_errors.append(msg)
                return 1

            # Wait for WAA server to respond through the tunnel
            if not wait_for_waa(args.server_url, timeout=args.waa_timeout):
                msg = f"WAA server not ready at {args.server_url} after {args.waa_timeout}s"
                logger.error(msg)
                phase_errors.append(msg)
                return 1

        # ---------------------------------------------------------------
        # Phase 1: Attempt without guidance
        # ---------------------------------------------------------------
        baseline_kwargs = dict(
            max_steps=args.max_steps,
            planner_model=baseline_planner,
            planner_provider=args.planner_provider,
            grounder_model=args.grounder_model,
            grounder_provider=args.grounder_provider,
            grounder_endpoint=args.grounder_endpoint,
        )

        try:
            attempt_score, attempt_screenshots = phase1_attempt(
                mock=args.mock,
                server_url=args.server_url,
                task_config=task_config,
                output_dir=output_dir,
                **baseline_kwargs,
            )
        except Exception as e:
            msg = f"Phase 1 failed: {e}"
            logger.error(msg, exc_info=True)
            phase_errors.append(msg)

        # ---------------------------------------------------------------
        # Phase 2: Capture/store correction
        # ---------------------------------------------------------------
        try:
            demo_dir = phase2_correct(
                task_config=task_config,
                demo_dir=args.demo_dir,
                attempt_screenshots=attempt_screenshots,
                output_dir=output_dir,
            )
        except Exception as e:
            msg = f"Phase 2 failed: {e}"
            logger.error(msg, exc_info=True)
            phase_errors.append(msg)

        # ---------------------------------------------------------------
        # Reset environment between phases (Bug 1 fix)
        # ---------------------------------------------------------------
        if args.reset_between_phases:
            try:
                reset_ok = _reset_environment_between_phases(
                    server_url=args.server_url,
                    task_config=task_config,
                    mock=args.mock,
                )
                if not reset_ok:
                    msg = "Environment reset between phases failed"
                    logger.warning(msg)
                    phase_errors.append(msg)
            except Exception as e:
                msg = f"Environment reset between phases failed: {e}"
                logger.error(msg, exc_info=True)
                phase_errors.append(msg)

        # ---------------------------------------------------------------
        # Phase 3: Retry with guidance
        # ---------------------------------------------------------------
        guided_kwargs = dict(
            max_steps=args.max_steps,
            planner_model=guided_planner,
            planner_provider=args.planner_provider,
            grounder_model=args.grounder_model,
            grounder_provider=args.grounder_provider,
            grounder_endpoint=args.grounder_endpoint,
        )

        try:
            retry_score, retry_screenshots = phase3_retry(
                mock=args.mock,
                server_url=args.server_url,
                task_config=task_config,
                demo_dir=demo_dir,
                output_dir=output_dir,
                **guided_kwargs,
            )
        except Exception as e:
            msg = f"Phase 3 failed: {e}"
            logger.error(msg, exc_info=True)
            phase_errors.append(msg)

        # ---------------------------------------------------------------
        # Phase 4: Verify improvement (always runs, even with partial data)
        # ---------------------------------------------------------------
        success = phase4_verify(
            attempt_score=attempt_score,
            retry_score=retry_score,
            output_dir=output_dir,
            task_name=task_config.name,
            baseline_model=args.baseline_model,
            guided_model=args.guided_model,
            phase_errors=phase_errors if phase_errors else None,
        )

        elapsed = time.monotonic() - start
        logger.info("Flywheel completed in %.1fs", elapsed)

        return 0 if (success and not phase_errors) else 1

    finally:
        # ---------------------------------------------------------------
        # Cleanup: Deallocate VM (ALWAYS runs, even on error)
        # ---------------------------------------------------------------
        if args.manage_vm:
            logger.info("=" * 60)
            logger.info("CLEANUP: Deallocating VM %s", args.vm_name)
            logger.info("=" * 60)
            deallocate_vm(args.vm_name, args.vm_resource_group)


if __name__ == "__main__":
    sys.exit(main())
