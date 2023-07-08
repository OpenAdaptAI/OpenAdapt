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
import os
import struct
import sys

from loguru import logger
import subprocess as sp

from openadapt import config


SERVER_SENDS = True


def getMessage() -> Any:
    """Read a message from stdin and decode it.

    Args:
        None

    Returns:
        A Python object representing the message.
    """
    rawLength = sys.stdin.buffer.read(4)
    if len(rawLength) == 0:
        sys.exit(0)
    messageLength = struct.unpack("@I", rawLength)[0]
    message = sys.stdin.buffer.read(messageLength).decode("utf-8")
    return json.loads(message)


def encodeMessage(messageContent: Any) -> dict[bytes, str]:
    """Encode a message for transmission, given its content.

    Args:
        messageContent: The content of the message to be encoded.

    Returns:
        A dictionary containing the encoded message.
    """
    # https://docs.python.org/3/library/json.html#basic-usage
    # To get the most compact JSON representation, you should specify
    # (',', ':') to eliminate whitespace.
    # We want the most compact representation because the browser rejects # messages that exceed 1 MB.
    encodedContent = json.dumps(messageContent, separators=(",", ":")).encode(
        "utf-8"
    )
    encodedLength = struct.pack("@I", len(encodedContent))
    return {"length": encodedLength, "content": encodedContent}


def sendMessage(encodedMessage: dict[bytes, str]) -> None:
    """Send an encoded message to stdout

    Args:
        encodedMessage: The encoded message to be sent.

    Returns:
        None
    """
    sys.stdout.buffer.write(encodedMessage["length"])
    sys.stdout.buffer.write(encodedMessage["content"])
    sys.stdout.buffer.flush()


# TODO: send the Javascript memory state to the server
# TODO: also send the window.location.href to the server


if __name__ == "__main__":
    address = (
        "localhost",
        config.SERVER_PORT,
    )  # family is deduced to be 'AF_INET'
    listener = Listener(address, authkey=config.SERVER_AUTHKEY)
    conn = listener.accept()
    logger.info(f"connection accepted from {listener.last_accepted=}")

    reply_num = 0
    while True:
        receivedMessage = getMessage()
        reply_num += 1
        conn.send(receivedMessage)
        sendMessage(encodeMessage(str(reply_num)))
        sendMessage(encodeMessage(receivedMessage))
