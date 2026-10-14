"""SSH Tunnel Manager for Azure VMs.

This module provides automatic SSH tunnel management for accessing services
running inside Azure VMs (VNC, WAA server) that are not exposed via NSG.

Architecture:
    Azure VMs have Network Security Groups (NSGs) that act as firewalls.
    By default, only port 22 (SSH) is open. To access other services like
    VNC (8006) and WAA (5000), we create SSH tunnels:

    Browser → localhost:8006 → SSH Tunnel → Azure VM:8006 → Docker → noVNC

    This is more secure than opening ports in NSG because:
    1. All traffic is encrypted through SSH
    2. No authentication bypass (VNC has no auth by default)
    3. Access requires SSH key authentication

Usage:
    from openadapt_evals.infrastructure.ssh_tunnel import SSHTunnelManager

    # Create manager
    manager = SSHTunnelManager()

    # Start tunnels for a VM
    manager.start_tunnels_for_vm(
        vm_ip="172.171.112.41",
        ssh_user="azureuser",
        ports={"vnc": 8006, "waa": 5000}
    )

    # Check tunnel status
    status = manager.get_tunnel_status()
    # {'vnc': {'active': True, 'local_port': 8006, 'remote': '172.171.112.41:8006'}, ...}

    # Stop all tunnels
    manager.stop_all_tunnels()

Integration:
    The SSHTunnelManager is integrated with the dashboard server (local.py):
    - When a VM's WAA probe becomes "ready", tunnels are auto-started
    - When VM goes offline, tunnels are auto-stopped
    - Dashboard shows tunnel status next to VNC button
    - VNC button links to localhost:port (tunnel endpoint)
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TunnelConfig:
    """Configuration for a single SSH tunnel."""

    name: str  # e.g., "vnc", "waa"
    local_port: int  # Local port to listen on
    remote_port: int  # Port on the remote VM
    remote_host: str = "localhost"  # Host on remote side (usually localhost)


@dataclass
class TunnelStatus:
    """Status of an SSH tunnel."""

    name: str
    active: bool
    local_port: int
    remote_endpoint: str  # e.g., "172.171.112.41:8006"
    pid: int | None = None
    error: str | None = None


class SSHTunnelManager:
    """Manages SSH tunnels for Azure VM access.

    Provides automatic setup and teardown of SSH tunnels for services
    running inside Azure VMs that are not exposed via NSG.

    Features:
    - Auto-reconnect: Automatically restarts dead tunnels
    - Health monitoring: Periodic checks to verify tunnels are working
    - Graceful handling of network interruptions

    Attributes:
        tunnels: Dict of tunnel name -> (TunnelConfig, process)
        ssh_key_path: Path to SSH private key
    """

    # Default tunnel configurations
    # Note: WAA uses local_port=5001 to avoid conflicts with any local WAA server on 5000
    # The remote port is still 5000 (where WAA Flask runs inside Windows)
    DEFAULT_TUNNELS = [
        TunnelConfig(name="vnc", local_port=8006, remote_port=8006),
        TunnelConfig(name="waa", local_port=5001, remote_port=5000),
        TunnelConfig(name="evaluate", local_port=5050, remote_port=5051),
    ]

    # Auto-reconnect settings
    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_DELAY_SECONDS = 2

    def __init__(
        self,
        ssh_key_path: str | Path | None = None,
        tunnels: list[TunnelConfig] | None = None,
        auto_reconnect: bool = True,
        host_key_alias: str | None = None,
        host_public_key: str | None = None,
    ):
        """Initialize tunnel manager.

        Args:
            ssh_key_path: Path to SSH private key. Defaults to ~/.ssh/id_rsa.
            tunnels: List of tunnel configurations. Defaults to VNC + WAA.
            auto_reconnect: If True, automatically restart dead tunnels.
            host_key_alias: Admitted SSH instance identity for strict checking.
            host_public_key: Admitted SSH public host key.
        """
        if (host_key_alias is None) != (host_public_key is None):
            raise ValueError("SSH host alias and public key must be supplied together")
        if host_key_alias is not None and (
            not host_key_alias or "\n" in host_key_alias or "\x00" in host_key_alias
        ):
            raise ValueError("SSH host key alias is invalid")
        if host_public_key is not None and (
            not host_public_key.startswith("ssh-")
            or "\n" in host_public_key
            or "\x00" in host_public_key
        ):
            raise ValueError("SSH host public key is invalid")
        self.ssh_key_path = Path(ssh_key_path or Path.home() / ".ssh" / "id_rsa")
        self.tunnel_configs = tunnels or self.DEFAULT_TUNNELS
        self._active_tunnels: dict[str, tuple[TunnelConfig, subprocess.Popen]] = {}
        self._current_vm_ip: str | None = None
        self._current_ssh_user: str | None = None
        self._auto_reconnect = auto_reconnect
        self._host_key_alias = host_key_alias
        self._host_public_key = host_public_key
        self._known_hosts_path: Path | None = None
        self._reconnect_attempts: dict[
            str, int
        ] = {}  # Track reconnect attempts per tunnel

    def start_tunnels_for_vm(
        self,
        vm_ip: str,
        ssh_user: str = "azureuser",
        tunnels: list[TunnelConfig] | None = None,
    ) -> dict[str, TunnelStatus]:
        """Start SSH tunnels for a VM.

        Args:
            vm_ip: IP address of the Azure VM.
            ssh_user: SSH username (default: azureuser).
            tunnels: Optional list of tunnels to start. Defaults to all configured tunnels.

        Returns:
            Dict of tunnel name -> TunnelStatus.
        """
        self._current_vm_ip = vm_ip
        self._current_ssh_user = ssh_user

        tunnels_to_start = tunnels or self.tunnel_configs
        results = {}

        for config in tunnels_to_start:
            status = self._start_tunnel(config, vm_ip, ssh_user)
            results[config.name] = status

        return results

    def _start_tunnel(
        self,
        config: TunnelConfig,
        vm_ip: str,
        ssh_user: str,
    ) -> TunnelStatus:
        """Start a single SSH tunnel.

        Args:
            config: Tunnel configuration.
            vm_ip: IP address of the Azure VM.
            ssh_user: SSH username.

        Returns:
            TunnelStatus indicating success or failure.
        """
        # Check if tunnel already active
        if config.name in self._active_tunnels:
            proc = self._active_tunnels[config.name][1]
            if proc.poll() is None:  # Still running
                logger.debug(f"Tunnel {config.name} already active")
                return TunnelStatus(
                    name=config.name,
                    active=True,
                    local_port=config.local_port,
                    remote_endpoint=f"{vm_ip}:{config.remote_port}",
                    pid=proc.pid,
                )

        # Check if local port is already in use
        if self._is_port_in_use(config.local_port):
            if self._host_key_alias is not None:
                return TunnelStatus(
                    name=config.name,
                    active=False,
                    local_port=config.local_port,
                    remote_endpoint=f"{vm_ip}:{config.remote_port}",
                    error=f"Port {config.local_port} is already in use",
                )
            # Port in use - check if it's an existing SSH tunnel (likely created manually)
            # If we can reach the service through it, consider it active
            if self._check_tunnel_works(config.local_port, config.remote_port):
                logger.info(f"Port {config.local_port} has existing working tunnel")
                return TunnelStatus(
                    name=config.name,
                    active=True,
                    local_port=config.local_port,
                    remote_endpoint=f"{vm_ip}:{config.remote_port}",
                    pid=None,  # We don't know the PID of the external tunnel
                )
            else:
                logger.warning(
                    f"Port {config.local_port} already in use by unknown process"
                )
                return TunnelStatus(
                    name=config.name,
                    active=False,
                    local_port=config.local_port,
                    remote_endpoint=f"{vm_ip}:{config.remote_port}",
                    error=f"Port {config.local_port} in use by another process",
                )

        # Build SSH command with keepalive settings to prevent timeout during long runs
        # ServerAliveInterval=60: Send keepalive every 60 seconds
        # ServerAliveCountMax=10: Disconnect after 10 missed keepalives (10 min tolerance)
        # TCPKeepAlive=yes: Enable TCP-level keepalive as additional safeguard
        host_key_options: list[str]
        if self._host_key_alias is not None and self._host_public_key is not None:
            if self._known_hosts_path is None:
                known_hosts = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="openadapt-known-hosts-",
                    delete=False,
                )
                try:
                    known_hosts.write(f"{self._host_key_alias} {self._host_public_key}\n")
                    known_hosts.flush()
                    os.fchmod(known_hosts.fileno(), 0o600)
                    self._known_hosts_path = Path(known_hosts.name)
                finally:
                    known_hosts.close()
            host_key_options = [
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self._known_hosts_path}",
                "-o",
                f"HostKeyAlias={self._host_key_alias}",
            ]
        else:
            host_key_options = [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
            ]
        ssh_cmd = [
            "ssh",
            *host_key_options,
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ServerAliveInterval=60",
            "-o",
            "ServerAliveCountMax=10",
            "-o",
            "TCPKeepAlive=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-i",
            str(self.ssh_key_path),
            "-N",  # Don't execute remote command
            "-L",
            f"{config.local_port}:{config.remote_host}:{config.remote_port}",
            f"{ssh_user}@{vm_ip}",
        ]

        try:
            # Start SSH tunnel in background
            proc = subprocess.Popen(
                ssh_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,  # Detach from terminal
            )

            # Wait briefly to check if it started successfully
            time.sleep(0.5)

            if proc.poll() is not None:
                # Process exited, get error
                _, stderr = proc.communicate(timeout=1)
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.error(f"Tunnel {config.name} failed: {error_msg}")
                return TunnelStatus(
                    name=config.name,
                    active=False,
                    local_port=config.local_port,
                    remote_endpoint=f"{vm_ip}:{config.remote_port}",
                    error=error_msg[:200],
                )

            # Tunnel started successfully
            self._active_tunnels[config.name] = (config, proc)
            logger.info(
                f"Started tunnel {config.name}: localhost:{config.local_port} -> {vm_ip}:{config.remote_port}"
            )

            return TunnelStatus(
                name=config.name,
                active=True,
                local_port=config.local_port,
                remote_endpoint=f"{vm_ip}:{config.remote_port}",
                pid=proc.pid,
            )

        except Exception as e:
            logger.error(f"Failed to start tunnel {config.name}: {e}")
            return TunnelStatus(
                name=config.name,
                active=False,
                local_port=config.local_port,
                remote_endpoint=f"{vm_ip}:{config.remote_port}",
                error=str(e)[:200],
            )

    def stop_tunnel(self, name: str) -> bool:
        """Stop a specific tunnel by name.

        Args:
            name: Tunnel name (e.g., "vnc", "waa").

        Returns:
            True if tunnel was stopped, False if not found.
        """
        if name not in self._active_tunnels:
            return False

        config, proc = self._active_tunnels[name]

        try:
            # Send SIGTERM to gracefully stop
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass  # Already dead
        except subprocess.TimeoutExpired:
            # Force kill
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

        del self._active_tunnels[name]
        logger.info(f"Stopped tunnel {name}")
        return True

    def stop_all_tunnels(self) -> None:
        """Stop all active tunnels."""
        for name in list(self._active_tunnels.keys()):
            self.stop_tunnel(name)
        self._current_vm_ip = None
        self._current_ssh_user = None
        if self._known_hosts_path is not None:
            try:
                self._known_hosts_path.unlink()
            except FileNotFoundError:
                pass
            self._known_hosts_path = None

    def get_tunnel_status(self, auto_restart: bool = True) -> dict[str, TunnelStatus]:
        """Get status of all configured tunnels.

        This method checks the actual port status, not just internal state.
        This correctly reports tunnels as active even if they were started
        by a different process or if the tunnel manager was restarted.

        If auto_reconnect is enabled and a tunnel is found dead, this method
        will attempt to restart it automatically.

        Args:
            auto_restart: If True and auto_reconnect is enabled, restart dead tunnels.

        Returns:
            Dict of tunnel name -> TunnelStatus.
        """
        results = {}
        tunnels_to_restart = []

        for config in self.tunnel_configs:
            if config.name in self._active_tunnels:
                _, proc = self._active_tunnels[config.name]
                if proc.poll() is None:  # Still running
                    # Reset reconnect attempts on successful check
                    self._reconnect_attempts[config.name] = 0
                    results[config.name] = TunnelStatus(
                        name=config.name,
                        active=True,
                        local_port=config.local_port,
                        remote_endpoint=f"{self._current_vm_ip}:{config.remote_port}"
                        if self._current_vm_ip
                        else "unknown",
                        pid=proc.pid,
                    )
                else:
                    # Process died - but check if port is still working
                    # (could be another tunnel on the same port)
                    del self._active_tunnels[config.name]
                    if self._host_key_alias is None and self._is_port_in_use(
                        config.local_port
                    ) and self._check_tunnel_works(
                        config.local_port, config.remote_port
                    ):
                        results[config.name] = TunnelStatus(
                            name=config.name,
                            active=True,
                            local_port=config.local_port,
                            remote_endpoint=f"{self._current_vm_ip}:{config.remote_port}"
                            if self._current_vm_ip
                            else "external",
                            pid=None,  # External tunnel, PID unknown
                        )
                    else:
                        # Tunnel is dead - mark for restart if auto_reconnect enabled
                        if (
                            self._auto_reconnect
                            and auto_restart
                            and self._current_vm_ip
                        ):
                            tunnels_to_restart.append(config)
                        results[config.name] = TunnelStatus(
                            name=config.name,
                            active=False,
                            local_port=config.local_port,
                            remote_endpoint="",
                            error="Tunnel process exited",
                        )
            else:
                # Not tracked internally - but check if an external tunnel exists
                # This handles tunnels started by other processes or after manager restart
                if self._host_key_alias is None and self._is_port_in_use(
                    config.local_port
                ) and self._check_tunnel_works(
                    config.local_port, config.remote_port
                ):
                    logger.debug(
                        f"Found working external tunnel on port {config.local_port}"
                    )
                    results[config.name] = TunnelStatus(
                        name=config.name,
                        active=True,
                        local_port=config.local_port,
                        remote_endpoint=f"{self._current_vm_ip}:{config.remote_port}"
                        if self._current_vm_ip
                        else "external",
                        pid=None,  # External tunnel, PID unknown
                    )
                else:
                    results[config.name] = TunnelStatus(
                        name=config.name,
                        active=False,
                        local_port=config.local_port,
                        remote_endpoint="",
                    )

        # Auto-restart dead tunnels
        for config in tunnels_to_restart:
            attempts = self._reconnect_attempts.get(config.name, 0)
            if attempts < self.MAX_RECONNECT_ATTEMPTS:
                logger.info(
                    f"Auto-reconnecting tunnel {config.name} (attempt {attempts + 1}/{self.MAX_RECONNECT_ATTEMPTS})"
                )
                time.sleep(self.RECONNECT_DELAY_SECONDS)
                self._reconnect_attempts[config.name] = attempts + 1
                status = self._start_tunnel(
                    config, self._current_vm_ip, self._current_ssh_user or "azureuser"
                )
                results[config.name] = status
                if status.active:
                    logger.info(f"Successfully reconnected tunnel {config.name}")
                    self._reconnect_attempts[config.name] = 0  # Reset on success
            else:
                logger.warning(
                    f"Tunnel {config.name} exceeded max reconnect attempts ({self.MAX_RECONNECT_ATTEMPTS})"
                )
                results[config.name] = TunnelStatus(
                    name=config.name,
                    active=False,
                    local_port=config.local_port,
                    remote_endpoint="",
                    error=f"Max reconnect attempts ({self.MAX_RECONNECT_ATTEMPTS}) exceeded",
                )

        return results

    def is_tunnel_active(self, name: str) -> bool:
        """Check if a specific tunnel is active.

        Args:
            name: Tunnel name.

        Returns:
            True if tunnel is active.
        """
        status = self.get_tunnel_status()
        return name in status and status[name].active

    def reset_reconnect_attempts(self, name: str | None = None) -> None:
        """Reset reconnect attempt counter for tunnels.

        Call this after manually fixing connectivity issues or when
        VM is known to be healthy again.

        Args:
            name: Tunnel name to reset, or None to reset all.
        """
        if name:
            self._reconnect_attempts[name] = 0
        else:
            self._reconnect_attempts.clear()
        logger.info(f"Reset reconnect attempts for {name or 'all tunnels'}")

    def ensure_tunnels_for_vm(
        self,
        vm_ip: str,
        ssh_user: str = "azureuser",
    ) -> dict[str, TunnelStatus]:
        """Ensure tunnels are running for a VM, starting if needed.

        This is idempotent - safe to call repeatedly.

        Args:
            vm_ip: IP address of the Azure VM.
            ssh_user: SSH username.

        Returns:
            Dict of tunnel name -> TunnelStatus.
        """
        # If VM changed, stop old tunnels and reset reconnect attempts
        if self._current_vm_ip and self._current_vm_ip != vm_ip:
            logger.info(
                f"VM IP changed from {self._current_vm_ip} to {vm_ip}, restarting tunnels"
            )
            self.stop_all_tunnels()
            self.reset_reconnect_attempts()  # Fresh start for new VM

        # Check current status and start any missing tunnels
        # get_tunnel_status will auto-restart dead tunnels if enabled
        current_status = self.get_tunnel_status()
        all_active = all(s.active for s in current_status.values())

        if all_active and self._current_vm_ip == vm_ip:
            return current_status

        # Start tunnels (also resets reconnect attempts for this VM)
        self.reset_reconnect_attempts()
        return self.start_tunnels_for_vm(vm_ip, ssh_user)

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a local port is in use.

        Args:
            port: Port number.

        Returns:
            True if port is in use.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", port))
                return False
            except OSError:
                return True

    def _check_tunnel_works(self, local_port: int, remote_port: int) -> bool:
        """Check if an existing tunnel on a port is actually working.

        For VNC (8006), check if we get HTTP response from noVNC.
        For WAA (5000), check if /probe endpoint responds.

        Args:
            local_port: Local port to check.
            remote_port: Remote port (used to determine service type).

        Returns:
            True if tunnel appears to be working.
        """
        import urllib.error
        import urllib.request

        try:
            if remote_port == 5000:
                # WAA server - check /probe endpoint
                req = urllib.request.Request(
                    f"http://localhost:{local_port}/probe",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return resp.status == 200
            elif remote_port == 8006:
                # VNC - check if noVNC responds
                req = urllib.request.Request(
                    f"http://localhost:{local_port}/",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return resp.status == 200
            else:
                # Unknown service - try to connect
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(3)
                    s.connect(("localhost", local_port))
                    return True
        except (urllib.error.URLError, socket.error, OSError):
            return False

    def __del__(self):
        """Clean up tunnels on destruction."""
        try:
            self.stop_all_tunnels()
        except Exception:
            pass


# Global tunnel manager instance
_tunnel_manager: SSHTunnelManager | None = None


def get_tunnel_manager() -> SSHTunnelManager:
    """Get the global tunnel manager instance.

    Returns:
        SSHTunnelManager instance.
    """
    global _tunnel_manager
    if _tunnel_manager is None:
        _tunnel_manager = SSHTunnelManager()
    return _tunnel_manager
