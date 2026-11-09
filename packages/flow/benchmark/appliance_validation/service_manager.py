"""Start / stop the real MLX-backed VLM service as a subprocess and wait for it.

This is validation infrastructure only -- it does NOT touch the shipped runtime.
It launches the exact production entrypoint

    VLM_BACKEND=mlx VLM_MODEL=<model> VLM_SERVICE_TOKEN=<tok> \
        python -m openadapt_flow.services.vlm_service --port <port>

so the harness drives the same server a customer would run on the GPU box, and
then talks to it only through the fail-safe clients in
``openadapt_flow.runtime.remote_vlm`` (never the model directly).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class ServiceHandle:
    proc: subprocess.Popen
    base_url: str
    token: str
    port: int

    def rss_mb(self) -> Optional[float]:
        """Resident set size of the service process in MiB (best effort)."""
        try:
            out = subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(self.proc.pid)], text=True
            )
            return int(out.strip()) / 1024.0
        except Exception:
            return None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def start_service(
    *,
    model: str,
    token: str = "test",
    port: int = 8077,
    host: str = "127.0.0.1",
    ready_timeout_s: float = 600.0,
    log_path: Optional[str] = None,
) -> tuple[ServiceHandle, float]:
    """Launch the MLX-backed service and block until ``/ready`` reports ready.

    Returns ``(handle, model_load_seconds)`` where ``model_load_seconds`` is the
    wall time from process start to the first ``ready: true`` (dominated by the
    one-time model load in the FastAPI lifespan).

    Raises ``RuntimeError`` if the process dies or never becomes ready.
    """
    env = dict(os.environ)
    # ZERO Anthropic calls anywhere in this study.
    env.pop("ANTHROPIC_API_KEY", None)
    env["VLM_BACKEND"] = "mlx"
    env["VLM_MODEL"] = model
    env["VLM_SERVICE_TOKEN"] = token

    log_file = open(log_path, "w") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openadapt_flow.services.vlm_service",
            "--host",
            host,
            "--port",
            str(port),
        ],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://{host}:{port}"
    handle = ServiceHandle(proc=proc, base_url=base_url, token=token, port=port)

    t0 = time.time()
    deadline = t0 + ready_timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"service exited early with code {proc.returncode}; see {log_path}"
            )
        try:
            r = httpx.get(f"{base_url}/ready", timeout=2.0)
            if r.status_code == 200 and r.json().get("ready") is True:
                return handle, time.time() - t0
        except httpx.HTTPError:
            pass
        time.sleep(0.5)

    handle.stop()
    raise RuntimeError(f"service not ready within {ready_timeout_s}s; see {log_path}")
