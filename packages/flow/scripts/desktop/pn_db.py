"""SQLite ground-truth layer for the desktop-benchmark harness app.

This is the *substitute* target app's data store (OpenDental's MariaDB demo
DB could not be installed no-touch — its trial is a 149 MB interactive
bootstrapper gated by SmartScreen + a UAC secure-desktop prompt; see
docs/desktop/PHASE2.md). A WinForms UI (patient_notes.ps1) reads/writes
through this CLI; the benchmark judge reads the same SQLite file directly, so
success is decided by DB state, never OCR.

Commands (all print machine-readable output):

    seed [--drift none|siblings|reorder]   (re)create + seed the DB
    list [filter]                          JSON list of patients (name match)
    save <id> <note_base64>                set a patient's note; prints OK
    get  <id>                              JSON of one patient
    all                                    JSON of every patient (ground truth)

Notes travel base64 so arbitrary text (unicode, quotes) survives the shell.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("PN_DB", r"C:\oa\patients.db")

# Fixed fictional roster. Includes a deliberate lookalike/sibling pair
# (Neil Sorenson / Nell Sorensen) — the identity work's first DESKTOP test —
# and same-surname neighbours for list reorder/insert drift.
SEED = [
    (1, "Neil", "Sorenson", "1984-03-12", ""),
    (2, "Nell", "Sorensen", "1986-07-09", ""),
    (3, "Maria", "Alvarez", "1979-11-02", ""),
    (4, "James", "Alvarez", "1981-05-21", ""),
    (5, "Priya", "Chandra", "1990-02-14", ""),
    (6, "Wei", "Chen", "1975-09-30", ""),
    (7, "Fatima", "Noor", "1988-12-01", ""),
    (8, "Oskar", "Bakke", "1992-06-18", ""),
    (9, "Robert", "Kowalski", "1968-08-08", ""),
    (10, "Grace", "Okafor", "1995-04-25", ""),
]

# A near-lexical sibling (Neil Sorensen ~ Neil Sorenson, adjacent DOB): the
# HARD identity case -- 1-char surname + 1-digit DOB differences sit inside an
# OCR-jitter-tolerant matcher's noise floor.
DRIFT_SIBLINGS = [(11, "Neil", "Sorensen", "1983-01-19", "")]

# A DISTINCT decoy sharing only the searched given name (very different
# surname + DOB) that sorts ABOVE the real patient (so row 0 is the wrong one
# for a positional selector): the discriminable case identity should resolve.
DRIFT_DECOY = [(12, "Neil", "Anderson", "1972-10-04", "")]


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def seed(drift: str = "none") -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = _conn()
    con.execute("DROP TABLE IF EXISTS patients")
    con.execute(
        "CREATE TABLE patients (id INTEGER PRIMARY KEY, first TEXT, "
        "last TEXT, dob TEXT, note TEXT)"
    )
    rows = list(SEED)
    if drift == "siblings":
        rows += DRIFT_SIBLINGS
    if drift == "decoy":
        rows += DRIFT_DECOY
    if drift == "reorder":
        rows = list(reversed(rows))
    con.executemany(
        "INSERT INTO patients (id, first, last, dob, note) VALUES (?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    print(json.dumps({"status": "ok", "seeded": len(rows), "drift": drift}))


def _patient_dict(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "first": r["first"], "last": r["last"],
            "dob": r["dob"], "note": r["note"] or ""}


def list_patients(filt: str = "") -> None:
    con = _conn()
    if filt:
        like = f"%{filt}%"
        cur = con.execute(
            "SELECT * FROM patients WHERE first LIKE ? OR last LIKE ? "
            "OR (first || ' ' || last) LIKE ? ORDER BY last, first",
            (like, like, like),
        )
    else:
        cur = con.execute("SELECT * FROM patients ORDER BY last, first")
    out = [_patient_dict(r) for r in cur.fetchall()]
    con.close()
    print(json.dumps(out))


def save(pid: int, note_b64: str) -> None:
    note = base64.b64decode(note_b64).decode("utf-8")
    con = _conn()
    con.execute("UPDATE patients SET note = ? WHERE id = ?", (note, pid))
    con.commit()
    changed = con.total_changes
    con.close()
    print(json.dumps({"status": "ok", "id": pid, "changed": changed}))


def get(pid: int) -> None:
    con = _conn()
    r = con.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    con.close()
    print(json.dumps(_patient_dict(r) if r else None))


def dump_all() -> None:
    con = _conn()
    rows = [_patient_dict(r) for r in
            con.execute("SELECT * FROM patients ORDER BY id").fetchall()]
    con.close()
    print(json.dumps(rows))


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: pn_db.py <seed|list|save|get|all> ...", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "seed":
        drift = "none"
        if "--drift" in argv:
            drift = argv[argv.index("--drift") + 1]
        seed(drift)
    elif cmd == "list":
        list_patients(argv[1] if len(argv) > 1 else "")
    elif cmd == "save":
        save(int(argv[1]), argv[2])
    elif cmd == "get":
        get(int(argv[1]))
    elif cmd == "all":
        dump_all()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
