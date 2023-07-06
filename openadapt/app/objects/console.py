import sys

from nicegui import ui


class Console(object):
    def __init__(self):
        self.log = ui.log().classes("w-full h-20")
        self.old_stderr = sys.stderr
        self.buffer = ""

    def write(self, data):
        self.buffer += data
        self.log.push(data[:-1])
        self.log.update()
        self.old_stderr.write(data)

    def flush(self):
        self.log.update()
        self.old_stderr.flush()

    def reset(self):
        self.log.clear()
        sys.stderr = self.old_stderr

    def copy_output(self):
        return self.buffer
