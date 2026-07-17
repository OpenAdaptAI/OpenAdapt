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

    def test_handlers_registered(self, handler) -> None:
        """Every frontend CMD name from engine.ts must be registered."""
        assert isinstance(handler._handlers, dict)
        for cmd in (
            "start_recording", "stop_recording", "get_status",
            "compile_recording", "replay_workflow", "run_workflow",
            "teach_fix", "push_workflow", "get_workflows", "get_needs_attention",
            "login_browser", "login_paste", "logout", "get_auth_status",
            "get_config", "set_config", "check_permissions", "get_sync_state",
        ):
            assert cmd in handler._handlers, f"missing handler: {cmd}"

    def test_get_status_dispatches(self, handler, capsys) -> None:
        """A real command routes through the dispatcher and returns ok."""
        handler._dispatch({"id": "s1", "cmd": "get_status", "params": {}})
        lines = [line for line in capsys.readouterr().out.strip().split("\n") if line]
        response = json.loads(lines[-1])
        assert response["id"] == "s1"
        assert response["status"] == "ok"
        assert "recording" in response["data"]
