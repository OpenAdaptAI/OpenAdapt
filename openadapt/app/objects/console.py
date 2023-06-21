import sys

from nicegui import ui


class Console(object):
    def __init__(self):
        self.log = ui.log().classes("w-full h-20")
        self.old_stderr = sys.stderr
        sys.stderr = self

    def write(self, data):
        self.log.push(data[:-1])
        self.log.update()

    def flush(self):
        self.log.update()

    def reset(self):
        self.log.clear()
        sys.stderr = self.old_stderr
