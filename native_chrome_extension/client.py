import os
import subprocess as sp

from namedpipe import NPopen

from openadapt import config


def client():
    pipe_name = os.path.join(r'\\.\pipe', config.PIPE_NAME)
    with open(pipe_name, 'rt') as f:
        while not f.closed:
            line = f.readline()
            if len(line) > 0:
                print(line)

if __name__ == '__main__':
    client()
