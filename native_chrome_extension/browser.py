#!/usr/bin/env -S python3 -u

"""Script for communicating with the browser extension.

Usage:

    See `native_chrome_extension/browser.bat`.

"""

# Note that running python with the `-u` flag is required on Windows,
# in order to ensure that stdin and stdout are opened in binary, rather
# than text, mode.

from multiprocessing.connection import Listener
from typing import Any
import json
import struct
import sys

from loguru import logger

from openadapt import config, sockets


def get_message() -> Any:
    """Read a message from stdin and decode it.

    Args:
        None

    Returns:
        A Python object representing the message.
    """
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        sys.exit(0)
    message_length = struct.unpack("@I", raw_length)[0]
    message = sys.stdin.buffer.read(message_length).decode("utf-8")
    return json.loads(message)


def encode_message(message_content: Any) -> dict[bytes, str]:
    """Encode a message for transmission, given its content.

    Args:
        message_content: The content of the message to be encoded.

    Returns:
        A dictionary containing the encoded message.
    """
    # https://docs.python.org/3/library/json.html#basic-usage
    # To get the most compact JSON representation, you should specify
    # (',', ':') to eliminate whitespace.
    # We want the most compact representation because the browser rejects
    # messages that exceed 1 MB.
    encoded_content = json.dumps(message_content, separators=(",", ":")).encode(
        "utf-8"
    )
    encoded_length = struct.pack("@I", len(encoded_content))
    return {"length": encoded_length, "content": encoded_content}


def send_message(encoded_message: dict[bytes, str]) -> None:
    """Send an encoded message to stdout

    Args:
        encoded_message: The encoded message to be sent.

    Returns:
        None
    """
    sys.stdout.buffer.write(encoded_message["length"])
    sys.stdout.buffer.write(encoded_message["content"])
    sys.stdout.buffer.flush()


# TODO: send the Javascript memory state to the server


if __name__ == "__main__":

    # Establish a server connection
    listener = sockets.create_server_connection(config.SOCKET_PORT)

    # logger.info(f"connection accepted from {listener.last_accepted=}")

    # Start the event loop
    while True:
        received_message = get_message()

        # Sending message to Client
        sockets.server_send_message(config.SOCKET_PORT, received_message)

        # # Receiving message from Server
        # response = sockets.server_receive_message(config.SOCKET_PORT)

        # Sending the received message back to background.js
        send_message(encode_message(received_message))
