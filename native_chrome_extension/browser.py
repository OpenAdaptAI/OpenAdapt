#!/usr/bin/env -S python3 -u

# Note that running python with the `-u` flag is required on Windows,
# in order to ensure that stdin and stdout are opened in binary, rather
# than text, mode.

from loguru import logger
from multiprocessing.connection import Listener
import sys
import json
import struct

import os
import subprocess as sp

from namedpipe import NPopen

# Read a message from stdin and decode it.
def getMessage():
    rawLength = sys.stdin.buffer.read(4)
    if len(rawLength) == 0:
        sys.exit(0)
    messageLength = struct.unpack('@I', rawLength)[0]
    message = sys.stdin.buffer.read(messageLength).decode('utf-8')
    return json.loads(message)


# Encode a message for transmission,
# given its content.
def encodeMessage(messageContent):
    # https://docs.python.org/3/library/json.html#basic-usage
    # To get the most compact JSON representation, you should specify 
    # (',', ':') to eliminate whitespace.
    # We want the most compact representation because the browser rejects # messages that exceed 1 MB.
    encodedContent = json.dumps(messageContent, separators=(',', ':')).encode('utf-8')
    encodedLength = struct.pack('@I', len(encodedContent))
    return {'length': encodedLength, 'content': encodedContent}


# Send an encoded message to stdout
def sendMessage(encodedMessage):
    sys.stdout.buffer.write(encodedMessage['length'])
    sys.stdout.buffer.write(encodedMessage['content'])
    sys.stdout.buffer.flush()


# TODO: send the Javascript memory state to the server


SERVER_SENDS = True
PORT = 6001

if __name__ == '__main__':
    address = ('localhost', PORT)     # family is deduced to be 'AF_INET'
    listener = Listener(address, authkey=b'secret password')
    conn = listener.accept()
    logger.info(f'connection accepted from {listener.last_accepted=}')

    reply_num = 0
    while True:
        receivedMessage = getMessage()
        reply_num += 1
        conn.send(receivedMessage)
        sendMessage(encodeMessage(str(reply_num)))
        sendMessage(encodeMessage(receivedMessage))
