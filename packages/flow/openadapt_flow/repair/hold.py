"""Keep a halted run's app open so a repair can be demonstrated on it.

``openadapt-flow run --hold-on-halt SECONDS`` calls :func:`hold_for_repair`
after a run halts, before the browser closes. It writes
``<run_dir>/repair-hold.json`` (mode 0600) naming a Unix socket in a private
0700 directory and a random secret, then answers JSON requests on that socket:

- ``{"secret": ..., "op": "viewport"}`` -> ``{"ok": true, "viewport": [w, h]}``
- ``{"secret": ..., "op": "screenshot"}`` -> ``{"ok": true, "png_base64": ...}``
- ``{"secret": ..., "op": "click", "x": X, "y": Y}`` -> ``{"ok": true}``
- ``{"secret": ..., "op": "release"}`` -> ends the hold

The hold ends on ``release``, after ``timeout_s``, or on SIGTERM. Either way the socket,
its directory, and the hold file are removed, and the caller closes the
browser as usual. Only openadapt-agent's teach_repair path uses this; it
never acts on its own.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import signal
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["HOLD_FILE", "hold_for_repair", "request"]

HOLD_FILE = "repair-hold.json"
_MAX_REQUEST = 4096


def _reply(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall(json.dumps(payload).encode() + b"\n")


def _handle(backend: Any, message: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    op = message.get("op")
    if op == "viewport":
        viewport = backend.viewport
        viewport = viewport() if callable(viewport) else viewport
        return {"ok": True, "viewport": [int(viewport[0]), int(viewport[1])]}, False
    if op == "screenshot":
        return {
            "ok": True,
            "png_base64": base64.b64encode(backend.screenshot()).decode(),
        }, False
    if op == "click":
        x, y = message.get("x"), message.get("y")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y)):
            return {"ok": False, "error": "x and y must be integers"}, False
        backend.click(x, y)
        return {"ok": True}, False
    if op == "release":
        return {"ok": True}, True
    return {"ok": False, "error": "unknown op"}, False


def hold_for_repair(
    backend: Any, run_dir: Path | str, timeout_s: float, *, exit_code: int | None = None
) -> str:
    """Serve the held backend until released or timed out. Returns why it ended."""
    run_dir = Path(run_dir)
    private = Path(tempfile.mkdtemp(prefix="oa-hold-"))
    os.chmod(private, 0o700)
    sock_path = private / "s"
    secret = secrets.token_urlsafe(32)
    hold_file = run_dir / HOLD_FILE
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ended = "timeout"
    stopping: list[int] = []
    previous = None
    if threading.current_thread() is threading.main_thread():
        # The parent ending the hold (SIGTERM) must still clean up and let the
        # caller close the browser, so turn it into a normal end of the hold.
        previous = signal.signal(
            signal.SIGTERM, lambda signum, frame: stopping.append(signum)
        )
    try:
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(1)
        deadline = time.monotonic() + timeout_s
        fd = os.open(hold_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "socket": str(sock_path),
                    "secret": secret,
                    "pid": os.getpid(),
                    "expires_in_s": timeout_s,
                    "exit_code": exit_code,
                },
                handle,
            )
        while True:
            remaining = deadline - time.monotonic()
            if stopping:
                ended = "terminated"
                break
            if remaining <= 0:
                break
            server.settimeout(min(remaining, 1.0))
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(30)
                try:
                    data = b""
                    while not data.endswith(b"\n") and len(data) < _MAX_REQUEST:
                        chunk = conn.recv(_MAX_REQUEST)
                        if not chunk:
                            break
                        data += chunk
                    message = json.loads(data.decode() or "{}")
                    if not isinstance(message, dict) or not secrets.compare_digest(
                        str(message.get("secret", "")), secret
                    ):
                        _reply(conn, {"ok": False, "error": "refused"})
                        continue
                    response, done = _handle(backend, message)
                    _reply(conn, response)
                    if done:
                        ended = "released"
                        break
                except Exception as exc:  # one bad request never ends the hold
                    try:
                        _reply(conn, {"ok": False, "error": type(exc).__name__})
                    except OSError:
                        pass
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        server.close()
        hold_file.unlink(missing_ok=True)
        shutil.rmtree(private, ignore_errors=True)
    return ended


def request(
    hold: dict[str, Any], payload: dict[str, Any], timeout_s: float = 30
) -> dict[str, Any]:
    """Send one request to a held run (client side, for openadapt-agent)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout_s)
        client.connect(hold["socket"])
        client.sendall(
            json.dumps({**payload, "secret": hold["secret"]}).encode() + b"\n"
        )
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(1 << 20)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())
