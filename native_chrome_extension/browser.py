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

from openadapt import config


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

    # sendMessage(encodeMessage("Waiting for client to connect..."))
    
    # Create a new txt file to show the brower has connected
    with open('browser_connected.txt', 'w') as f:
        f.write('Browser connected')

    with NPopen('wt', name=config.PIPE_NAME) as pipe:  # writable text pipe
        stream = pipe.wait()
        sendMessage(encodeMessage("Client connected: " + str(stream)))
        dom_change_num = 0
        while True:
            receivedMessage = getMessage()
            dom_change_num += 1

            # Check if client (record.py) is still connected
            if pipe.stream:
                message = message + '\n'
                # Write message to the stream
                pipe.stream.write(receivedMessage)
                pipe.stream.flush()

            sendMessage(encodeMessage(str(dom_change_num)))
            sendMessage(encodeMessage(message))
                
                
def server2():
    sendMessage(encodeMessage("Waiting for client to connect..."))
    n = 0
    while True:
        n += 1
        message = get_message()
        sendMessage(encodeMessage(str(n)))
        sendMessage(encodeMessage(message))


if __name__ == "__main__":
    server()
