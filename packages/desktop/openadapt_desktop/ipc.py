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
import queue
import sys
import threading
from typing import Any

from loguru import logger

from openadapt_desktop.config import EngineConfig
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices

#: Commands served while another command runs. A run can stop before a save
#: and wait for a person; these read and answer that wait, and set up the
#: approver key the answer needs, so they never queue behind the run and never
#: take the dispatch lock. They touch only the dispatcher's approval watches,
#: Flow's approval files and the approver key (which has its own lock).
CONCURRENT_COMMANDS = frozenset(
    {
        "get_item_approval",
        "answer_item_approval",
        "get_approver_key",
        "set_up_approver_key",
        "remove_approver_key",
        "list_stop_cards",
        "answer_stop_approval",
    }
)

#: Sent by the Tauri shell when the app quits. Handled on the reader thread,
#: ahead of queued commands, because the shell kills the engine shortly after.
SHUTDOWN_COMMAND = "shutdown"


class IPCHandler:
    """Handles JSON line protocol communication with the Tauri shell.

    Reads commands from stdin, dispatches to the appropriate handler,
    and writes responses/events to stdout.

    The command names are the EXACT ``CMD`` values in the app's
    ``src/lib/engine.ts`` (``compile_recording`` etc.), routed through the
    shared :class:`~engine.dispatch.EngineDispatcher` so the stdin/stdout wire
    and the tray loopback socket can never drift. Events the dispatcher emits
    are written to stdout as ``{"event", "data"}`` lines and re-emitted by the
    Rust shell as ``engine://<event>``.

    Args:
        config: Engine configuration.
        services: Injected :class:`~engine.dispatch.EngineServices` (built from
            ``config`` otherwise). Injected in tests.
    """

    def __init__(self, config: EngineConfig, services: EngineServices | None = None) -> None:
        self.config = config
        self.dispatcher = EngineDispatcher(config, services=services, emit=self.send_event)
        self.dispatch_lock = threading.RLock()
        # The paired phone's answers to stops take the same lock, so an
        # answer that runs an item again waits its turn like a command.
        self.dispatcher.serial_lock = self.dispatch_lock
        self._write_lock = threading.Lock()
        self._handlers: dict[str, Any] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register a handler per command name from the shared dispatcher."""
        self._handlers = {
            cmd: (lambda _cmd=cmd, **params: self._dispatch_command(_cmd, params))
            for cmd in self.dispatcher.commands
        }

    def _dispatch_command(self, cmd: str, params: dict) -> dict | None:
        """Serialize commands from the two local IPC wires.

        The approval commands are the exception: they answer a run that holds
        the lock while it waits for a person.
        """
        if cmd in CONCURRENT_COMMANDS:
            return self.dispatcher.dispatch(cmd, params)
        with self.dispatch_lock:
            return self.dispatcher.dispatch(cmd, params)

    def run(self) -> None:
        """Start the IPC message loop. Blocks until stdin is closed.

        Commands run one at a time, in the order they arrive, on one worker
        thread. Reading stdin never waits for them, so an approval command
        (:data:`CONCURRENT_COMMANDS`) is answered while a run waits for it.
        Once stdin closes, the commands already received finish first.
        """
        logger.info("IPC handler ready, listening on stdin")
        pending: queue.Queue[dict | None] = queue.Queue()

        def work() -> None:
            while True:
                message = pending.get()
                if message is None:
                    return
                try:
                    self._dispatch(message)
                except Exception:
                    logger.exception("Error processing a command")

        worker = threading.Thread(target=work, name="engine-commands", daemon=True)
        worker.start()
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.error("Invalid JSON received: {line}", line=line)
                    continue
                if isinstance(message, dict) and message.get("cmd") == SHUTDOWN_COMMAND:
                    # The shell is quitting and kills the engine soon after.
                    # Erase the shadow buffer first, then answer and stop.
                    self._shutdown(message.get("id"))
                    break
                try:
                    if isinstance(message, dict) and message.get("cmd") in CONCURRENT_COMMANDS:
                        self._dispatch(message)
                    else:
                        pending.put(message)
                except Exception:
                    logger.exception("Error processing message: {line}", line=line)
        finally:
            pending.put(None)
            worker.join()

    def _shutdown(self, msg_id: str | None) -> None:
        try:
            self.dispatcher.close_shadow()
        except Exception:
            logger.exception("Shadow mode didn't close cleanly")
        self.send_response(msg_id, {"stopping": True})

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
        with self._write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
