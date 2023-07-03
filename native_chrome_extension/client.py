import subprocess as sp

from namedpipe import NPopen

from openadapt import config


def client():
    with open(os.path.join(r'\\.\pipe', config.PIPE_NAME), 'rt') as f:
        while not f.closed:
            line = f.readline()
            if len(line) > 0:
                print(line)

if __name__ == '__main__':
    client()
