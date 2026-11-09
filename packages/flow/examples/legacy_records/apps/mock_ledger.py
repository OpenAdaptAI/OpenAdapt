"""Mock Ledger: a desktop accounts-payable app with no API.

SYNTHETIC DATA ONLY. Suppliers and bills are made up.

    python mock_ledger.py --db ledger.sqlite --seed
    python mock_ledger.py --db ledger.sqlite --export-dir exports
    python mock_ledger.py --db ledger.sqlite --drop-save
    python mock_ledger.py --db ledger.sqlite --export-only --export-dir exports

``--export-only`` is the scheduled export: a separate process that writes
the saved bills to CSV without the UI. Ctrl+E in the app is an export from
the live session.

Keys: Ctrl+F find supplier by id, Ctrl+B new bill for the open supplier,
Ctrl+S save, Ctrl+O open bill by id, Ctrl+E export all bills to CSV, Esc back,
Ctrl+A then Ctrl+C copy the record pane.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

from _tkapp import App

SEED = [
    ("V-0042", "Example Office Supply Co", "Net 30"),
    ("V-0077", "Sample Facilities Ltd", "Net 15"),
]


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS supplier (supplier_id TEXT PRIMARY KEY,
            name TEXT, terms TEXT);
        CREATE TABLE IF NOT EXISTS bill (bill_id TEXT PRIMARY KEY, supplier_id TEXT,
            invoice_no TEXT, amount TEXT, due_date TEXT, status TEXT);
        """
    )
    return con


def export_bills(con: sqlite3.Connection, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = con.execute("SELECT * FROM bill ORDER BY bill_id").fetchall()
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["bill_id", "supplier_id", "invoice_no", "amount", "due_date", "status"]
        )
        w.writerows(rows)
    tmp.replace(out)
    return len(rows)


class MockLedger:
    def __init__(self, db: Path, export_dir: Path, drop_save: bool) -> None:
        self.con = connect(db)
        self.export_dir = export_dir
        self.drop_save = drop_save
        self.app = App("Mock Ledger")
        self.supplier: str | None = None
        a = self.app
        a.bind("<Control-f>", self.find)
        a.bind("<Control-b>", self.new_bill)
        a.bind("<Control-o>", self.open_prompt)
        a.bind("<Control-e>", self.export)
        a.bind("<Escape>", self.find)
        self.find()

    def find(self) -> None:
        self.supplier = None
        self.app.prompt("Find supplier", "Supplier id:", self.show_supplier)

    def show_supplier(self, sid: str) -> None:
        sid = sid.strip()
        row = self.con.execute(
            "SELECT * FROM supplier WHERE supplier_id=?", (sid,)
        ).fetchone()
        if row is None:
            self.app.text_screen(
                "Supplier not found", ["Matches: 0", f"No supplier {sid}"]
            )
            return
        self.supplier = sid
        n = self.con.execute(
            "SELECT count(*) FROM bill WHERE supplier_id=?", (sid,)
        ).fetchone()[0]
        self.app.text_screen(
            f"Supplier {sid}",
            [
                "Matches: 1",
                f"Supplier: {row[0]}",
                f"Name: {row[1]}",
                f"Terms: {row[2]}",
                f"Open bills: {n}",
            ],
            "Ctrl+B enters a bill for this supplier.",
        )

    def new_bill(self) -> None:
        if self.supplier is None:
            return
        sid = self.supplier
        self.app.form(
            f"New bill {sid}",
            ["Invoice no", "Amount", "Due date"],
            lambda v: self.save_bill(sid, v),
        )

    def save_bill(self, sid: str, v: dict[str, str]) -> None:
        n = self.con.execute("SELECT count(*) FROM bill").fetchone()[0]
        bid = f"B-{318 + n:06d}"
        if not self.drop_save:
            self.con.execute(
                "INSERT INTO bill VALUES (?,?,?,?,?,?)",
                (
                    bid,
                    sid,
                    v["Invoice no"].strip(),
                    v["Amount"].strip(),
                    v["Due date"].strip(),
                    "approved",
                ),
            )
            self.con.commit()
        self.app.text_screen(
            f"Saved {bid}", [f"Saved bill {bid}."], "Ctrl+O opens a bill."
        )

    def open_prompt(self) -> None:
        self.app.prompt("Open bill", "Bill id:", lambda b: self.bill(b.strip()))

    def bill(self, bid: str) -> None:
        row = self.con.execute("SELECT * FROM bill WHERE bill_id=?", (bid,)).fetchone()
        if row is None:
            self.app.text_screen("Bill not found", [f"No bill {bid}"])
            return
        self.app.text_screen(
            f"Bill {bid}",
            [
                f"Bill: {row[0]}",
                f"Supplier: {row[1]}",
                f"Invoice no: {row[2]}",
                f"Amount: {row[3]}",
                f"Due date: {row[4]}",
                f"Status: {row[5]}",
            ],
            "Ctrl+E exports all bills. Esc goes back.",
        )

    def export(self) -> None:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        out = self.export_dir / "bills.csv"
        rows = self.con.execute("SELECT * FROM bill ORDER BY bill_id").fetchall()
        with out.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                ["bill_id", "supplier_id", "invoice_no", "amount", "due_date", "status"]
            )
            w.writerows(rows)
        self.app.set_status(f"Exported {len(rows)} bills to {out.name}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--export-dir", type=Path, default=Path("exports"))
    p.add_argument("--seed", action="store_true")
    p.add_argument("--drop-save", action="store_true")
    p.add_argument("--export-only", action="store_true")
    a = p.parse_args()
    if a.export_only:
        export_bills(connect(a.db), a.export_dir / "scheduled" / "bills.csv")
        return
    if a.seed:
        con = connect(a.db)
        con.executemany("INSERT OR REPLACE INTO supplier VALUES (?,?,?)", SEED)
        con.commit()
        return
    MockLedger(a.db, a.export_dir, a.drop_save).app.run()


if __name__ == "__main__":
    main()
