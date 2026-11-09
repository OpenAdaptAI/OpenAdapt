"""Mock EMR: a desktop records app with no API, for the referral template.

SYNTHETIC DATA ONLY. Every person in the seed data is a made-up test record.

    python mock_emr.py --db emr.sqlite --seed            # create test charts
    python mock_emr.py --db emr.sqlite                   # run
    python mock_emr.py --db emr.sqlite --drop-save       # says "Saved", keeps nothing

Keys: Ctrl+F search by name and date of birth, Ctrl+N new chart, Ctrl+O open
chart by record id, Ctrl+T new triage entry on the open chart, Ctrl+S save,
Esc back to search, Ctrl+A then Ctrl+C copy the record pane.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

from _tkapp import App

SEED = [
    ("REC-100001", "TESTPATIENT, BRAVO", "1980-02-02", "TEST-0000-0002"),
    ("REC-100002", "TESTPATIENT, CHARLIE", "1990-03-03", "TEST-0000-0003"),
    ("REC-100003", "TESTPATIENT, CHARLIE", "1990-03-03", "TEST-0000-0033"),
]


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS chart (record_id TEXT PRIMARY KEY, name TEXT,
            dob TEXT, subject_id TEXT);
        CREATE TABLE IF NOT EXISTS triage (record_id TEXT, priority TEXT,
            referral_code TEXT, document TEXT, entered TEXT);
        """
    )
    return con


class MockEmr:
    def __init__(self, db: Path, drop_save: bool) -> None:
        self.con = connect(db)
        self.drop_save = drop_save
        self.app = App("Mock EMR")
        self.open_id: str | None = None
        a = self.app
        a.bind("<Control-f>", self.search)
        a.bind("<Control-n>", self.new_chart)
        a.bind("<Control-o>", self.open_prompt)
        a.bind("<Control-t>", self.new_triage)
        a.bind("<Escape>", self.search)
        self.search()

    def search(self) -> None:
        self.open_id = None
        self.app.prompt(
            "Search", "Name and date of birth (NAME YYYY-MM-DD):", self.results
        )

    def results(self, query: str) -> None:
        parts = query.strip().rsplit(" ", 1)
        name, dob = (parts[0], parts[1]) if len(parts) == 2 else (query.strip(), "")
        rows = self.con.execute(
            "SELECT record_id, name, dob FROM chart WHERE upper(name)=upper(?) AND dob=?",
            (name.strip(), dob),
        ).fetchall()
        lines = [f"Matches: {len(rows)}"] + [f"{r[0]}  {r[1]}  {r[2]}" for r in rows]
        self.app.text_screen(
            f"Search results ({len(rows)})",
            lines,
            "Ctrl+O opens a chart by record id. Ctrl+N creates a chart.",
        )

    def new_chart(self) -> None:
        self.app.form(
            "New chart", ["Name", "Date of birth", "Subject ID"], self.save_chart
        )

    def save_chart(self, v: dict[str, str]) -> None:
        n = self.con.execute("SELECT count(*) FROM chart").fetchone()[0]
        rid = f"REC-{100001 + n + 40:06d}"
        self.con.execute(
            "INSERT INTO chart VALUES (?,?,?,?)",
            (
                rid,
                v["Name"].strip(),
                v["Date of birth"].strip(),
                v["Subject ID"].strip(),
            ),
        )
        self.con.commit()
        self.chart(rid, "Chart created")

    def open_prompt(self) -> None:
        self.app.prompt("Open chart", "Record id:", lambda rid: self.chart(rid.strip()))

    def chart(self, rid: str, status: str = "") -> None:
        row = self.con.execute(
            "SELECT * FROM chart WHERE record_id=?", (rid,)
        ).fetchone()
        if row is None:
            self.app.text_screen("Not found", [f"No chart {rid}"], "Esc to search.")
            return
        self.open_id = rid
        tri = self.con.execute(
            "SELECT priority, referral_code, document, entered FROM triage "
            "WHERE record_id=? ORDER BY rowid",
            (rid,),
        ).fetchall()
        lines = [
            f"Record: {row[0]}",
            f"Name: {row[1]}",
            f"DOB: {row[2]}",
            f"Subject ID: {row[3]}",
            "",
        ]
        if tri:
            last = tri[-1]
            lines += [
                f"Triage entries: {len(tri)}",
                f"Priority: {last[0]}",
                f"Referral code: {last[1]}",
                f"Document: {last[2]}",
                f"Entered: {last[3]}",
            ]
        else:
            lines += ["Triage entries: 0"]
        self.app.text_screen(
            f"Chart {rid}",
            lines,
            status or "Ctrl+T adds a triage entry. Esc closes the chart.",
        )

    def new_triage(self) -> None:
        if self.open_id is None:
            return
        rid = self.open_id
        self.app.form(
            f"Triage entry {rid}",
            ["Priority", "Referral code", "Document"],
            lambda v: self.save_triage(rid, v),
        )

    def save_triage(self, rid: str, v: dict[str, str]) -> None:
        if not self.drop_save:
            self.con.execute(
                "INSERT INTO triage VALUES (?,?,?,?,?)",
                (
                    rid,
                    v["Priority"].strip(),
                    v["Referral code"].strip(),
                    v["Document"].strip(),
                    date.today().isoformat(),
                ),
            )
            self.con.commit()
        # The fault mode still reports success, like an app that drops a save.
        self.app.text_screen(f"Saved {rid}", ["Saved."], "Esc closes the chart.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--seed", action="store_true")
    p.add_argument("--drop-save", action="store_true")
    a = p.parse_args()
    if a.seed:
        con = connect(a.db)
        con.executemany("INSERT OR REPLACE INTO chart VALUES (?,?,?,?)", SEED)
        con.commit()
        return
    MockEmr(a.db, a.drop_save).app.run()


if __name__ == "__main__":
    main()
