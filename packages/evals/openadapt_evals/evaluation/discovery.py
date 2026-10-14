"""VM IP auto-discovery for WAA evaluation.

Provides multiple methods to discover the WAA VM IP address without
requiring manual configuration.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class DiscoveryMethod(Enum):
    """Methods for discovering VM IP address."""
    EXPLICIT = "explicit"           # Passed directly
    ENV_VAR = "env_var"             # From environment variable
    SSH_TUNNEL = "ssh_tunnel"       # localhost when tunnel active
    DOCKER = "docker"               # From Docker network inspection
    SESSION_FILE = "session_file"   # From session tracker file
    AZURE_STATUS = "azure_status"   # From azure_ops_status.json
    PROBE = "probe"                 # Scan common IPs


@dataclass
class DiscoveryResult:
    """Result of IP discovery attempt."""
    ip: Optional[str]
    method: DiscoveryMethod
    confidence: float  # 0.0 to 1.0
    details: str


class VMIPDiscovery:
    """Discovers WAA VM IP address from multiple sources."""

    def __init__(self):
        self._cached_ip: Optional[str] = None
        self._cached_method: Optional[DiscoveryMethod] = None

    def discover(self, explicit_ip: Optional[str] = None) -> DiscoveryResult:
        """Discover VM IP using multiple methods in priority order.

        Args:
            explicit_ip: If provided, use this IP directly.

        Returns:
            DiscoveryResult with the discovered IP and method used.
        """
        # Priority 1: Explicit IP provided
        if explicit_ip:
            return DiscoveryResult(
                ip=explicit_ip,
                method=DiscoveryMethod.EXPLICIT,
                confidence=1.0,
                details="IP provided explicitly"
            )

        # Priority 2: Environment variable
        result = self._try_env_var()
        if result.ip:
            return result

        # Priority 3: Session tracker file
        result = self._try_session_file()
        if result.ip:
            return result

        # Priority 4: Azure ops status file
        result = self._try_azure_status()
        if result.ip:
            return result

        # Priority 5: SSH tunnel (localhost)
        result = self._try_ssh_tunnel()
        if result.ip:
            return result

        # Priority 6: Docker network
        result = self._try_docker()
        if result.ip:
            return result

        # No IP found
        return DiscoveryResult(
            ip=None,
            method=DiscoveryMethod.PROBE,
            confidence=0.0,
            details="No VM IP could be discovered from any source"
        )

    def _try_env_var(self) -> DiscoveryResult:
        """Try to get IP from environment variables."""
        for var in ["WAA_VM_IP", "VM_IP", "AZURE_VM_IP"]:
            ip = os.environ.get(var)
            if ip:
                return DiscoveryResult(
                    ip=ip,
                    method=DiscoveryMethod.ENV_VAR,
                    confidence=0.95,
                    details=f"From environment variable {var}"
                )
        return DiscoveryResult(None, DiscoveryMethod.ENV_VAR, 0.0, "No env var set")

    def _try_session_file(self) -> DiscoveryResult:
        """Try to get IP from session tracker file."""
        session_paths = [
            Path.home() / ".openadapt" / "vm_session.json",
            Path("benchmark_results/vm_session.json"),
            Path("/tmp/vm_session.json"),
        ]

        for path in session_paths:
            try:
                if path.exists():
                    data = json.loads(path.read_text())
                    ip = data.get("vm_ip")
                    if ip:
                        return DiscoveryResult(
                            ip=ip,
                            method=DiscoveryMethod.SESSION_FILE,
                            confidence=0.9,
                            details=f"From session file: {path}"
                        )
            except (json.JSONDecodeError, IOError):
                continue

        return DiscoveryResult(None, DiscoveryMethod.SESSION_FILE, 0.0, "No session file found")

    def _try_azure_status(self) -> DiscoveryResult:
        """Try to get IP from azure_ops_status.json."""
        status_paths = [
            Path("benchmark_results/azure_ops_status.json"),
            Path("training_output/current/azure_ops_status.json"),
        ]

        for path in status_paths:
            try:
                if path.exists():
                    data = json.loads(path.read_text())
                    ip = data.get("vm_ip")
                    if ip:
                        return DiscoveryResult(
                            ip=ip,
                            method=DiscoveryMethod.AZURE_STATUS,
                            confidence=0.85,
                            details=f"From status file: {path}"
                        )
            except (json.JSONDecodeError, IOError):
                continue

        return DiscoveryResult(None, DiscoveryMethod.AZURE_STATUS, 0.0, "No status file found")

    def _try_ssh_tunnel(self) -> DiscoveryResult:
        """Check if SSH tunnel is active (localhost:5000 reachable)."""
        import socket

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', 5000))
            sock.close()

            if result == 0:
                return DiscoveryResult(
                    ip="localhost",
                    method=DiscoveryMethod.SSH_TUNNEL,
                    confidence=0.95,
                    details="SSH tunnel detected on localhost:5000"
                )
        except socket.error:
            pass

        return DiscoveryResult(None, DiscoveryMethod.SSH_TUNNEL, 0.0, "No SSH tunnel detected")

    def _try_docker(self) -> DiscoveryResult:
        """Try to get IP from Docker network inspection."""
        try:
            # Check if we're inside a Docker container
            result = subprocess.run(
                ["docker", "inspect", "winarena", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                ip = result.stdout.strip()
                return DiscoveryResult(
                    ip=ip,
                    method=DiscoveryMethod.DOCKER,
                    confidence=0.9,
                    details="From Docker container inspection"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Check for standard Docker bridge IP
        docker_ip = "172.30.0.2"  # Standard WAA container IP
        return DiscoveryResult(
            ip=docker_ip,
            method=DiscoveryMethod.DOCKER,
            confidence=0.5,
            details="Using default Docker bridge IP (unverified)"
        )


def discover_vm_ip(explicit_ip: Optional[str] = None) -> Optional[str]:
    """Convenience function to discover VM IP.

    Args:
        explicit_ip: If provided, returns this IP directly.

    Returns:
        Discovered VM IP or None if not found.
    """
    discovery = VMIPDiscovery()
    result = discovery.discover(explicit_ip)
    return result.ip
