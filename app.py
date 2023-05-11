import os
import sys
import time
import tkinter as tk
from tkinter import simpledialog
from PIL import Image, ImageTk
from openadapt import record, visualize, replay, console


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OpenAdapt Alpha 0.1.0")
        self.geometry("600x400")
        self.resizable(False, False)
        self.image = tk.PhotoImage(file="assets/logo.png")
        self.title_label = tk.Label(
            self, text="OpenAdapt Alpha 0.1.0", image=self.image, compound="left"
        )

        # center window
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry("{}x{}+{}+{}".format(width, height, x, y))

        # display logo
        self.logo = ImageTk.PhotoImage(
            Image.open("assets/logo.png").resize((128, 128), Image.LANCZOS)
        )
        self.logo_label = tk.Label(self, image=self.logo, compound="left")
        self.logo_label.place(x=75, y=0, width=128, height=128)

        self.logo_label.bind(
            "<Button-1>",
            lambda e: on_logo_click(
                simpledialog.askstring("Task", "Enter a task name: ")
            ),
        )

        # display log
        self.log = console.Console(self)
        self.log.place(x=300, y=0, width=300, height=400)

        # add scrollbars
        self.vscrollbar = tk.Scrollbar(self, orient="vertical", command=self.log.yview)
        self.vscrollbar.place(x=590, y=6, width=10, height=390)

        self.hscrollbar = tk.Scrollbar(
            self, orient="horizontal", command=self.log.xview
        )
        self.hscrollbar.place(x=300, y=390, width=290, height=10)

        self.log.config(
            yscrollcommand=self.vscrollbar.set, xscrollcommand=self.hscrollbar.set
        )

        # add buttons
        self.record_button = tk.Button(
            self,
            text="Record",
            command=lambda: on_record(
                simpledialog.askstring("Record", "Enter a name for the recording: ")
            ),
        )

        self.record_button.place(x=50, y=140, width=200, height=50)

        self.visualize_button = tk.Button(
            self, text="Visualize", command=lambda: visualize.main()
        )
        self.visualize_button.place(x=50, y=204, width=200, height=50)

        self.replay_button = tk.Button(
            self, text="Replay", command=lambda: replay.replay("NaiveReplayStrategy")
        )
        self.replay_button.place(x=50, y=268, width=200, height=50)

        self.clear_cache_button = tk.Button(self, text="Clear Data", command=clear_db)
        self.clear_cache_button.place(x=50, y=332, width=200, height=50)


def on_logo_click(task):
    if task is not None:
        print("Task: {}".format(task), file=sys.stderr)
        print("Executing...", file=sys.stderr)
        replay.replay("NaiveReplayStrategy")
        os.system(
            "curl -X 'POST' \
            'http://localhost:3333/?data=write%20{}' \
            -H 'accept: application/json' \
            -d ''".format(
                task.replace(" ", "%20")
            )
        )
        print("Execution complete.", file=sys.stderr)

    else:
        print("Execution cancelled.", file=sys.stderr)


def on_record(name):
    if name is not None:
        # fails
        # record.record(name)

        # works
        time.sleep(1)
        print(
            "Recording {}... Press CTRL/CMD + C in log window to cancel".format(name),
            file=sys.stderr,
        )
        os.system("python3 -m openadapt.record \"{}\"".format(name))
        print("Recording complete.", file=sys.stderr)

    else:
        print("Recording cancelled.", file=sys.stderr)


def clear_db():
    os.remove("openadapt.db")
    os.system("alembic upgrade head")
    print("Database cleared.", file=sys.stderr)


if __name__ == "__main__":
    app = App()
    app.mainloop()
