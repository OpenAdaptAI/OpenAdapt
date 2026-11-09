"""A strip under the app that shows the latest lines of the run logs (demo only)."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

logs = [Path(p) for p in sys.argv[1:]]
root = tk.Tk(className="DemoLog")
root.title("Demo log")
root.geometry("1000x110+0+690")
root.configure(bg="#111")
label = tk.Label(
    root,
    text="",
    justify="left",
    anchor="nw",
    bg="#111",
    fg="#e6e6e6",
    font=("DejaVu Sans Mono", 11),
    padx=10,
    pady=6,
)
label.pack(fill="both", expand=True)


def tick() -> None:
    lines: list[str] = []
    for p in logs:
        if p.is_file():
            lines += [ln for ln in p.read_text().splitlines() if ln.strip()]
    label.configure(text="PROTOTYPE - synthetic data\n" + "\n".join(lines[-4:]))
    root.after(250, tick)


tick()
root.mainloop()
