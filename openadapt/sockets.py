"""Module for managing socket connections and communication."""

from multiprocessing import Queue
from multiprocessing.connection import Client, Connection, Listener
from typing import Any, Optional
import time

from loguru import logger

from openadapt import config

client_by_port = {}
server_by_port = {}
queue_by_port = {}


def client_send_message(port: int, msg: Any) -> None:
    """Send a message to the client connection associated with the given port.

    Args:
        port: The port number associated with the client connection.
        msg: The message to be sent.

    Returns:
        None
    """
    client_conn = client_by_port.get(port)
    if client_conn:
        client_conn.send(msg)


def server_send_message(port: int, msg: Any) -> None:
    """Send a message to the server connection associated with the given port.

    Args:
        port: The port number associated with the server connection.
        msg: The message to be sent.

    Returns:
        None
    """
    server_conn = server_by_port.get(port)
    if server_conn:
        server_conn.send(msg)


def client_receive_message(port: int) -> Optional[str]:
    """Receive a message from the client connection associated with the given port.

    Args:
        port: The port number associated with the client connection.

    Returns:
        The received message as a string, or None if no message is available.
    """
    client_conn = client_by_port.get(port)
    if client_conn:
        try:
            if message := client_conn.recv():
                return message
        except Exception as exc:
            logger.error("Connection was closed.")
            logger.error(exc)
            del client_by_port[port]


def server_receive_message(port: int) -> Optional[str]:
    """Receive a message from the server connection associated with the given port.

    Args:
        port: The port number associated with the server connection.

    Returns:
        The received message as a string, or None if no message is available.
    """
    server_conn = server_by_port.get(port)
    while True:
        if server_conn:
            try:
                message = server_conn.recv()
                return message
            except EOFError:
                logger.warning("Connection closed. Reconnecting...")
                while True:
                    try:
                        server_conn = create_server_connection(port)
                        break
                    except Exception as exc:
                        logger.warning(f"Failed to reconnect: {exc}")
                        time.sleep(config.SOCKET_RETRY_INTERVAL)
    return None


def client_add_sink(port: int, queue: Queue) -> None:
    """Add a sink queue to the specified client port.

    Args:
        port: The port number to associate with the sink queue.
        queue: The queue to be added as a sink.

    Raises:
        ValueError: If the specified port already has a sink assigned.

    Returns:
        None
    """
    if port in queue_by_port:
        raise ValueError(f"Port {port} already has a sink assigned.")
    queue_by_port[port] = queue


def server_add_sink(port: int, queue: Queue) -> None:
    """Add a sink queue to the specified server port.

    Args:
        port: The port number to associate with the sink queue.
        queue: The queue to be added as a sink.

    Raises:
        ValueError: If the specified port already has a sink assigned.

    Returns:
        None
    """
    if port in queue_by_port:
        raise ValueError(f"Port {port} already has a sink assigned.")
    queue_by_port[port] = queue


_terminate_event: Optional[bool] = None


def set_terminate_event(terminate_event: bool) -> None:
    """Set the termination event to control the event loop.

    Args:
        terminate_event: The termination event object.

    Returns:
        None
    """
    global _terminate_event
    _terminate_event = terminate_event


def create_client_connection(port: int) -> Connection:
    """Create a client connection and establish a connection to the specified port.

    Args:
        port: The port number to connect to.

    Returns:
        The created client connection object.
    """
    address = (config.SOCKET_ADDRESS, port)
    conn = Client(address, authkey=config.SOCKET_AUTHKEY)
    client_by_port[port] = conn
    logger.info("Connected to the Client.")
    return conn


def create_server_connection(port: int) -> Connection:
    """Create and listen for connections on the specified port.

    Args:
        port: The port number to bind the server connection to.

    Returns:
        The created server connection object.
    """
    address = (config.SOCKET_ADDRESS, port)
    conn = Listener(address, authkey=config.SOCKET_AUTHKEY)
    conn = conn.accept()
    server_by_port[port] = conn
    logger.info("Connected to the Server.")
    return conn


def event_loop() -> None:
    """The event loop for receiving and handling messages.

    Raises:
        AssertionError: If `_terminate_event` is not set.

    Returns:
        None
    """
    assert _terminate_event, "You must call set_terminate_event"
    while not _terminate_event.is_set():
        for port, client_conn in client_by_port.items():
            try:
                message = client_conn.recv()  # noqa: F841
                # if message:
                # TODO: Handle message

            except EOFError:
                # Handle connection closed or error
                del client_by_port[port]
                del queue_by_port[port]

        # for port, server_conn in server_by_port.items():
        #     try:
        #         message = server_conn.recv()
        #         if message:
        #             queue = queue_by_port.get(port)
        #             if queue:
        #                 queue.put(message)
        #     except EOFError:
        #         # Handle connection closed or error
        #         del server_by_port[port]
        #         del queue_by_port[port]


def server_sends(conn: Connection, message: Any) -> None:
    """Send a message to the server connection associated with the given port."""
    if conn:
        conn.send(message)


def client_receive(conn: Connection) -> Any:
    """Receive a message from the client connection associated with the given port."""
    if conn:
        try:
            message = conn.recv()
            return message
        except EOFError:
            logger.warning("Connection was closed.")
            return None
