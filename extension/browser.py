#!/usr/bin/env -S python3 -u

""" Module for communicating with the browser extension. """

# Note that running python with the `-u` flag is required on Windows,
# in order to ensure that stdin and stdout are opened in binary, rather
# than text mode.


import sys
import json
import struct


def get_message():
    """
    Read a message from stdin and decode it.

    Args:
        None

    Returns:
        A Python dictionary representing the message.
    """

    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        sys.exit(0)
    message_length = struct.unpack("@I", raw_length)[0]
    message = sys.stdin.buffer.read(message_length).decode("utf-8")
    return json.loads(message)


def encode_message(message_content):
    """
    Encode a message for transmission,

    Args:
        message_content: The content of the message to be encoded.

    Returns:
        A Python dictionary representing the encoded message.
    """

    # https://docs.python.org/3/library/json.html#basic-usage
    # To get the most compact JSON representation, you should specify
    # (',', ':') to eliminate whitespace.
    # We want the most compact representation because
    # the browser rejects # messages that exceed 1 MB.
    encoded_content = json.dumps(
        message_content, separators=(",", ":")
    ).encode("utf-8")
    encoded_content = struct.pack("@I", len(encoded_content))
    return {"length": encoded_content, "content": encoded_content}


def send_message(encoded_message):
    """
    Send an encoded message to stdout

    Args:
        encoded_message: The encoded message to be sent.

    Returns:
        None
    """

    sys.stdout.buffer.write(encoded_message["length"])
    sys.stdout.buffer.write(encoded_message["content"])
    sys.stdout.buffer.flush()
    # TODO: Named Pipe


send_message(encode_message("Hello from Python!"))
DOM_UPDATE_COUNTER = 0
while True:
    receivedMessage = get_message()
    DOM_UPDATE_COUNTER += 1
    send_message(encode_message(str(DOM_UPDATE_COUNTER)))
    send_message(encode_message(receivedMessage))
