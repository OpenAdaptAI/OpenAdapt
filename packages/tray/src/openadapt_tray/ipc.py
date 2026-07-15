"""Inter-process communication for OpenAdapt Tray.

The tray is an IPC *client* that connects to a loopback TCP socket server
exposed by the desktop app. The desktop writes its bound host/port and a
per-session shared token to a discovery file at ``~/.openadapt/desktop_ipc.json``;
the tray reads it, connects, and includes the token on every command so the
desktop can reject other local processes.

Transport is newline-delimited JSON. Commands flow tray→desktop; events flow
desktop→tray. The desktop app is the source of truth for all state — the tray
only renders it.
"""

import json
import socket
import threading
from pathlib import Path
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass
from enum import Enum


# Discovery file the desktop app writes on startup (see §3d of the spec).
DEFAULT_DISCOVERY_PATH = Path.home() / ".openadapt" / "desktop_ipc.json"


class IPCMessageType(Enum):
    """IPC message types exchanged with the desktop app."""

    # Commands (tray → desktop)
    START_RECORDING = "start_recording"
    STOP_RECORDING = "stop_recording"
    GET_STATUS = "get_status"
    OPEN_WORKFLOW_LIBRARY = "open_workflow_library"
    OPEN_TEACH = "open_teach"
    PAUSE_SYNC = "pause_sync"
    RESUME_SYNC = "resume_sync"

    # Events (desktop → tray)
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    RECORDING_ERROR = "recording_error"
    STATUS_UPDATE = "status_update"
    COMPILE_PROGRESS = "compile_progress"
    SYNC_STATE = "sync_state"
    BREAK_COUNT = "break_count"


@dataclass
class IPCMessage:
    """IPC message structure (newline-delimited JSON on the wire).

    Command messages (tray → desktop) carry the per-session ``token`` from the
    discovery file so the desktop can authenticate the sender. Event messages
    (desktop → tray) carry a ``data`` payload.
    """

    type: IPCMessageType
    data: Optional[Dict[str, Any]] = None
    token: Optional[str] = None

    def to_json(self) -> str:
        """Serialize to JSON string."""
        obj: Dict[str, Any] = {
            "type": self.type.value,
            "data": self.data,
        }
        if self.token is not None:
            obj["token"] = self.token
        return json.dumps(obj)

    @classmethod
    def from_json(cls, json_str: str) -> "IPCMessage":
        """Deserialize from JSON string."""
        obj = json.loads(json_str)
        return cls(
            type=IPCMessageType(obj["type"]),
            data=obj.get("data"),
            token=obj.get("token"),
        )


@dataclass
class DesktopEndpoint:
    """Discovered desktop-app IPC endpoint."""

    host: str
    port: int
    token: Optional[str] = None

    @classmethod
    def load(
        cls, path: Optional[Path] = None
    ) -> Optional["DesktopEndpoint"]:
        """Load the desktop IPC endpoint from the discovery file.

        Args:
            path: Discovery file path. Defaults to
                ``~/.openadapt/desktop_ipc.json``.

        Returns:
            A :class:`DesktopEndpoint` if the file exists and is valid,
            otherwise ``None`` (desktop app not running).
        """
        path = path or DEFAULT_DISCOVERY_PATH
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text())
            port = data.get("port")
            if port is None:
                return None
            return cls(
                host=data.get("host", "127.0.0.1"),
                port=int(port),
                token=data.get("token"),
            )
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"Could not read desktop IPC discovery file: {e}")
            return None


