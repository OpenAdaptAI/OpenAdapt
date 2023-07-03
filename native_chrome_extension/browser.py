#!/usr/bin/env -S python3 -u

# Note that running python with the `-u` flag is required on Windows,
# in order to ensure that stdin and stdout are opened in binary, rather
# than text, mode.

""" Module for the browser-side native messaging host/server. """


import json
import struct
import subprocess as sp
import sys
import time

from namedpipe import NPopen


# Credit and Reference:
# https://github.com/Rayquaza01/nativemessaging/blob/master/nativemessaging/


def getMessage() -> Any:
    """
    Read a message from stdin and decode it.

    Args:
        None

    Returns:
        A Python object, corresponding to the decoded message.
    """

    rawLength = sys.stdin.buffer.read(4)
    if len(rawLength) == 0:
        sys.exit(0)
    messageLength = struct.unpack("@I", rawLength)[0]
    message = sys.stdin.buffer.read(messageLength).decode("utf-8")
    return json.loads(message)


def encodeMessage(messageContent: Any) -> Dict[str, bytes]:
    """
    Encode a message for transmission,
    given its content.

    Args:
        messageContent: The content of the message to encode.

    Returns:
        A dictionary containing the encoded message.
    """

    # https://docs.python.org/3/library/json.html#basic-usage
    # To get the most compact JSON representation, you should specify
    # (',', ':') to eliminate whitespace.
    # We want the most compact representation because the browser rejects
    # # messages that exceed 1 MB.
    encodedContent = json.dumps(messageContent, separators=(",", ":")).encode(
        "utf-8"
    )
    encodedLength = struct.pack("@I", len(encodedContent))
    return {"length": encodedLength, "content": encodedContent}


def sendMessage(encodedMessage: Dict[str, bytes]) -> None:
    """
    Send an encoded message to stdout

    Args:
        encodedMessage: The encoded message to send.

    Returns:
        None
    """

    sys.stdout.buffer.write(encodedMessage["length"])
    sys.stdout.buffer.write(encodedMessage["content"])
    sys.stdout.buffer.flush()


def server():
    """
    The main function of the browser-side native messaging host/server

    Args:
        None

    Returns:
        None
    """

    reply_num = 0
    while True:
        receivedMessage = getMessage()
        # pipe.send(receivedMessage)
        reply_num += 1
        sendMessage(encodeMessage(str(reply_num)))
        sendMessage(encodeMessage(receivedMessage))


if __name__ == "__main__":
    server()
