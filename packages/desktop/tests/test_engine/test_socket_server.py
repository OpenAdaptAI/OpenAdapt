"""Tests for the tray-facing loopback socket server + discovery file (P0-1)."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.socket_server import DesktopSocketServer


@pytest.fixture
def server(tmp_path: Path):
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    disc = tmp_path / "desktop_ipc.json"
    srv = DesktopSocketServer(config, discovery_path=disc)
    srv.start()
    yield srv
    srv.stop()


def _connect(server: DesktopSocketServer) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect((server.host, server.port))
    return s


def _read_frame(sock: socket.socket) -> dict:
    buf = ""
    deadline = time.time() + 5.0
    while "\n" not in buf and time.time() < deadline:
        buf += sock.recv(4096).decode("utf-8")
    return json.loads(buf.split("\n", 1)[0])


class TestDiscoveryFile:
    def test_discovery_file_shape(self, server) -> None:
        """The tray reads {host, port, token} from the discovery file."""
        data = json.loads(server.discovery_path.read_text())
        assert data["host"] == "127.0.0.1"
        assert data["port"] == server.port
        assert data["token"] == server.token
        assert isinstance(data["token"], str) and len(data["token"]) >= 16

    def test_discovery_file_removed_on_stop(self, tmp_path: Path) -> None:
        config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
        disc = tmp_path / "d.json"
        srv = DesktopSocketServer(config, discovery_path=disc)
        srv.start()
        assert disc.exists()
        srv.stop()
        assert not disc.exists()


class TestCommandDispatch:
    def test_get_status_returns_status_update_event(self, server) -> None:
        """A tray get_status command streams back a status_update event."""
        sock = _connect(server)
        try:
            frame = {"type": "get_status", "data": {}, "token": server.token}
            sock.sendall((json.dumps(frame) + "\n").encode())
            event = _read_frame(sock)
        finally:
            sock.close()
        assert event["type"] == "status_update"
        assert "recording" in event["data"]

    def test_bad_token_rejected(self, server) -> None:
        """A frame with the wrong session token is rejected as unauthorized."""
        sock = _connect(server)
        try:
            frame = {"type": "get_status", "data": {}, "token": "wrong"}
            sock.sendall((json.dumps(frame) + "\n").encode())
            event = _read_frame(sock)
        finally:
            sock.close()
        assert event["type"] == "recording_error"
        assert event["data"]["error"] == "unauthorized"

    def test_engine_event_broadcast_to_client(self, server) -> None:
        """A tray-known engine event is streamed to the connected client."""
        sock = _connect(server)
        # Give the accept loop a moment to register the client.
        time.sleep(0.2)
        try:
            server._broadcast("sync_state", {"state": "pushing", "queued": 1})
            event = _read_frame(sock)
        finally:
            sock.close()
        assert event["type"] == "sync_state"
        assert event["data"]["state"] == "pushing"

    def test_non_tray_events_are_filtered(self, server) -> None:
        """Events the tray enum can't decode are never forwarded."""
        from engine.socket_server import _TRAY_EVENTS

        # replay_progress / log_line / open_window are not tray vocabulary.
        assert "replay_progress" not in _TRAY_EVENTS
        assert "log_line" not in _TRAY_EVENTS
        assert "open_window" not in _TRAY_EVENTS
        # Broadcasting one with no client connected must not raise.
        server._broadcast("replay_progress", {"state": "running"})