class IPCClient:
    """IPC client for communicating with OpenAdapt processes."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 9876

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: Optional[str] = None,
    ):
        """Initialize IPC client.

        Args:
            host: Host address for the desktop IPC server.
            port: Port number for the desktop IPC server.
            token: Per-session shared token (from the discovery file) attached
                to every command so the desktop can authenticate the sender.
        """
        self.host = host
        self.port = port
        self.token = token
        self._socket: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._handlers: Dict[IPCMessageType, Callable[[IPCMessage], None]] = {}

    @classmethod
    def from_discovery(
        cls, path: Optional[Path] = None
    ) -> Optional["IPCClient"]:
        """Build a client from the desktop discovery file, if present.

        Args:
            path: Discovery file path. Defaults to
                ``~/.openadapt/desktop_ipc.json``.

        Returns:
            A configured :class:`IPCClient`, or ``None`` if the desktop app is
            not running (no discovery file).
        """
        endpoint = DesktopEndpoint.load(path)
        if endpoint is None:
            return None
        return cls(host=endpoint.host, port=endpoint.port, token=endpoint.token)

    def refresh_from_discovery(self, path: Optional[Path] = None) -> bool:
        """Re-read the discovery file and update host/port/token in place.

        Useful after launching the desktop app: the discovery file appears once
        the desktop's socket server is bound.

        Args:
            path: Discovery file path. Defaults to the standard location.

        Returns:
            True if a valid endpoint was found and applied.
        """
        endpoint = DesktopEndpoint.load(path)
        if endpoint is None:
            return False
        self.host = endpoint.host
        self.port = endpoint.port
        self.token = endpoint.token
        return True

    def register_handler(
        self,
        message_type: IPCMessageType,
        handler: Callable[[IPCMessage], None],
    ) -> None:
        """Register a message handler.

        Args:
            message_type: Type of message to handle.
            handler: Callback function for this message type.
        """
        self._handlers[message_type] = handler

    def connect(self) -> bool:
        """Connect to the IPC server.

        Returns:
            True if connected successfully.
        """
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(5.0)
            self._socket.connect((self.host, self.port))
            self._socket.settimeout(None)

            # Start listener thread
            self._running = True
            self._listener_thread = threading.Thread(
                target=self._listen_loop,
                daemon=True,
            )
            self._listener_thread.start()

            return True
        except (socket.error, OSError) as e:
            print(f"IPC connection failed: {e}")
            self._socket = None
            return False

    def _listen_loop(self) -> None:
        """Listen for incoming messages."""
        buffer = ""
        while self._running and self._socket:
            try:
                data = self._socket.recv(4096)
                if not data:
                    break

                buffer += data.decode("utf-8")

                # Process complete messages (newline-delimited)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line:
                        self._handle_message(line)

            except socket.timeout:
                continue
            except (socket.error, OSError):
                break
            except Exception as e:
                print(f"IPC receive error: {e}")
                break

        self._running = False

    def _handle_message(self, json_str: str) -> None:
        """Handle an incoming message.

        Args:
            json_str: JSON-encoded message string.
        """
        try:
            message = IPCMessage.from_json(json_str)
            handler = self._handlers.get(message.type)
            if handler:
                handler(message)
        except Exception as e:
            print(f"Error handling IPC message: {e}")

    def send(self, message: IPCMessage) -> bool:
        """Send a message to the desktop IPC server.

        The per-session token is attached automatically if not already set on
        the message.

        Args:
            message: Message to send.

        Returns:
            True if sent successfully.
        """
        if not self._socket:
            return False

        if message.token is None and self.token is not None:
            message.token = self.token

        try:
            data = message.to_json() + "\n"
            self._socket.sendall(data.encode("utf-8"))
            return True
        except (socket.error, OSError) as e:
            print(f"IPC send error: {e}")
            return False

    def send_command(
        self,
        message_type: IPCMessageType,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Send a parameterless-or-simple command to the desktop.

        Args:
            message_type: The command type.
            data: Optional payload.

        Returns:
            True if sent successfully.
        """
        return self.send(IPCMessage(type=message_type, data=data))

    def send_start_recording(self, name: str) -> bool:
        """Send start recording command.

        Args:
            name: Recording name.

        Returns:
            True if sent successfully.
        """
        return self.send(
            IPCMessage(
                type=IPCMessageType.START_RECORDING,
                data={"name": name},
            )
        )

    def send_stop_recording(self) -> bool:
        """Send stop recording command."""
        return self.send(IPCMessage(type=IPCMessageType.STOP_RECORDING))

    def send_get_status(self) -> bool:
        """Send status request."""
        return self.send(IPCMessage(type=IPCMessageType.GET_STATUS))

    def send_open_workflow_library(self) -> bool:
        """Ask the desktop to open its local workflow library window."""
        return self.send(IPCMessage(type=IPCMessageType.OPEN_WORKFLOW_LIBRARY))

    def send_open_teach(self, workflow_id: Optional[str] = None) -> bool:
        """Ask the desktop to open the local teach-the-fix view.

        Args:
            workflow_id: Optional workflow to open teach for.
        """
        data = {"workflow_id": workflow_id} if workflow_id else None
        return self.send(IPCMessage(type=IPCMessageType.OPEN_TEACH, data=data))

    def send_pause_sync(self) -> bool:
        """Ask the desktop to pause the upload/sync queue."""
        return self.send(IPCMessage(type=IPCMessageType.PAUSE_SYNC))

    def send_resume_sync(self) -> bool:
        """Ask the desktop to resume the upload/sync queue."""
        return self.send(IPCMessage(type=IPCMessageType.RESUME_SYNC))

    def is_connected(self) -> bool:
        """Check if connected to IPC server."""
        return self._socket is not None and self._running

    def close(self) -> None:
        """Close the IPC connection."""
        self._running = False

        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        if self._listener_thread:
            self._listener_thread.join(timeout=1.0)
            self._listener_thread = None


