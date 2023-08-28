#!/usr/bin/env python3

"""Script for communicating with the browser extension.
Usage:
    See `native_chrome_extension/browser.bat`.
"""

# Note that running python with the `-u` flag is required on Windows,
# in order to ensure that stdin and stdout are opened in binary, rather
# than text, mode.

from multiprocessing.connection import Listener
import json
import sqlite3
import struct
import sys

from openadapt import config, sockets

STORE_DATA = False


def get_message() -> dict:
    """Read a message from stdin and decode it.

    Returns:
        A dictionary representing the decoded message.
    """
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        sys.exit(0)
    message_length = struct.unpack("@I", raw_length)[0]
    message = sys.stdin.buffer.read(message_length).decode("utf-8")
    return json.loads(message)


def encode_message(message_content: any) -> dict:
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
    encoded_content = json.dumps(message_content, separators=(",", ":")).encode("utf-8")
    encoded_length = struct.pack("@I", len(encoded_content))
    return {"length": encoded_length, "content": encoded_content}


def send_message(encoded_message: dict) -> None:
    """Send an encoded message to stdout

    Args:
        encoded_message: The encoded message to be sent.
    """
    sys.stdout.buffer.write(encoded_message["length"])
    sys.stdout.buffer.write(encoded_message["content"])
    sys.stdout.buffer.flush()


def send_message_to_client(message):
    # check if client connection exists
    # if not, create one
    try:
        conn = sockets.create_client_connection(config.SOCKET_PORT)
        conn.send(message)
        conn.close()
    except Exception as exc:
        print(f"Error sending message to client: {exc}")


def main() -> None:
    # TODO: use sockets to communicate with openadapt client

    # Connect to the database
    conn = sqlite3.connect("messages.db")
    c = conn.cursor()
    # Create the messages table if it doesn't exist
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL
        )
        """)

    while True:
        conn = sockets.create_server_connection(config.SOCKET_PORT)
        message = get_message()
        if STORE_DATA:
            # Log the message to the database
            c.execute(
                "INSERT INTO messages (message) VALUES (?)", (json.dumps(message),)
            )
            conn.commit()
            response = {"message": "Data received and logged successfully!"}
            encoded_response = encode_message(response)
            send_message(encoded_response)
            send_message_to_client(message)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
