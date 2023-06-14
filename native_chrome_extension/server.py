import subprocess as sp
from namedpipe import NPopen
import time


def get_dom_changes(n):
    if n < 20:
        return 'dom_change'
    else:
        return 'quit'


def server():
    with NPopen('wt', name='test') as pipe:  # writable text pipe
        print('Pipe Created = ', pipe.path)
        n = 0
        while True:
            n += 1
            time.sleep(1)
            message = get_dom_changes(n)
            if message == 'quit':
                break

            # Check if client (record.py) has connected
            if pipe.stream:
                message = message + '\n'
                # Write message to the stream
                pipe.stream.write(b'sent')
                pipe.stream.flush()

if __name__ == '__main__':
    server()
