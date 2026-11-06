"""Synthetic Tk account-note app for the loopback RDP campaign only."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

APP = "synthetic-account-notes"
ENVIRONMENT = "synthetic-account-notes:1280x800:24:lossless"
TARGET = "AC200001"
IDENTIFIER = (40, 100, 700, 50)
POINTS = ((240, 220), (580, 220), (260, 340), (580, 440))
MARKERS = ((1040, 20), (1100, 20), (1160, 20))


def marker_bytes(value: str) -> bytes:
    digest = hashlib.sha256(value.encode()).digest()
    pixels = bytearray()
    for y in range(52):
        for x in range(52):
            bit = ((y - 2) // 3) * 16 + (x - 2) // 3
            on = 2 <= x < 50 and 2 <= y < 50 and digest[bit // 8] & (128 >> (bit % 8))
            pixels.extend((255, 255, 255) if on else (0, 0, 0))
    return b"P6\n52 52\n255\n" + pixels


def main(database: Path) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry("1280x800+0+0")
    root.configure(background="white")
    version = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    markers = []
    for (x, y), value in zip(MARKERS, (APP, version, ENVIRONMENT)):
        picture = tk.PhotoImage(data=marker_bytes(value), format="PPM", master=root)
        markers.append(picture)
        tk.Label(root, image=picture, borderwidth=0, highlightthickness=0).place(x=x, y=y, width=52, height=52)
    font = ("DejaVu Sans", 18)
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT id, holder, note FROM accounts WHERE id=?", (TARGET,)).fetchone()
    if row is None:
        raise ValueError("synthetic target absent")
    tk.Label(root, text=f"Account {row[0]} — {row[1]}", font=font, background="white", anchor="w").place(x=40, y=100, width=700, height=50)
    tk.Label(root, text="Account lookup", font=font, background="white").place(x=40, y=165)
    query = tk.Entry(root, font=font)
    query.place(x=40, y=200, width=400, height=40)
    status = tk.Label(root, text="All accounts", font=font, background="white", anchor="w")
    status.place(x=40, y=260, width=700, height=40)
    note = tk.Entry(root, font=font)
    note.place(x=40, y=320, width=400, height=40)
    note.insert(0, row[2])
    selected = [False]

    def search() -> None:
        selected[0] = query.get() == TARGET
        status.configure(text="One matching account" if selected[0] else "No matching account")
        query.delete(0, tk.END)

    def save() -> None:
        if not selected[0]:
            status.configure(text="Select an account before saving")
            return
        with sqlite3.connect(database) as db:
            changed = db.execute("UPDATE accounts SET note=? WHERE id=?", (note.get(), TARGET)).rowcount
            if changed != 1:
                raise ValueError("unexpected update count")
        status.configure(text="Account note saved")

    tk.Button(root, text="Search", font=font, command=search).place(x=510, y=195, width=140, height=50)
    tk.Button(root, text="Save note", font=font, command=save).place(x=500, y=415, width=160, height=50)
    # Fixture startup owns focus; overlays must never change or repair it.
    root.update()
    query.focus_force()
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    main(parser.parse_args().database)
