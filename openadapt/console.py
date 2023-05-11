import sys
from tkinter import Text


class Console(Text):
    def __init__(self, *args, **kwargs):
        kwargs.update({"state": "disabled"})
        Text.__init__(self, *args, **kwargs)
        self.bind("<Destroy>", self.reset)
        self.old_stderr = sys.stderr
        sys.stderr = self

    def delete(self, *args, **kwargs):
        self.config(state="normal")
        self.delete(*args, **kwargs)
        self.config(state="disabled")

    def write(self, content):
        self.config(state="normal")
        self.insert("end", content)
        self.config(state="disabled")
        self.update()

    def flush(self):
        self.update()

    def reset(self, event):
        sys.stderr = self.old_stderr
