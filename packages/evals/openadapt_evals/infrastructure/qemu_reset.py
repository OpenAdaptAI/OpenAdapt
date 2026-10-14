"""QEMU Monitor Reset Manager for Windows VMs.

Provides reliable Windows restart inside QEMU (dockur/windows Docker image)
by sending ``system_reset`` via the QEMU monitor telnet interface on port 7100.

The WAA Flask server running inside Windows dies when you send
``shutdown /r /t 0`` through the ``/execute`` endpoint, making that approach
unreliable.  Sending ``system_reset`` through the QEMU monitor is a hard
reset that works regardless of the guest OS state.

Architecture::

    Local machine
       --> SSH --> Azure VM (Ubuntu host)
           --> docker exec winarena
               --> echo "system_reset" | nc -q1 localhost 7100
               (QEMU monitor telnet on port 7100)

After reset, the container's ``entry_setup.sh`` automatically polls
``172.30.0.2:5000/probe`` and the WAA Flask server comes back up in
~90 seconds.

Usage::

    from openadapt_evals.infrastructure.qemu_reset import QEMUResetManager

    mgr = QEMUResetManager(vm_ip="10.0.0.1")

    # Full restart: send reset + wait for WAA server
    success, message = mgr.restart_windows()

    # Or do each step separately
    mgr.reset_windows()
    mgr.wait_for_waa_ready()
"""

from __future__ import annotations

import logging
import subprocess
import time

import requests

logger = logging.getLogger(__name__)

# SSH options consistent with the rest of the codebase
_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
]


class QEMUResetManager:
    """Manage Windows restarts via QEMU monitor inside a Docker container.

    Attributes:
        vm_ip: IP address of the Azure Ubuntu VM hosting the Docker container.
        ssh_user: SSH user for the VM (default ``azureuser``).
        qemu_monitor_port: QEMU monitor telnet port inside the container (default 7100).
        container_name: Docker container name (default ``winarena``).
        timeout_seconds: Maximum seconds to wait for the WAA server after reset.
    """

    def __init__(
        self,
        vm_ip: str,
        ssh_user: str = "azureuser",
        qemu_monitor_port: int = 7100,
        container_name: str = "winarena",
        timeout_seconds: int = 300,
    ) -> None:
        self.vm_ip = vm_ip
        self.ssh_user = ssh_user
        self.qemu_monitor_port = qemu_monitor_port
        self.container_name = container_name
        self.timeout_seconds = timeout_seconds

    def reset_windows(self) -> bool:
        """Send ``system_reset`` via the QEMU monitor over SSH.

        Executes::

            ssh {user}@{ip} "docker exec {container} bash -c
                'echo system_reset | nc -q1 localhost {port}'"

        Returns:
            True if the SSH + docker exec command succeeded (exit code 0).
        """
        docker_cmd = (
            f"docker exec {self.container_name} bash -c "
            f"'echo system_reset | nc -q1 localhost {self.qemu_monitor_port}'"
        )
        ssh_cmd = [
            "ssh",
            *_SSH_OPTS,
            f"{self.ssh_user}@{self.vm_ip}",
            docker_cmd,
        ]

        logger.info(
            "Sending system_reset via QEMU monitor (port %d) on %s",
            self.qemu_monitor_port,
            self.vm_ip,
        )

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.error("SSH command timed out sending system_reset")
            return False

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            logger.error(
                "QEMU monitor reset failed (rc=%d): %s",
                result.returncode,
                stderr,
            )
            return False

        logger.info("QEMU system_reset sent successfully")
        return True

    def wait_for_waa_ready(
        self,
        server_url: str = "http://localhost:5001",
        check_interval: int = 10,
    ) -> bool:
        """Poll the WAA ``/probe`` endpoint until it responds or timeout.

        Args:
            server_url: Base URL of the WAA server (through SSH tunnel).
            check_interval: Seconds between probe attempts.

        Returns:
            True if the server responded within ``timeout_seconds``, False on timeout.
        """
        probe_url = f"{server_url}/probe"
        deadline = time.time() + self.timeout_seconds
        start = time.time()

        logger.info(
            "Waiting up to %ds for WAA server at %s",
            self.timeout_seconds,
            probe_url,
        )

        while time.time() < deadline:
            elapsed = int(time.time() - start)
            try:
                resp = requests.get(probe_url, timeout=check_interval)
                if resp.ok:
                    logger.info("WAA server ready after %ds", elapsed)
                    return True
            except (requests.ConnectionError, requests.Timeout):
                pass

            remaining = int(deadline - time.time())
            if remaining > 0:
                logger.info(
                    "[%ds] WAA not ready yet, retrying in %ds (%ds remaining)...",
                    elapsed,
                    check_interval,
                    remaining,
                )
                time.sleep(check_interval)

        elapsed = int(time.time() - start)
        logger.error("WAA server did not become ready within %ds", elapsed)
        return False

    def restart_windows(
        self,
        server_url: str = "http://localhost:5001",
    ) -> tuple[bool, str]:
        """Full restart cycle: send QEMU reset then wait for WAA readiness.

        Args:
            server_url: Base URL of the WAA server (through SSH tunnel).

        Returns:
            Tuple of (success, message) where *success* is True if the
            server came back within the timeout.
        """
        if not self.reset_windows():
            return False, "Failed to send system_reset via QEMU monitor"

        logger.info("Reset sent, waiting for WAA server to come back...")

        if self.wait_for_waa_ready(server_url=server_url):
            return True, "Windows restarted and WAA server is ready"

        return False, f"WAA server did not come back within {self.timeout_seconds}s"

    def is_qemu_monitor_reachable(self) -> bool:
        """Check whether the QEMU monitor telnet port is reachable inside the container.

        This can be used to decide whether to fall back to ``docker restart``.

        Returns:
            True if the QEMU monitor responds.
        """
        docker_cmd = (
            f"docker exec {self.container_name} bash -c "
            f"'echo info version | nc -q1 localhost {self.qemu_monitor_port}'"
        )
        ssh_cmd = [
            "ssh",
            *_SSH_OPTS,
            f"{self.ssh_user}@{self.vm_ip}",
            docker_cmd,
        ]

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                timeout=15,
            )
            stdout = result.stdout.decode("utf-8", errors="replace")
            reachable = result.returncode == 0 and "QEMU" in stdout
            logger.debug(
                "QEMU monitor reachable: %s (stdout: %s)",
                reachable,
                stdout.strip()[:100],
            )
            return reachable
        except subprocess.TimeoutExpired:
            logger.debug("QEMU monitor reachability check timed out")
            return False
