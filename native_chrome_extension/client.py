import subprocess as sp
from namedpipe import NPopen


def client():
    with NPopen('rt', name='test') as pipe:
        stream = pipe.wait()
        while True:
            message = pipe.stream.read()
            if not message:
                print("Pipe is empty")
                break
            print(f'Received: {message}')

if __name__ == '__main__':
    client()
