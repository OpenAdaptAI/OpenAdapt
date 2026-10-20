#!/usr/bin/env python3
"""A loopback HTTP bridge from a browser page to a real Desktop engine.

The setup walk (``walk.py``) drives the Desktop UI from ``npm run dev`` in a
headless browser. The page reaches the engine through Tauri's ``invoke``; the
walk's init script answers ``engine_invoke`` by posting here instead, and this
process hands the command to a real :class:`EngineDispatcher` over a private
data directory. Engine events queue here and the page polls for them, so the
approval dialog and progress events behave as they do in the app.

Development and audit use only. It binds 127.0.0.1, accepts only the walk's
per-run token, and never starts with a user's data directory: ``--data-dir``
must be a fresh folder the walk created.
"""

from __future__ import annotations

import argparse
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openadapt_desktop.config import EngineConfig
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices
from openadapt_desktop.ipc import CONCURRENT_COMMANDS


class Bridge:
    """One dispatcher, one event queue, one log of every call the page made."""

    def __init__(self, data_dir: Path, token: str) -> None:
        self.token = token
        self.events: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # Commands run one at a time, as on the app's stdin wire (ipc.py),
        # except the two that answer a run waiting for a person.
        self._dispatch_lock = threading.RLock()
        config = EngineConfig(data_dir=data_dir / ".openadapt", log_level="WARNING")
        self.db = IndexDB(data_dir / "index.db")
        self.db.initialize()
        self.dispatcher = EngineDispatcher(
            config, services=EngineServices(config, db=self.db), emit=self._emit
        )

    def _emit(self, event: str, data: dict) -> None:
        with self._lock:
            self.events.append({"event": event, "payload": data})

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self.events = self.events, []
        return out

    def invoke(self, cmd: str, params: dict[str, Any]) -> Any:
        with self._lock:
            self.calls.append({"cmd": cmd, "params": sorted(params)})
        if cmd in CONCURRENT_COMMANDS:
            return self.dispatcher.dispatch(cmd, params)
        with self._dispatch_lock:
            return self.dispatcher.dispatch(cmd, params)


def serve(bridge: Bridge, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            return

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type, x-walk-token")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _reply(self, status: int, body: Any) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("x-walk-token") != bridge.token:
                self._reply(403, {"error": "bad token"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/events":
                self._reply(200, bridge.drain())
                return
            if self.path == "/calls":
                self._reply(200, bridge.calls)
                return
            try:
                result = bridge.invoke(str(request["cmd"]), dict(request.get("params") or {}))
                self._reply(200, {"ok": True, "result": result})
            except KeyError as exc:
                self._reply(200, {"ok": False, "error": f"unknown command {exc}"})
            except Exception as exc:  # the page sees the engine's failure
                traceback.print_exc()
                self._reply(200, {"ok": False, "error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    bridge = Bridge(args.data_dir, args.token)
    server = serve(bridge, args.port)
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
