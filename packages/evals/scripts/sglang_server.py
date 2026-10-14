"""SGLang server lifecycle management for remote GPU hosts.

Manages an SGLang inference server on a remote GPU host via SSH:
    1. SSH to gpu_host and install SGLang if needed
    2. Start the server as a background process
    3. Wait for /v1/models to respond
    4. Set up an SSH tunnel from local_port to gpu_host:remote_port
    5. Provide the tunneled endpoint for use as OpenAI-compatible API
    6. Stop the server and tunnel on cleanup
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger("compare_models.sglang")


@dataclass
class SGLangServeConfig:
    """Configuration for serving a model via SGLang on a remote GPU host."""

    engine: str = "sglang"
    port: int = 8080
    args: str = ""


class SGLangServerManager:
    """Manages an SGLang inference server on a remote GPU host via SSH."""

    def __init__(
        self,
        gpu_host: str,
        model_name: str,
        remote_port: int = 8080,
        local_port: int | None = None,
        extra_args: str = "",
        ssh_key: str | None = None,
    ):
        self.gpu_host = gpu_host
        self.model_name = model_name
        self.remote_port = remote_port
        self.local_port = local_port or remote_port
        self.extra_args = extra_args
        self.ssh_key = ssh_key
        self._tunnel_proc: subprocess.Popen | None = None
        self._server_started = False

    @property
    def endpoint(self) -> str:
        """OpenAI-compatible base URL for the tunneled SGLang server."""
        return f"http://localhost:{self.local_port}/v1"

    def _ssh_cmd(
        self, command: str, timeout: int = 300
    ) -> tuple[int, str, str]:
        """Run a command on the GPU host via SSH.

        Args:
            command: Shell command to run remotely.
            timeout: Seconds before timing out.

        Returns:
            Tuple of (returncode, stdout, stderr).
        """
        ssh_args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
        ]
        if self.ssh_key:
            ssh_args += ["-i", self.ssh_key]
        ssh_args += [self.gpu_host, command]

        logger.debug("SSH: %s", " ".join(ssh_args))
        try:
            proc = subprocess.run(
                ssh_args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            logger.error(
                "SSH command timed out after %ds: %s", timeout, command
            )
            return -1, "", "timeout"

    def install_if_needed(self) -> bool:
        """Install SGLang on the GPU host if not already present.

        Returns:
            True if sglang is available after this call.
        """
        logger.info("Checking SGLang installation on %s...", self.gpu_host)
        rc, _, _ = self._ssh_cmd("python -m sglang.launch_server --help")
        if rc == 0:
            logger.info("SGLang already installed on %s", self.gpu_host)
            return True

        logger.info("Installing SGLang on %s...", self.gpu_host)
        rc, _, stderr = self._ssh_cmd(
            "pip install 'sglang[all]'", timeout=600
        )
        if rc != 0:
            logger.error(
                "Failed to install SGLang on %s: %s",
                self.gpu_host,
                stderr[:500],
            )
            return False

        logger.info("SGLang installed successfully on %s", self.gpu_host)
        return True

    def start_server(self) -> bool:
        """Start the SGLang server on the remote GPU host.

        Returns:
            True if the server started and is responding.
        """
        # Kill any existing server on that port first
        self._ssh_cmd(
            f"pkill -f 'sglang.launch_server.*--port {self.remote_port}'"
            " || true"
        )
        time.sleep(2)

        cmd_parts = [
            "python", "-m", "sglang.launch_server",
            "--model-path", shlex.quote(self.model_name),
            "--port", str(self.remote_port),
            "--host", "0.0.0.0",
        ]
        if self.extra_args:
            cmd_parts.append(self.extra_args)

        launch_cmd = " ".join(cmd_parts)
        bg_cmd = (
            f"nohup {launch_cmd} "
            f"> /tmp/sglang_server.log 2>&1 & echo $!"
        )

        logger.info(
            "Starting SGLang server on %s: %s", self.gpu_host, launch_cmd
        )
        rc, stdout, stderr = self._ssh_cmd(bg_cmd, timeout=30)
        if rc != 0:
            logger.error("Failed to start SGLang server: %s", stderr[:500])
            return False

        pid = stdout.strip()
        logger.info(
            "SGLang server started with PID %s on %s", pid, self.gpu_host
        )
        self._server_started = True
        return self._wait_for_server_ready()

    def _wait_for_server_ready(
        self, timeout: int = 300, poll_interval: int = 10
    ) -> bool:
        """Poll /v1/models until the server responds.

        Args:
            timeout: Total seconds to wait.
            poll_interval: Seconds between polls.

        Returns:
            True if the server became ready within the timeout.
        """
        logger.info(
            "Waiting for SGLang server to be ready (timeout=%ds)...", timeout
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc, stdout, _ = self._ssh_cmd(
                "curl -s -o /dev/null -w '%%{http_code}' "
                f"http://localhost:{self.remote_port}/v1/models",
                timeout=15,
            )
            if rc == 0 and stdout.strip() == "200":
                logger.info(
                    "SGLang server is ready on port %d", self.remote_port
                )
                return True

            # Check if process is still alive
            rc2, _, _ = self._ssh_cmd(
                f"pgrep -f 'sglang.launch_server.*--port {self.remote_port}'",
                timeout=10,
            )
            if rc2 != 0:
                logger.error(
                    "SGLang server process died. "
                    "Check /tmp/sglang_server.log on %s",
                    self.gpu_host,
                )
                _, log_tail, _ = self._ssh_cmd(
                    "tail -20 /tmp/sglang_server.log", timeout=10
                )
                if log_tail:
                    logger.error("Server log tail:\n%s", log_tail)
                return False

            logger.info(
                "Server not ready yet, retrying in %ds...", poll_interval
            )
            time.sleep(poll_interval)

        logger.error(
            "SGLang server did not become ready within %ds", timeout
        )
        return False

    def start_tunnel(self) -> bool:
        """Set up an SSH tunnel from local_port to the remote SGLang server.

        Returns:
            True if the tunnel was established.
        """
        ssh_args = [
            "ssh", "-N", "-L",
            f"{self.local_port}:localhost:{self.remote_port}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
        ]
        if self.ssh_key:
            ssh_args += ["-i", self.ssh_key]
        ssh_args.append(self.gpu_host)

        logger.info(
            "Setting up SSH tunnel: localhost:%d -> %s:%d",
            self.local_port, self.gpu_host, self.remote_port,
        )
        try:
            self._tunnel_proc = subprocess.Popen(
                ssh_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            time.sleep(3)

            if self._tunnel_proc.poll() is not None:
                _, stderr = self._tunnel_proc.communicate(timeout=5)
                logger.error(
                    "SSH tunnel failed to start: %s",
                    stderr.decode()[:500] if stderr else "unknown error",
                )
                self._tunnel_proc = None
                return False

            # Verify tunnel
            try:
                resp = requests.get(
                    f"http://localhost:{self.local_port}/v1/models",
                    timeout=10,
                )
                if resp.status_code == 200:
                    logger.info(
                        "SSH tunnel verified: localhost:%d -> %s:%d",
                        self.local_port, self.gpu_host, self.remote_port,
                    )
                    return True
                logger.warning(
                    "Tunnel responded with status %d (may still work)",
                    resp.status_code,
                )
                return True
            except requests.RequestException as e:
                logger.error("Failed to verify tunnel: %s", e)
                return False

        except OSError as e:
            logger.error("Failed to start SSH tunnel: %s", e)
            return False

    def stop(self) -> None:
        """Stop the SSH tunnel and the remote SGLang server."""
        if self._tunnel_proc:
            logger.info(
                "Stopping SSH tunnel (PID %d)...", self._tunnel_proc.pid
            )
            self._tunnel_proc.terminate()
            try:
                self._tunnel_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._tunnel_proc.kill()
            self._tunnel_proc = None

        if self._server_started:
            logger.info("Stopping SGLang server on %s...", self.gpu_host)
            self._ssh_cmd(
                f"pkill -f 'sglang.launch_server.*--port {self.remote_port}'"
                " || true"
            )
            self._server_started = False


def setup_sglang_server(
    model_name: str,
    gpu_host: str,
    serve_config: SGLangServeConfig | None = None,
    ssh_key: str | None = None,
) -> SGLangServerManager | None:
    """Set up an SGLang server for a model on a remote GPU host.

    Handles installation, server start, readiness check, and SSH tunnel.

    Args:
        model_name: HuggingFace model ID (e.g. "Qwen/Qwen3.5-9B").
        gpu_host: SSH target (e.g. "user@gpu-server").
        serve_config: SGLang serving configuration.
        ssh_key: Optional path to SSH private key.

    Returns:
        SGLangServerManager if successful, None on failure.
    """
    if not serve_config:
        serve_config = SGLangServeConfig()

    manager = SGLangServerManager(
        gpu_host=gpu_host,
        model_name=model_name,
        remote_port=serve_config.port,
        extra_args=serve_config.args,
        ssh_key=ssh_key,
    )

    if not manager.install_if_needed():
        return None

    if not manager.start_server():
        return None

    if not manager.start_tunnel():
        manager.stop()
        return None

    return manager
