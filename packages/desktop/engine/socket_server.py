"""socket_server -- the loopback TCP wire the tray connects to.

The tray (:mod:`openadapt_tray.ipc`) is a socket CLIENT. It reads
``~/.openadapt/desktop_ipc.json`` = ``{host, port, token}`` and dials a loopback
TCP server, sending newline-delimited JSON ``IPCMessage`` frames
``{type, data, token}`` (tray→desktop commands) and receiving the same shape
(desktop→tray events). This module is the missing SERVER (review 2.1 P0-1).

Contract (spec 3d, matched to the tray's ``ipc.py`` EXACTLY):
    * bind ``127.0.0.1:<ephemeral>``; write the discovery file on startup with a
      fresh per-session token;
    * accept frames ``{"type": <str>, "data": {...}, "token": <str>}``;
    * reject any frame whose ``token`` != the session token (loopback-only is not
      enough -- any local process could connect);
    * dispatch command ``type``\\ s to engine actions via the shared
      :class:`~engine.dispatch.EngineDispatcher` (same dispatcher the Tauri
      stdin/stdout wire uses, so the two never drift);
    * stream engine events back to every connected client as ``{"type", "data"}``.

Only the events the tray's ``IPCMessageType`` enum knows are forwarded; other
dispatcher events (``replay_progress``/``log_line``/``open_window`` ...) are
dropped so the tray's ``IPCMessage.from_json`` never rejects an unknown type.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from pathlib import Path

from loguru import logger

from engine.config import EngineConfig
from engine.dispatch import EngineDispatcher, EngineServices

# Discovery file the tray reads (tray ipc.py: DEFAULT_DISCOVERY_PATH).
DEFAULT_DISCOVERY_PATH = Path.home() / ".openadapt" / "desktop_ipc.json"

# Commands the tray may send (its IPCMessageType command members). A strict
# subset of the frontend CMD catalog; all resolve to dispatcher commands.
_TRAY_COMMANDS = frozenset({
    "start_recording",
    "stop_recording",
    "get_status",
    "open_workflow_library",
    "open_teach",
    "pause_sync",
    "resume_sync",
})

# Events the tray's IPCMessageType enum can decode. Anything else is dropped
# before forwarding so the tray never hits a from_json ValueError.
_TRAY_EVENTS = frozenset({
    "recording_started",
    "recording_stopped",
    "recording_error",
    "status_update",
    "compile_progress",
    "sync_state",
    "break_count",
})


class DesktopSocketServer:
    """A loopback JSON-lines TCP server bridging the tray to the engine.

    Args:
        config: Engine configuration.
        host: Loopback host to bind (always ``127.0.0.1``).
        discovery_path: Where to write ``{host, port, token}``.
        token: Per-session shared token (generated when omitted).
        dispatcher: Injected dispatcher (built from ``config`` otherwise).
        services: Injected engine services for the built dispatcher.
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        host: str = "127.0.0.1",
        discovery_path: Path | None = None,
        token: str | None = None,
        dispatcher: EngineDispatcher | None = None,
        services: EngineServices | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.discovery_path = discovery_path or DEFAULT_DISCOVERY_PATH
        self.token = token or secrets.token_urlsafe(24)
        self.dispatcher = dispatcher or EngineDispatcher(
            config, services=services, emit=self._broadcast
        )
        # Ensure our broadcast is wired even when a dispatcher was injected.
        self.dispatcher.emit = self._broadcast
        self._server: socket.socket | None = None
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self._accept_thread: threading.Thread | None = None
        self._running = False
        self.port: int | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> int:
        """Bind, write the discovery file, and start accepting connections.

        Returns:
            The bound ephemeral port.
        """
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, 0))  # ephemeral port
        self._server.listen(8)
        port = int(self._server.getsockname()[1])
        self.port = port
        self._running = True
        self._write_discovery()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        logger.info("Desktop IPC socket server on {h}:{p}", h=self.host, p=port)
        return port

    def stop(self) -> None:
        """Stop accepting, close clients, and remove the discovery file."""
        self._running = False
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        try:
            if self.discovery_path.exists():
                self.discovery_path.unlink()
        except OSError:
            pass

    def _write_discovery(self) -> None:
        self.discovery_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"host": self.host, "port": self.port, "token": self.token}
        tmp = self.discovery_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.discovery_path)
        try:  # tighten perms -- the token is a local secret
            os.chmod(self.discovery_path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------- accept/recv

    def _accept_loop(self) -> None:
        while self._running and self._server is not None:
            try:
                conn, _addr = self._server.accept()
            except OSError:
                break
            with self._clients_lock:
                self._clients.append(conn)
            threading.Thread(
                target=self._client_loop, args=(conn,), daemon=True
            ).start()

    def _client_loop(self, conn: socket.socket) -> None:
        buffer = ""
        try:
            while self._running:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        self._handle_frame(conn, line)
        except OSError:
            pass
        finally:
            with self._clients_lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _handle_frame(self, conn: socket.socket, line: str) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Tray sent invalid JSON; ignoring")
            return
        if not isinstance(frame, dict):
            return
        if frame.get("token") != self.token:
            logger.warning("Tray frame rejected: bad/missing session token")
            self._send(conn, "recording_error", {"error": "unauthorized"})
            return
        cmd = frame.get("type")
        if cmd not in _TRAY_COMMANDS:
            logger.warning("Tray sent unsupported command: {c}", c=cmd)
            return
        params = frame.get("data") or {}
        try:
            result = self.dispatcher.dispatch(cmd, params)
        except Exception as exc:
            logger.exception("Tray command {c} failed", c=cmd)
            self._send(conn, "recording_error", {"error": str(exc)})
            return
        # get_status has no natural event emission -- echo it as status_update
        # so the tray (which only renders events) updates immediately.
        if cmd == "get_status" and result is not None:
            self._send(conn, "status_update", result)

    # ------------------------------------------------------------- send/emit

    def _broadcast(self, event: str, data: dict) -> None:
        """Emit an engine event to every connected tray client (filtered)."""
        if event not in _TRAY_EVENTS:
            return
        with self._clients_lock:
            clients = list(self._clients)
        for conn in clients:
            self._send(conn, event, data)

    def _send(self, conn: socket.socket, event: str, data: dict) -> None:
        frame = json.dumps({"type": event, "data": data}) + "\n"
        try:
            conn.sendall(frame.encode("utf-8"))
        except OSError:
            with self._clients_lock:
                if conn in self._clients:
                    self._clients.remove(conn)
