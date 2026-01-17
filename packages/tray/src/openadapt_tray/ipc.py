"""Inter-process communication for OpenAdapt Tray."""

import json
import socket
import threading
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass
from enum import Enum


class IPCMessageType(Enum):
    """IPC message types."""

    # Commands (from tray to capture process)
    START_RECORDING = "start_recording"
    STOP_RECORDING = "stop_recording"
    GET_STATUS = "get_status"

    # Events (from capture process to tray)
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    RECORDING_ERROR = "recording_error"
    STATUS_UPDATE = "status_update"
    TRAINING_PROGRESS = "training_progress"


@dataclass
class IPCMessage:
    """IPC message structure."""

    type: IPCMessageType
    data: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(
            {
                "type": self.type.value,
                "data": self.data,
            }
        )

    @classmethod
    def from_json(cls, json_str: str) -> "IPCMessage":
        """Deserialize from JSON string."""
        obj = json.loads(json_str)
        return cls(
            type=IPCMessageType(obj["type"]),
            data=obj.get("data"),
        )


class IPCClient:
    """IPC client for communicating with OpenAdapt processes."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 9876

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ):
        """Initialize IPC client.

        Args:
            host: Host address for IPC server.
            port: Port number for IPC server.
        """
        self.host = host
        self.port = port
        self._socket: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._handlers: Dict[IPCMessageType, Callable[[IPCMessage], None]] = {}

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
        """Send a message to the IPC server.

        Args:
            message: Message to send.

        Returns:
            True if sent successfully.
        """
        if not self._socket:
            return False

        try:
            data = message.to_json() + "\n"
            self._socket.sendall(data.encode("utf-8"))
            return True
        except (socket.error, OSError) as e:
            print(f"IPC send error: {e}")
            return False

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
        """Send stop recording command.

        Returns:
            True if sent successfully.
        """
        return self.send(IPCMessage(type=IPCMessageType.STOP_RECORDING))

    def send_get_status(self) -> bool:
        """Send status request.

        Returns:
            True if sent successfully.
        """
        return self.send(IPCMessage(type=IPCMessageType.GET_STATUS))

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
