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
        self.readiness_first: dict[str, Any] | None = None
        self.readiness_last: dict[str, Any] | None = None
        self._readiness_current: dict[str, Any] | None = None

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

    def _readiness_check(self, marker: str, check: str, read: Any) -> bool:
        observation = self._readiness_current
        if observation is not None:
            observation["stage"] = f"{marker}.{check}"
        value = read()
        if observation is not None:
            observation["checks"][marker][check] = value
        return value

    def _marker_probe(self, index: int, value: str, display: str, port: int):
        expected = Image.open(io.BytesIO(app.marker_bytes(value))).convert("RGB")
        x, y = app.MARKERS[index]
        marker = ("application", "version", "environment")[index]

        def observed(png: bytes) -> bool:
            if (
                not self._readiness_check(
                    marker,
                    "children_alive",
                    lambda: not any(p.poll() is not None for p in self.processes),
                )
                or not self._readiness_check(
                    marker,
                    "display_matches",
                    lambda: display_identity(display) == self.identity,
                )
                or not self._readiness_check(
                    marker,
                    "listener_owned",
                    lambda: owned_listener(self.shadow, port),
                )
            ):
                return False
            with Image.open(io.BytesIO(png)) as image:
                return self._readiness_check(
                    marker,
                    "pixels_match",
                    lambda: (
                        image.size == (1280, 800)
                        and image.convert("RGB").crop((x, y, x + 52, y + 52)).tobytes()
                        == expected.tobytes()
                    ),
                )

        return observed

    def _project_readiness_frame(self, image: Image.Image, observation: dict) -> None:
        # Only the already captured frame and three predetermined synthetic crops.
        width, height = image.size
        if any(type(v) is not int or not 0 < v <= 8192 for v in (width, height)):
            raise ValueError("invalid diagnostic geometry")
        observation["frame_size"] = [width, height]
        for name, (x, y), value in zip(
            ("application", "version", "environment"),
            app.MARKERS,
            (app.APP, self.version, app.ENVIRONMENT),
        ):
            expected = Image.open(io.BytesIO(app.marker_bytes(value))).convert("RGB")
            actual = image.convert("RGB").crop((x, y, x + 52, y + 52))
            observation["markers"][name] = {
                "region": [x, y, 52, 52],
                "expected_rgb_sha256": hashlib.sha256(expected.tobytes()).hexdigest(),
                "observed_rgb_sha256": hashlib.sha256(actual.tobytes()).hexdigest(),
                "matches": actual.tobytes() == expected.tobytes(),
            }

    def _initial_frame_ready(self, probes: Any) -> bool:
        observation: dict[str, Any] = {
            "schema": "openadapt.rdp-frame-readiness/v1",
            "stage": "framebuffer",
            "exception_category": None,
            "frame_size": None,
            "diagnostic_error": False,
            "markers": {
                name: None for name in ("application", "version", "environment")
            },
            "checks": {
                name: dict.fromkeys(
                    (
                        "children_alive",
                        "display_matches",
                        "listener_owned",
                        "pixels_match",
                    )
                )
                for name in ("application", "version", "environment")
            },
        }
        self._readiness_current = observation
        image = None
        try:
            image, _, _ = self.transport.framebuffer()
            observation["stage"] = "encode"
            out = io.BytesIO()
            image.save(out, format="PNG")
            return all(p(out.getvalue()) for p in probes)
        except BaseException as exc:
            observation["exception_category"] = (
                "file_unavailable"
                if isinstance(exc, FileNotFoundError)
                else "connection_refused"
                if isinstance(exc, ConnectionRefusedError)
                else "other"
            )
            raise
        finally:
            self._readiness_current = None
            try:
                if image is not None:
                    self._project_readiness_frame(image, observation)
            except Exception:
                observation["diagnostic_error"] = True
                observation["frame_size"] = None
                observation["markers"] = dict.fromkeys(
                    ("application", "version", "environment")
                )
            self.readiness_last = observation
            if self.readiness_first is None:
                self.readiness_first = observation

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

            probes = tuple(
                self._marker_probe(i, value, env["DISPLAY"], port)
                for i, value in enumerate((app.APP, self.version, app.ENVIRONMENT))
            )
            app_probe, version_probe, env_probe = probes
            self._wait(lambda: self._initial_frame_ready(probes))

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
