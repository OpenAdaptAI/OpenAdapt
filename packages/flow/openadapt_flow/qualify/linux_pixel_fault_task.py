"""Owned Linux display children implementing the existing pixel task lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openadapt_flow.qualify import linux_pixel_fault_renderer as renderer


@dataclass(eq=False)
class LinuxPixelTaskHandle:
    process: subprocess.Popen[bytes]
    nonce: str
    binding: str
    guest_digest: str
    source_sha256: str
    request_sha256: str
    deadline: float
    ready_status: str | None = None
    refusal_stage: str | None = None


class LinuxPixelTasks:
    """Bind finite overlays to an actual owned X-server and its live children.

    Construct only inside a trusted Python session hook. ``server`` must be the
    Popen which created this display, not a PID found by scanning processes.
    Separate instances may bind the connection witness and then the observed
    guest digest. A manager cannot adopt or close another manager's handles.
    """

    def __init__(
        self,
        *,
        server: subprocess.Popen[bytes],
        children: tuple[subprocess.Popen[bytes], ...],
        display: str,
        viewport: tuple[int, int],
        home: Path,
        session_binding_sha256: str,
    ) -> None:
        if not renderer.HEX64.fullmatch(session_binding_sha256):
            raise ValueError("invalid session binding")
        if server.poll() is not None or any(
            child.poll() is not None for child in children
        ):
            raise ValueError("owned display session is not live")
        self._identity = renderer.display_identity(display)
        if self._identity["pid"] != server.pid:
            raise ValueError("display peer does not belong to the owned server")
        self._server = server
        self._children = children
        self._display = display
        self._viewport = viewport
        self._home = home.resolve(strict=True)
        if not self._home.is_dir():
            raise ValueError("owned runtime directory required")
        self._binding = session_binding_sha256
        self._source = Path(renderer.__file__).resolve(strict=True)
        self._source_digest = hashlib.sha256(self._source.read_bytes()).hexdigest()
        self._handles: list[LinuxPixelTaskHandle] = []
        self._nonces: set[str] = set()
        self._closed = False
        self._cleanup_failed = False

    def _current(self) -> bool:
        try:
            return (
                not self._closed
                and self._server.poll() is None
                and all(child.poll() is None for child in self._children)
                and renderer.display_identity(self._display) == self._identity
                and hashlib.sha256(self._source.read_bytes()).hexdigest()
                == self._source_digest
            )
        except Exception:
            return False

    def launch(self, request: dict[str, Any]) -> LinuxPixelTaskHandle:
        if not self._current():
            raise ValueError("owned display generation is unavailable")
        renderer.validate_request(request, self._binding, self._viewport)
        nonce = request["nonce"]
        if nonce in self._nonces:
            raise ValueError("task nonce already used")
        self._nonces.add(nonce)
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        envelope = json.dumps(
            {
                "display": self._display,
                "identity": self._identity,
                "viewport": self._viewport,
                "binding": self._binding,
                "request": request,
            }
        ).encode()
        if len(envelope) > 16384:
            raise ValueError("request too large")
        lifetime = (
            request["lifetime_seconds"] if request["schema"] == renderer.PAIRING else 8
        )
        started = time.monotonic()
        process = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-S", str(self._source)],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(self._home),
                "DISPLAY": self._display,
            },
            cwd=self._home,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        handle = LinuxPixelTaskHandle(
            process,
            nonce,
            self._binding,
            renderer.identity_digest(self._identity),
            self._source_digest,
            hashlib.sha256(encoded).hexdigest(),
            started + lifetime,
        )
        # Record ownership before writing stdin: a broken pipe still needs close.
        self._handles.append(handle)
        try:
            assert process.stdin is not None
            process.stdin.write(envelope)
            process.stdin.close()
            if not self._current():
                raise ValueError("display changed during launch")
            return handle
        except Exception:
            self.close()
            raise

    def wait_ready(
        self, handle: LinuxPixelTaskHandle, *, timeout_s: float = 15
    ) -> str | None:
        if (
            not any(handle is owned for owned in self._handles)
            or type(timeout_s) not in (int, float)
            or not 0 < timeout_s <= 15
        ):
            return None
        deadline = min(handle.deadline, time.monotonic() + timeout_s)
        expected = f"ready:{handle.nonce}:{handle.guest_digest}"
        stream = handle.process.stdout
        if stream is None or handle.ready_status is not None:
            return None
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            data = bytearray()
            while (
                time.monotonic() < deadline
                and self._current()
            ):
                if not selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    if handle.process.poll() is not None:
                        return None
                    continue
                chunk = os.read(stream.fileno(), 256)
                if not chunk or len(data) + len(chunk) > 128:
                    return None
                data.extend(chunk)
                if b"\n" in data:
                    for stage in renderer.REFUSAL_STAGES:
                        if bytes(data) == f"refused:{stage}\n".encode("ascii"):
                            handle.refusal_stage = stage
                            return None
                    if bytes(data) != (expected + "\n").encode("ascii"):
                        return None
                    if (
                        self._current()
                        and handle.process.poll() is None
                        and time.monotonic() < deadline
                    ):
                        handle.ready_status = expected
                        return expected
                    return None
        return None

    def close(self) -> None:
        if self._closed:
            if self._cleanup_failed:
                raise RuntimeError("owned pixel task cleanup failed")
            return
        self._closed = True
        failed = False
        for handle in reversed(self._handles):
            process = handle.process
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
            except Exception:
                failed = True
        self._cleanup_failed = failed
        if failed:
            raise RuntimeError("owned pixel task cleanup failed")
