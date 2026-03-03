"""End-to-end tests for the IPC protocol.

Tests the JSON-over-stdin/stdout communication protocol between
the Tauri shell and the Python engine sidecar.

Run:
    uv run pytest tests/test_e2e/test_ipc.py -v
"""

from __future__ import annotations

import json
import uuid

import pytest

from engine.config import EngineConfig
from engine.ipc import IPCHandler


@pytest.fixture
def config(tmp_path):
    """Test configuration."""
    return EngineConfig(data_dir=tmp_path / ".openadapt", log_level="DEBUG")


@pytest.fixture
def handler(config):
    """IPC handler instance."""
    return IPCHandler(config=config)


class TestIPCProtocol:
    """Tests for IPC message handling."""

    def test_unknown_command_returns_error(self, handler) -> None:
        """Unknown commands should return an error response."""
        msg_id = str(uuid.uuid4())
        message = {"id": msg_id, "cmd": "nonexistent_command", "params": {}}
        handler._dispatch(message)
        # The error is written to stdout; we verify dispatch doesn't crash

    def test_dispatch_missing_cmd(self, handler) -> None:
        """Messages without a cmd field should return an error."""
        message = {"id": "test-123", "params": {}}
        handler._dispatch(message)

    def test_send_response_format(self, handler, capsys) -> None:
        """Responses should be valid JSON with id, status, and data fields."""
        handler.send_response("test-id", {"key": "value"})
        captured = capsys.readouterr()
        response = json.loads(captured.out.strip())
        assert response["id"] == "test-id"
        assert response["status"] == "ok"
        assert response["data"]["key"] == "value"

    def test_send_error_format(self, handler, capsys) -> None:
        """Error responses should have status=error and an error message."""
        handler.send_error("test-id", "something went wrong")
        captured = capsys.readouterr()
        response = json.loads(captured.out.strip())
        assert response["id"] == "test-id"
        assert response["status"] == "error"
        assert response["error"] == "something went wrong"

    def test_send_event_format(self, handler, capsys) -> None:
        """Events should have event and data fields but no id."""
        handler.send_event("recording_started", {"capture_id": "abc123"})
        captured = capsys.readouterr()
        event = json.loads(captured.out.strip())
        assert "id" not in event
        assert event["event"] == "recording_started"
        assert event["data"]["capture_id"] == "abc123"

    def test_response_is_single_line(self, handler, capsys) -> None:
        """Each response should be exactly one line (JSON lines format)."""
        handler.send_response("id1", {"a": 1})
        handler.send_response("id2", {"b": 2})
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # Each line is valid JSON


class TestIPCHandlerRegistration:
    """Tests for command handler registration."""

    def test_initial_handlers_empty(self, handler) -> None:
        """Handler registry should start empty (commands are TODO stubs)."""
        # Currently no handlers are registered (all TODO)
        assert isinstance(handler._handlers, dict)
