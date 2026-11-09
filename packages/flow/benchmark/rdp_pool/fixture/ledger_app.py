"""Synthetic account lookup screen for the RDP pool benchmark.

A Tk window with an account-number box. Enter shows the account's holder and
balance. Accounts are generated from a seed; no real data.

Run: ``DISPLAY=:N python3 ledger_app.py --seed 1``
"""

from __future__ import annotations

import argparse
import random
import tkinter as tk


def accounts(seed: int, n: int = 500) -> dict[str, tuple[str, str]]:
    rng = random.Random(seed)
    first = ["Alex", "Brook", "Casey", "Drew", "Emery", "Finley", "Harper", "Jordan"]
    last = ["Archer", "Brooks", "Carver", "Dalton", "Ellis", "Foster", "Grant", "Hayes"]
    out: dict[str, tuple[str, str]] = {}
    while len(out) < n:
        acct = f"AC{rng.randint(200000, 999999)}"
        out[acct] = (
            f"{rng.choice(last)}, {rng.choice(first)}",
            f"{rng.randint(10, 99999)}.{rng.randint(0, 99):02d}",
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    args = ap.parse_args()
    data = accounts(args.seed)

    root = tk.Tk()
    root.title("Ledger - Account lookup")
    root.geometry(f"{args.width}x{args.height}+0+0")
    root.configure(bg="white")
    font = ("DejaVu Sans", 18)
    tk.Label(root, text="Account", font=font, bg="white").place(x=40, y=40)
    entry = tk.Entry(root, font=font, width=14)
    entry.place(x=180, y=36)
    holder = tk.Label(root, text="", font=font, bg="white", anchor="w", width=40)
    holder.place(x=40, y=120)
    balance = tk.Label(root, text="", font=font, bg="white", anchor="w", width=40)
    balance.place(x=40, y=170)

    def lookup(_event: object = None) -> None:
        acct = entry.get().strip().upper()
        found = data.get(acct)
        holder.configure(text=f"Holder: {found[0]}" if found else "Holder: not found")
        balance.configure(text=f"Balance: {found[1]}" if found else "Balance: -")
        entry.delete(0, tk.END)

    entry.bind("<Return>", lookup)
    entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