class IPCServer:
    """Simple IPC server for testing or standalone mode."""

    def __init__(
        self,
        host: str = IPCClient.DEFAULT_HOST,
        port: int = IPCClient.DEFAULT_PORT,
    ):
        """Initialize IPC server.

        Args:
            host: Host address to bind to.
            port: Port number to listen on.
        """
        self.host = host
        self.port = port
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._handlers: Dict[IPCMessageType, Callable[[IPCMessage], IPCMessage]] = {}

    def register_handler(
        self,
        message_type: IPCMessageType,
        handler: Callable[[IPCMessage], Optional[IPCMessage]],
    ) -> None:
        """Register a message handler.

        Args:
            message_type: Type of message to handle.
            handler: Callback function that may return a response.
        """
        self._handlers[message_type] = handler

    def start(self) -> bool:
        """Start the IPC server.

        Returns:
            True if started successfully.
        """
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((self.host, self.port))
            self._socket.listen(5)

            self._running = True
            threading.Thread(target=self._accept_loop, daemon=True).start()

            return True
        except (socket.error, OSError) as e:
            print(f"IPC server start failed: {e}")
            return False

    def _accept_loop(self) -> None:
        """Accept incoming connections."""
        while self._running and self._socket:
            try:
                self._socket.settimeout(1.0)
                client, addr = self._socket.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle_client(self, client: socket.socket) -> None:
        """Handle a client connection.

        Args:
            client: Client socket.
        """
        buffer = ""
        try:
            while self._running:
                data = client.recv(4096)
                if not data:
                    break

                buffer += data.decode("utf-8")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line:
                        response = self._process_message(line)
                        if response:
                            client.sendall((response.to_json() + "\n").encode("utf-8"))
        except Exception as e:
            print(f"Error handling IPC client: {e}")
        finally:
            client.close()

    def _process_message(self, json_str: str) -> Optional[IPCMessage]:
        """Process an incoming message.

        Args:
            json_str: JSON-encoded message string.

        Returns:
            Optional response message.
        """
        try:
            message = IPCMessage.from_json(json_str)
            handler = self._handlers.get(message.type)
            if handler:
                return handler(message)
        except Exception as e:
            print(f"Error processing IPC message: {e}")
        return None

    def stop(self) -> None:
        """Stop the IPC server."""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
