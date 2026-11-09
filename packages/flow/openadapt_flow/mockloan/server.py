"""Threaded static-file server for the MockLoan demo app.

Serves ``openadapt_flow/mockloan/static`` on localhost. No external resources
are referenced by the app, so tests never touch the network beyond localhost.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

STATIC_DIR = Path(__file__).resolve().parent / "static"


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that suppresses per-request stderr logging."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def serve(port: int = 0, *, host: str = "127.0.0.1") -> tuple[str, Callable[[], None]]:
    """Serve the MockLoan static app in a background thread.

    Args:
        port: TCP port to bind; ``0`` (default) picks an ephemeral port.
        host: Interface to bind; defaults to localhost only.

    Returns:
        ``(url, stop)`` where ``url`` is the app's base URL (trailing slash)
        and ``stop()`` shuts the server down and joins its thread.
    """
    handler = partial(_QuietHandler, directory=str(STATIC_DIR))
    httpd = ThreadingHTTPServer((host, port), handler)
    actual_port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, name="mockloan-http", daemon=True
    )
    thread.start()
    url = f"http://{host}:{actual_port}/"

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return url, stop
