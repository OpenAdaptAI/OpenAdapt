import os
import subprocess as sp

from namedpipe import NPopen


with open(r'\\.\pipe\test', 'rt') as f:
    while not f.closed:
        line = f.readline()
        if len(line) > 0:
            print(line)

