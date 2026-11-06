"""Owned loopback lifecycle for the synthetic RDP campaign; never an app engine."""

from __future__ import annotations

import hashlib
import io
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from PIL import Image

from openadapt_flow.backends.xfreerdp import (
    RdpTarget,
    SessionProfile,
    XFreeRDPTransport,
    _claim_display,
    _release_display,
)
from openadapt_flow.qualify.linux_pixel_fault_renderer import display_identity
from openadapt_flow.qualify.linux_pixel_fault_task import LinuxPixelTasks
from openadapt_flow.qualify.rdp_pixel_fault_controller import (
    open_rdp_qualification_session,
)
from tests.fixtures import rdp_write_app as app

PROFILE = SessionProfile(
    width=1280, height=800, color_depth=24, scale=100, lossless=True
)


def owned_listener(process: subprocess.Popen[bytes], port: int) -> bool:
    if process.poll() is not None:
        return False
    sockets = {os.readlink(path) for path in Path(f"/proc/{process.pid}/fd").iterdir()}
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if (
            fields[1] == f"0100007F:{port:04X}"
            and fields[3] == "0A"
            and f"socket:[{fields[9]}]" in sockets
        ):
            return process.poll() is None
    return False


class LoopbackSession:
    """Fresh display, app, loopback shadow and transport, closed by exact handles."""

    def __init__(self, root: Path, database: Path) -> None:
        self.root = root
        root.mkdir(exist_ok=False)
        self.database = database
        self.processes: list[subprocess.Popen[bytes]] = []
        self.transport: XFreeRDPTransport | None = None
        self.display: int | None = None
        self.claim_identity: tuple[int, int] | None = None
        self.owners: list[LinuxPixelTasks] = []
        self.closed = False
        self.cleanup_failed = False
        self.version = (
            "sha256:" + hashlib.sha256(Path(app.__file__).read_bytes()).hexdigest()
        )
        self.phase = "NEW"

    def _spawn(self, argv: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            argv,
            env=env,
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(child)
        return child

    def _wait(self, predicate: Any, seconds: float = 10) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in self.processes):
                raise RuntimeError("owned loopback child exited during setup")
            try:
                if predicate():
                    return
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            time.sleep(0.05)
        raise TimeoutError("owned loopback setup did not become ready")

    def start(self):
        try:
            self.display = _claim_display()
            claim = Path(f"/tmp/.oaflow-xfreerdp-{self.display}.claim").stat()
            self.claim_identity = (claim.st_dev, claim.st_ino)
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.root),
                "DISPLAY": f":{self.display}",
            }
            self.phase = "XSERVER"
            self.server = self._spawn(
                [
                    "Xvfb",
                    env["DISPLAY"],
                    "-screen",
                    "0",
                    "1280x800x24",
                    "-nolisten",
                    "tcp",
                ],
                env,
            )
            self._wait(
                lambda: display_identity(env["DISPLAY"])["pid"] == self.server.pid
            )
            self.identity = display_identity(env["DISPLAY"])
            self.phase = "APP"
            self.app = self._spawn(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    str(Path(app.__file__).resolve()),
                    str(self.database),
                ],
                env,
            )
            # Let the actual app render; frame probes below are the readiness proof.
            self.phase = "SHADOW"
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            self.shadow = self._spawn(
                [
                    "freerdp-shadow-cli3",
                    f"/port:{port}",
                    "/bind-address:127.0.0.1",
                    "-auth",
                ],
                env,
            )
            self._wait(lambda: owned_listener(self.shadow, port))
            self.phase = "TRANSPORT"
            self.transport = XFreeRDPTransport(
                RdpTarget(
                    "127.0.0.1", port, username="synthetic", ignore_certificate=True
                ),
                password=lambda: "synthetic",
                profile=PROFILE,
            )
            self.transport.connect()

            def probe(index: int, value: str):
                expected = Image.open(io.BytesIO(app.marker_bytes(value))).convert(
                    "RGB"
                )
                x, y = app.MARKERS[index]

                def observed(png: bytes) -> bool:
                    if (
                        any(p.poll() is not None for p in self.processes)
                        or display_identity(env["DISPLAY"]) != self.identity
                        or not owned_listener(self.shadow, port)
                    ):
                        return False
                    with Image.open(io.BytesIO(png)) as image:
                        return (
                            image.size == (1280, 800)
                            and image.convert("RGB")
                            .crop((x, y, x + 52, y + 52))
                            .tobytes()
                            == expected.tobytes()
                        )

                return observed

            app_probe, version_probe, env_probe = (
                probe(i, value)
                for i, value in enumerate((app.APP, self.version, app.ENVIRONMENT))
            )

            def initial_frame_ready():
                image, _, _ = self.transport.framebuffer()
                out = io.BytesIO()
                image.save(out, format="PNG")
                return all(
                    p(out.getvalue()) for p in (app_probe, version_probe, env_probe)
                )

            self._wait(initial_frame_ready)

            def factory(binding: str):
                owner = LinuxPixelTasks(
                    server=self.server,
                    children=(self.app, self.shadow),
                    display=env["DISPLAY"],
                    viewport=(1280, 800),
                    home=self.root,
                    session_binding_sha256=binding,
                )
                self.owners.append(owner)
                return owner

            self.phase = "PAIRING"
            backend, close = open_rdp_qualification_session(
                self.transport,
                None,
                application_marker=app.APP,
                application_probe=app_probe,
                application_version_marker=self.version,
                application_version_probe=version_probe,
                environment_marker=app.ENVIRONMENT,
                environment_probe=env_probe,
                witness_region=(1200, 730, 52, 52),
                excluded_regions=((0, 80, 1000, 600), (1030, 0, 190, 80)),
                guest_dir="",
                close_owner=self.close,
                lifetime_seconds=300,
                task_owner_factory=factory,
            )
            self.phase = "READY"
            return backend, close
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.closed:
            if self.cleanup_failed:
                raise RuntimeError("loopback cleanup previously failed")
            return
        self.closed = True
        errors = []
        for owner in reversed(self.owners):
            try:
                owner.close()
            except Exception:
                errors.append("overlay")
        if self.transport is not None:
            try:
                children = tuple(self.transport._procs)
                self.transport.disconnect()
                for child in children:
                    child.wait(timeout=5)
            except Exception:
                errors.append("transport")
        for process in reversed(self.processes):
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except Exception:
                errors.append("child")
        if self.display is not None and not errors:
            path = Path(f"/tmp/.oaflow-xfreerdp-{self.display}.claim")
            try:
                current = path.stat()
                if (
                    current.st_dev,
                    current.st_ino,
                ) != self.claim_identity or path.read_text() != str(os.getpid()):
                    raise ValueError("display claim changed")
                if (
                    Path(f"/tmp/.X11-unix/X{self.display}").exists()
                    or Path(f"/tmp/.X{self.display}-lock").exists()
                ):
                    raise ValueError("display resources remain")
                _release_display(self.display)
            except Exception:
                errors.append("claim")
        self.cleanup_failed = bool(errors)
        if errors:
            raise RuntimeError("owned loopback cleanup failed")
