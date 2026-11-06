"""Real Linux CLI startup/health proof; no desktop capture or input is requested."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.win_agent_capture_telemetry import safe_projection


pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="owned Linux /proc socket startup proof"
)


def _owned_listeners(process: subprocess.Popen[bytes]) -> list[tuple[str, int, str]]:
    """Resolve only sockets held by our live child, including port-zero binding."""
    assert process.poll() is None, "telemetry module exited before readiness"
    owned = set()
    for descriptor in Path(f"/proc/{process.pid}/fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            owned.add(target[8:-1])
    listeners = []
    for family in ("tcp", "tcp6"):
        table = Path(f"/proc/{process.pid}/net/{family}").read_text()
        for line in table.splitlines()[1:]:
            fields = line.split()
            if fields[3] != "0A" or fields[9] not in owned:
                continue
            address, port = fields[1].split(":")
            listeners.append((address, int(port, 16), fields[9]))
    return listeners


def _close_owned(process: subprocess.Popen[bytes], listener_inode: str | None) -> None:
    """Close only the Popen we created; reap it even when an assertion fails."""
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    else:
        process.wait(timeout=5)
    assert process.poll() is not None, "owned module process was not reaped"
    if listener_inode is not None:
        for family in ("tcp", "tcp6"):
            rows = Path(f"/proc/self/net/{family}").read_text().splitlines()[1:]
            assert all(row.split()[9] != listener_inode for row in rows), (
                "owned listening socket survived child cleanup"
            )


def test_exact_module_starts_original_server_and_serves_health(tmp_path: Path) -> None:
    sidecar = tmp_path / "capture-telemetry.json"
    home = tmp_path / "home"
    home.mkdir()
    flow_root = Path(__file__).resolve().parents[1]
    # This explicit environment does not inherit real token/TLS/display settings.
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.e2e.win_agent_capture_telemetry",
            "--capture-telemetry-sidecar",
            str(sidecar),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--token",
            "synthetic-startup-only-token",
        ],
        cwd=flow_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    listener_inode = None
    connection = None
    try:
        deadline = time.monotonic() + 15
        listeners = []
        while time.monotonic() < deadline:
            listeners = _owned_listeners(process)
            if listeners:
                break
            time.sleep(0.05)
        assert len(listeners) == 1, "owned module did not expose exactly one listener"
        address, port, listener_inode = listeners[0]
        assert address == "0100007F", "module listener was not IPv4 loopback"
        assert 0 < port < 65536
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200
        body = response.read(65537)
        assert len(body) <= 65536
        assert json.loads(body) == {
            "status": "ok",
            "agent": "openadapt_flow.win_agent",
            "active_console_session": -1,
            "auth_required": True,
            "capabilities": [
                "screenshot",
                "context_identity_v1",
                "context_environment_v1",
                "typed_input_v1",
                "guarded_input_v1",
                "uia_v1",
                "uia_read_v1",
            ],
        }
        assert process.poll() is None
        assert safe_projection(sidecar) == {"status": "available", "diagnoses": []}
        assert json.loads(sidecar.read_text()) == {
            "schema": "openadapt.test-win-agent-capture-telemetry/v1",
            "captures": [],
            "guarded_refusals": [],
        }
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            _close_owned(process, listener_inode)
