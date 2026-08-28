"""Integration contract for the exact Desktop and Tray socket implementations."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.socket_server import IPC_PROTOCOL_VERSION, DesktopSocketServer

TRAY_SOURCE = os.environ.get("OPENADAPT_TRAY_SOURCE")
pytestmark = pytest.mark.skipif(
    not TRAY_SOURCE,
    reason="the exact Tray source is supplied by the qualification job",
)


class _Dispatcher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.event_received = threading.Event()
        self.emit = self._capture

    def _capture(self, event: str, data: dict) -> None:
        self.events.append((event, data))
        self.event_received.set()

    def dispatch(self, cmd: str, params: dict) -> dict:
        if cmd == "get_status":
            return {
                "recording": True,
                "paused": False,
                "capture_id": "capture-1",
            }
        if cmd == "open_workflow_library":
            self.emit("open_window", {"view": "workflow_library"})
            return {"ok": True}
        raise AssertionError(f"unexpected Tray command: {cmd} {params}")


def test_exact_tray_client_uses_the_desktop_protocol(tmp_path: Path) -> None:
    tray_source = Path(str(TRAY_SOURCE)).resolve()
    ipc_path = tray_source / "openadapt_tray" / "ipc.py"
    assert ipc_path.is_file()
    module_name = "openadapt_exact_tray_ipc"
    spec = importlib.util.spec_from_file_location(module_name, ipc_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    IPCClient = module.IPCClient
    IPCMessageType = module.IPCMessageType

    discovery = tmp_path / "desktop_ipc.json"
    dispatcher = _Dispatcher()
    server = DesktopSocketServer(
        EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING"),
        discovery_path=discovery,
        dispatcher=dispatcher,  # type: ignore[arg-type]
    )
    server.start()
    client = IPCClient.from_discovery(discovery)
    assert client is not None
    incompatible = tmp_path / "incompatible_desktop_ipc.json"
    incompatible.write_text(
        json.dumps(
            {
                "protocol_version": IPC_PROTOCOL_VERSION + 1,
                "host": server.host,
                "port": server.port,
                "token": server.token,
            }
        ),
        encoding="utf-8",
    )
    assert IPCClient.from_discovery(incompatible) is None

    status_received = threading.Event()
    compile_received = threading.Event()
    recording_received = threading.Event()
    observed: dict[str, dict] = {}

    def remember(name: str, ready: threading.Event):
        def handler(message) -> None:
            observed[name] = message.data or {}
            ready.set()

        return handler

    client.register_handler(
        IPCMessageType.STATUS_UPDATE,
        remember("status", status_received),
    )
    client.register_handler(
        IPCMessageType.COMPILE_PROGRESS,
        remember("compile", compile_received),
    )
    client.register_handler(
        IPCMessageType.RECORDING_STARTED,
        remember("recording", recording_received),
    )

    try:
        assert client.connect() is True
        assert status_received.wait(5)
        assert observed["status"]["recording"] is True
        assert observed["status"]["capture_id"] == "capture-1"

        server._broadcast(
            "compile_progress",
            {"state": "compiled", "capture_id": "capture-1"},
        )
        assert compile_received.wait(5)
        assert observed["compile"]["state"] == "compiled"
        assert observed["compile"]["capture_id"] == "capture-1"

        server._broadcast("recording_started", {"capture_id": "capture-1"})
        assert recording_received.wait(5)
        assert observed["recording"]["capture_id"] == "capture-1"

        dispatcher.event_received.clear()
        assert client.send_open_workflow_library() is True
        assert dispatcher.event_received.wait(5)
        assert dispatcher.events[-1] == ("open_window", {"view": "workflow_library"})

        endpoint = json.loads(discovery.read_text(encoding="utf-8"))
        assert endpoint["protocol_version"] == IPC_PROTOCOL_VERSION
        assert endpoint["host"] == "127.0.0.1"
        assert endpoint["port"] == server.port
        assert endpoint["token"] == server.token
        assert server.dispatcher is dispatcher
    finally:
        client.close()
        server.stop()

    assert not discovery.exists()
