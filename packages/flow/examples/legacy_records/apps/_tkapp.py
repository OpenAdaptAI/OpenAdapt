"""A small keyboard-driven Tk shell shared by the two mock desktop apps.

Each app is a set of screens in one window. The window title always names the
current screen, so a driver can see where it is the same way a person does.
Every screen that shows a record uses a read-only text pane; Ctrl+A then
Ctrl+C copies the whole pane to the clipboard, as many legacy apps allow.

The apps have no API. Their only outputs are the screen, the clipboard and,
where the app offers one, an export file.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Optional

BANNER = "SYNTHETIC TEST DATA - PROTOTYPE"


class App:
    def __init__(self, name: str, *, width: int = 1000, height: int = 680) -> None:
        self.name = name
        self.root = tk.Tk(className=name.replace(" ", ""))
        self.root.geometry(f"{width}x{height}+0+0")
        self.mono = tkfont.Font(family="DejaVu Sans Mono", size=15)
        self.small = tkfont.Font(family="DejaVu Sans", size=11)
        tk.Label(
            self.root,
            text=f"{name}  |  {BANNER}",
            bg="#7a1f1f",
            fg="white",
            font=self.small,
            anchor="w",
            padx=8,
        ).pack(fill="x")
        self.body = tk.Frame(self.root, padx=16, pady=12)
        self.body.pack(fill="both", expand=True)
        self.status = tk.Label(
            self.root, text="", anchor="w", font=self.small, bg="#e8e8e8", padx=8
        )
        self.status.pack(fill="x", side="bottom")
        self.pane: Optional[tk.Text] = None
        self.root.bind_all("<Control-a>", self._select_all)
        self.root.bind_all("<Control-c>", self._copy)

    # -- screens -------------------------------------------------------------

    def _clear(self, title: str) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.pane = None
        self.root.title(f"{self.name} - {title}")

    def text_screen(self, title: str, lines: list[str], hint: str = "") -> None:
        self._clear(title)
        tk.Label(self.body, text=title, font=self.mono, anchor="w").pack(fill="x")
        pane = tk.Text(
            self.body,
            font=self.mono,
            height=16,
            wrap="none",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
        )
        pane.insert("1.0", "\n".join(lines))
        pane.configure(state="disabled")
        pane.pack(fill="both", expand=True, pady=(8, 0))
        pane.focus_set()
        self.pane = pane
        self.set_status(hint)

    def form(
        self,
        title: str,
        labels: list[str],
        on_save: Callable[[dict[str, str]], None],
        hint: str = "",
    ) -> None:
        self._clear(title)
        tk.Label(self.body, text=title, font=self.mono, anchor="w").pack(fill="x")
        grid = tk.Frame(self.body, pady=8)
        grid.pack(fill="x")
        entries: dict[str, tk.Entry] = {}
        for i, label in enumerate(labels):
            tk.Label(grid, text=label + ":", font=self.mono, anchor="e", width=16).grid(
                row=i, column=0, sticky="e", pady=4
            )
            e = tk.Entry(grid, font=self.mono, width=48)
            e.grid(row=i, column=1, sticky="w", pady=4)
            entries[label] = e
        entries[labels[0]].focus_set()

        def save(_evt: object = None) -> str:
            on_save({k: v.get() for k, v in entries.items()})
            return "break"

        for e in entries.values():
            e.bind("<Control-s>", save)
        self.set_status(hint or "Tab between fields. Ctrl+S saves. Esc cancels.")

    def prompt(self, title: str, label: str, on_enter: Callable[[str], None]) -> None:
        self._clear(title)
        tk.Label(self.body, text=label, font=self.mono, anchor="w").pack(fill="x")
        e = tk.Entry(self.body, font=self.mono, width=48)
        e.pack(anchor="w", pady=8)
        e.focus_set()
        e.bind("<Return>", lambda _e: on_enter(e.get()))
        self.set_status("Enter to continue. Esc cancels.")

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # -- clipboard -----------------------------------------------------------

    def _select_all(self, _evt: object = None) -> Optional[str]:
        if self.pane is not None:
            self.pane.tag_add("sel", "1.0", "end-1c")
            return "break"
        return None

    def _copy(self, _evt: object = None) -> Optional[str]:
        if self.pane is not None and self.pane.tag_ranges("sel"):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.pane.get("sel.first", "sel.last"))
            return "break"
        return None

    def bind(self, seq: str, fn: Callable[[], None]) -> None:
        self.root.bind_all(seq, lambda _e: (fn(), "break")[1])

    def run(self) -> None:
        self.root.mainloop()
