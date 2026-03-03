"""IPC handler -- JSON-over-stdin/stdout protocol for Tauri <-> Python communication.

Messages are JSON objects, one per line, sent over stdin (commands from Tauri)
and stdout (responses/events to Tauri).

Command format (Tauri -> Python):
    {"id": "uuid", "cmd": "start_recording", "params": {"quality": "standard"}}

Response format (Python -> Tauri):
    {"id": "uuid", "status": "ok", "data": {...}}
    {"id": "uuid", "status": "error", "error": "message"}

Event format (Python -> Tauri, no id):
    {"event": "recording_started", "data": {"capture_id": "abc123"}}

See Appendix B of DESIGN.md for the full protocol specification.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from loguru import logger

from engine.config import EngineConfig


class IPCHandler:
    """Handles JSON line protocol communication with the Tauri shell.

    Reads commands from stdin, dispatches to the appropriate handler,
    and writes responses/events to stdout.

    Args:
        config: Engine configuration.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._handlers: dict[str, Any] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register command handlers for all supported IPC commands."""
        # TODO: Register handlers for each command:
        # - start_recording, stop_recording, pause_recording, resume_recording
        # - get_status, get_captures, get_storage_usage
        # - set_config
        # - scrub_capture, get_scrub_manifest
        # - approve_review, dismiss_review, get_review_status, get_pending_reviews
        # - upload_capture, delete_capture
        # - get_active_backends, get_egress_destinations
        pass

    def run(self) -> None:
        """Start the IPC message loop. Blocks until stdin is closed."""
        logger.info("IPC handler ready, listening on stdin")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                self._dispatch(message)
            except json.JSONDecodeError:
                logger.error("Invalid JSON received: {line}", line=line)
            except Exception:
                logger.exception("Error processing message: {line}", line=line)

    def _dispatch(self, message: dict) -> None:
        """Dispatch a command message to the appropriate handler.

        Args:
            message: Parsed JSON command object with 'id', 'cmd', and 'params' keys.
        """
        msg_id = message.get("id")
        cmd = message.get("cmd")
        params = message.get("params", {})

        handler = self._handlers.get(cmd)
        if handler is None:
            self.send_error(msg_id, f"Unknown command: {cmd}")
            return

        try:
            result = handler(**params)
            self.send_response(msg_id, result)
        except Exception as exc:
            logger.exception("Handler error for {cmd}", cmd=cmd)
            self.send_error(msg_id, str(exc))

    def send_response(self, msg_id: str | None, data: Any) -> None:
        """Send a success response to the Tauri shell.

        Args:
            msg_id: The request ID to correlate with the original command.
            data: The response payload.
        """
        response = {"id": msg_id, "status": "ok", "data": data}
        self._write(response)

    def send_error(self, msg_id: str | None, error: str) -> None:
        """Send an error response to the Tauri shell.

        Args:
            msg_id: The request ID to correlate with the original command.
            error: Human-readable error message.
        """
        response = {"id": msg_id, "status": "error", "error": error}
        self._write(response)

    def send_event(self, event: str, data: Any) -> None:
        """Send an unsolicited event to the Tauri shell.

        Events do not have an ID and are not correlated with any command.

        Args:
            event: Event type (e.g., "recording_started", "upload_progress").
            data: Event payload.
        """
        message = {"event": event, "data": data}
        self._write(message)

    def _write(self, obj: dict) -> None:
        """Write a JSON object as a single line to stdout.

        Args:
            obj: The object to serialize and write.
        """
        line = json.dumps(obj)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
