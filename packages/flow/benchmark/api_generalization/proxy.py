"""A reverse proxy in front of each app that injects faults on the save request.

Every request from the browser and from the API step goes through this
proxy, so a fault hits both arms at the same place: the app's front door.
The mode applies to the next save, once. A save is a POST whose path matches
``path_regex``, whose body matches ``body_require`` when set (an endpoint
shared by several actions) and does not match ``body_exclude`` (an app whose
page script posts the same form for a preview):

- ``pass``: forward unchanged.
- ``reject``: answer 503 without forwarding (nothing is written).
- ``partial``: forward, let the app commit, then answer 502 with an empty
  body (the write happened, the response was lost).
- ``duplicate``: forward the same request twice and answer with the second
  response (an at-least-once retry by something between client and app).

Each request is logged to ``log_path`` (one JSON line) so the harness can
count how many times a save reached the app.

    python -m benchmark.api_generalization.proxy --listen 9822 --upstream 9821 --state <dir>
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade"}


class Control:
    """Mode file shared with the harness (``mode.json`` in the state dir)."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.mode_path = state / "mode.json"
        self.log_path = state / "requests.jsonl"
        self.lock = threading.Lock()

    def take(self, method: str, path: str, body: bytes) -> tuple[str, bool]:
        """The fault for this request (consumed if it applies), and whether it is a save."""
        with self.lock:
            try:
                mode = json.loads(self.mode_path.read_text())
            except (OSError, ValueError):
                return "pass", False
            text = body.decode("latin-1")
            is_save = (method == "POST" and bool(re.search(mode.get("path_regex", "$^"), path))
                       and not (mode.get("body_exclude") and re.search(mode["body_exclude"], text))
                       and not (mode.get("body_require") and not re.search(mode["body_require"], text)))
            if mode.get("mode", "pass") == "pass" or not is_save:
                return "pass", is_save
            self.mode_path.write_text(json.dumps({**mode, "mode": "pass", "consumed": mode["mode"]}))
            return str(mode["mode"]), True

    def log(self, row: dict) -> None:
        with self.lock, open(self.log_path, "a") as fh:
            fh.write(json.dumps(row) + "\n")


def make_handler(upstream_host: str, upstream_port: int, control: Control):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # quiet
            pass

        def _forward(self, body: bytes) -> tuple[int, str, list[tuple[str, str]], bytes]:
            conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=120)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers["Connection"] = "close"
            conn.request(self.command, self.path, body=body or None, headers=headers)
            r = conn.getresponse()
            data = r.read()
            out = (r.status, r.reason, [(k, v) for k, v in r.getheaders() if k.lower() not in HOP], data)
            conn.close()
            return out

        def _reply(self, status: int, reason: str, headers: list[tuple[str, str]], data: bytes) -> None:
            self.send_response(status, reason)
            for k, v in headers:
                if k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self.close_connection = True

        def _handle(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            fault, is_save = control.take(self.command, self.path.split("?")[0], body)
            t0 = time.time()
            status = None
            try:
                if fault == "reject":
                    status = 503
                    self._reply(503, "Service Unavailable", [("Content-Type", "text/plain")], b"injected fault: reject")
                elif fault == "partial":
                    status, *_ = self._forward(body)
                    self._reply(502, "Bad Gateway", [("Content-Type", "text/plain")], b"")
                    status = f"{status}->502"
                elif fault == "duplicate":
                    first, *_ = self._forward(body)
                    st, reason, headers, data = self._forward(body)
                    self._reply(st, reason, headers, data)
                    status = f"{first}+{st}"
                else:
                    st, reason, headers, data = self._forward(body)
                    self._reply(st, reason, headers, data)
                    status = st
            finally:
                control.log({"t": round(t0, 3), "method": self.command, "path": self.path.split("?")[0],
                             "save": is_save, "fault": fault, "status": status, "ms": round((time.time() - t0) * 1000)})

        do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle

    return Handler


def serve(listen: int, upstream: int, state: Path, upstream_host: str = "127.0.0.1") -> ThreadingHTTPServer:
    state.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", listen), make_handler(upstream_host, upstream, Control(state)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--upstream", type=int, required=True)
    ap.add_argument("--state", required=True)
    a = ap.parse_args()
    serve(a.listen, a.upstream, Path(a.state))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
