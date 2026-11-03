"""System of record for the Windows environment packs (SQLite, guest side).

Two self-built line-of-business apps keep their data here: Patient Chart
(WinForms; write a note on a patient's chart) and Claims Desk (WPF;
adjudicate a claim). The apps read and write only through this CLI. The
environment's oracle reads the same file through ``dump``, over a channel the
agent never touches (WinRM from the host), so the reward comes from the
database, never from the screen.

Planted faults live in ``<app>_fault.json`` beside the database. The apps
never read that file and always report the save as done, so the screen says
"saved" whatever the database holds:

* ``ok``: the write lands once.
* ``drop``: nothing is written.
* ``wrong_row``: the write lands on the target's look-alike record.
* ``duplicate``: the write lands twice.
* ``truncate``: the text lands with its last word cut.
* ``stale``: an old value is written instead of the new one (Patient Chart
  re-saves the chart's previous note; Claims Desk writes the claim's billed
  amount instead of the allowed amount entered).

Commands (all print one line of JSON or ``OK``)::

    seed  <app> [--fault F] [--variant V]   restore the pristine database
    digest <app>                            sha256 over every table
    dump  <app>                             {"records": [...]} for the oracle
    pc-list <filter> | pc-get <mrn> | pc-save <mrn> <note_b64>
    cd-list <filter> | cd-get <claim_no> | cd-save <claim_no> <status> <amount> <note_b64>

Text travels base64 so quotes and unicode survive the shell. Standard library
only: the guest has a stock CPython and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.environ.get("OA_PACK_DIR", r"C:\oa\pack")
APPS = ("patient_chart", "claims_desk")
FAULTS = ("ok", "drop", "wrong_row", "duplicate", "truncate", "stale")

# Fictional roster. Look-alike pairs differ by one transposed MRN digit and a
# near-identical name, so a wrong click is easy to make and hard to see.
PATIENTS = [
    ("MRN-104821", "Neil", "Sorenson", "1984-03-12"),
    ("MRN-104812", "Nell", "Sorensen", "1986-07-09"),
    ("MRN-220931", "Maria", "Alvarez", "1979-11-02"),
    ("MRN-220913", "Mario", "Alvarez", "1979-11-20"),
    ("MRN-318470", "Priya", "Chandra", "1990-02-14"),
    ("MRN-318407", "Priya", "Chandran", "1990-12-04"),
    ("MRN-402215", "Wei", "Chen", "1975-09-30"),
    ("MRN-402251", "Wei", "Cheng", "1975-03-09"),
    ("MRN-517736", "Fatima", "Noor", "1988-12-01"),
    ("MRN-517763", "Fatma", "Nour", "1988-10-21"),
    ("MRN-603318", "Oskar", "Bakke", "1992-06-18"),
    ("MRN-709924", "Robert", "Kowalski", "1968-08-08"),
    ("MRN-709942", "Roberta", "Kowalska", "1968-08-18"),
    ("MRN-811045", "Grace", "Okafor", "1995-04-25"),
    ("MRN-912260", "Tomas", "Lindqvist", "1971-01-30"),
    ("MRN-912206", "Thomas", "Lindquist", "1971-10-03"),
]
PATIENT_TWIN = {
    a: b
    for a, b in [
        ("MRN-104821", "MRN-104812"),
        ("MRN-220931", "MRN-220913"),
        ("MRN-318470", "MRN-318407"),
        ("MRN-402215", "MRN-402251"),
        ("MRN-517736", "MRN-517763"),
        ("MRN-709924", "MRN-709942"),
        ("MRN-912260", "MRN-912206"),
    ]
    for a, b in ((a, b), (b, a))
}
# Notes already on file, so a lost or misplaced note is not the only row.
PRIOR_NOTES = [
    ("MRN-104821", "Annual review, no changes", "2026-09-14T10:02:00Z"),
    ("MRN-220913", "Flu vaccine given, left arm", "2026-10-01T15:40:00Z"),
    ("MRN-709924", "BP 142/90, recheck in 1 month", "2026-10-19T09:15:00Z"),
]

CLAIMS = [
    # claim_no, member, member_id, service_date, billed, status
    ("CLM-2026-00417", "Ana Petrovic", "MEM-55120", "2026-10-02", "412.50", "Open"),
    ("CLM-2026-00471", "Ana Petrovic", "MEM-55102", "2026-10-20", "421.50", "Open"),
    ("CLM-2026-01388", "Daniel Osei", "MEM-60871", "2026-09-28", "1290.00", "Open"),
    ("CLM-2026-01838", "Daniela Osei", "MEM-60817", "2026-09-28", "1209.00", "Open"),
    ("CLM-2026-02254", "Hiro Tanaka", "MEM-71344", "2026-10-11", "86.40", "Open"),
    ("CLM-2026-02245", "Hiro Tanaka", "MEM-71434", "2026-10-11", "68.40", "Open"),
    ("CLM-2026-03109", "Leila Haddad", "MEM-80233", "2026-10-05", "2750.00", "Open"),
    ("CLM-2026-03190", "Layla Haddad", "MEM-80323", "2026-10-15", "2570.00", "Open"),
    ("CLM-2026-04462", "Samuel Reyes", "MEM-91005", "2026-08-30", "155.00", "Pended"),
    ("CLM-2026-04426", "Samuel Reyes", "MEM-91050", "2026-08-03", "515.00", "Open"),
    ("CLM-2026-05531", "Ines Carvalho", "MEM-12409", "2026-10-22", "640.00", "Open"),
    ("CLM-2026-06027", "Marek Novak", "MEM-23116", "2026-10-09", "980.25", "Open"),
    ("CLM-2026-06072", "Marek Nowak", "MEM-23161", "2026-10-09", "908.25", "Open"),
    ("CLM-2026-07714", "Chloe Martin", "MEM-34877", "2026-10-17", "220.00", "Open"),
]
CLAIM_TWIN = {
    a: b
    for a, b in [
        ("CLM-2026-00417", "CLM-2026-00471"),
        ("CLM-2026-01388", "CLM-2026-01838"),
        ("CLM-2026-02254", "CLM-2026-02245"),
        ("CLM-2026-03109", "CLM-2026-03190"),
        ("CLM-2026-04462", "CLM-2026-04426"),
        ("CLM-2026-06027", "CLM-2026-06072"),
    ]
    for a, b in ((a, b), (b, a))
}
PRIOR_ADJUDICATIONS = [
    (
        "CLM-2026-04462",
        "Pended",
        "0.00",
        "Awaiting itemized bill",
        "2026-09-02T11:00:00Z",
    ),
]


def _db(app: str) -> str:
    return os.path.join(ROOT, f"{app}.db")


def _pristine(app: str) -> str:
    return os.path.join(ROOT, f"{app}.pristine.db")


def _fault_path(app: str) -> str:
    return os.path.join(ROOT, f"{app}_fault.json")


def _conn(app: str) -> sqlite3.Connection:
    con = sqlite3.connect(_db(app))
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build(path: str, app: str, variant: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    if app == "patient_chart":
        con.execute(
            "CREATE TABLE patients (mrn TEXT PRIMARY KEY, first TEXT, last TEXT, "
            "dob TEXT, sort INTEGER)"
        )
        con.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, mrn TEXT, "
            "note TEXT, written_at TEXT)"
        )
        rows = list(PATIENTS)
        if variant == "reorder":
            rows.reverse()
        con.executemany(
            "INSERT INTO patients VALUES (?,?,?,?,?)",
            [(*row, i) for i, row in enumerate(rows)],
        )
        con.executemany(
            "INSERT INTO notes (mrn, note, written_at) VALUES (?,?,?)", PRIOR_NOTES
        )
    else:
        con.execute(
            "CREATE TABLE claims (claim_no TEXT PRIMARY KEY, member TEXT, "
            "member_id TEXT, service_date TEXT, billed TEXT, status TEXT, sort INTEGER)"
        )
        con.execute(
            "CREATE TABLE adjudications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "claim_no TEXT, status TEXT, allowed_amount TEXT, note TEXT, written_at TEXT)"
        )
        rows = list(CLAIMS)
        if variant == "reorder":
            rows.reverse()
        con.executemany(
            "INSERT INTO claims VALUES (?,?,?,?,?,?,?)",
            [(*row, i) for i, row in enumerate(rows)],
        )
        con.executemany(
            "INSERT INTO adjudications (claim_no, status, allowed_amount, note, "
            "written_at) VALUES (?,?,?,?,?)",
            PRIOR_ADJUDICATIONS,
        )
    con.commit()
    con.close()


def seed(app: str, fault: str = "ok", variant: str = "none") -> None:
    """Restore the pristine database (built once per variant) and set the fault.

    A restore is a file copy of a database built from the fixed seed, so two
    episodes start from byte-identical tables whatever the last one did.
    """
    if app not in APPS:
        raise SystemExit(f"unknown app {app!r}")
    if fault not in FAULTS:
        raise SystemExit(f"unknown fault {fault!r}")
    os.makedirs(ROOT, exist_ok=True)
    pristine = _pristine(app) + ("" if variant == "none" else f".{variant}")
    if not os.path.exists(pristine):
        _build(pristine, app, variant)
    for suffix in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(_db(app) + suffix):
            os.remove(_db(app) + suffix)
    shutil.copyfile(pristine, _db(app))
    with open(_fault_path(app), "w", encoding="ascii") as fh:
        json.dump({"fault": fault}, fh)


def _fault(app: str) -> str:
    try:
        with open(_fault_path(app), encoding="ascii") as fh:
            return str(json.load(fh).get("fault") or "ok")
    except (OSError, ValueError):
        return "ok"


def digest(app: str) -> str:
    con = _conn(app)
    h = hashlib.sha256()
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ):
        h.update(name.encode())
        for row in con.execute(f"SELECT * FROM {name} ORDER BY 1"):
            h.update(json.dumps(list(row)).encode())
    return h.hexdigest()


def dump(app: str) -> dict:
    con = _conn(app)
    if app == "patient_chart":
        rows = con.execute(
            "SELECT n.id, n.mrn, p.first, p.last, n.note, n.written_at FROM notes n "
            "LEFT JOIN patients p ON p.mrn = n.mrn ORDER BY n.id"
        )
    else:
        rows = con.execute(
            "SELECT a.id, a.claim_no, c.member, a.status, a.allowed_amount, a.note, "
            "a.written_at FROM adjudications a LEFT JOIN claims c "
            "ON c.claim_no = a.claim_no ORDER BY a.id"
        )
    records = [
        {k: ("" if v is None else str(v)) for k, v in dict(r).items()} for r in rows
    ]
    return {"records": records}


def _cut_last_word(text: str) -> str:
    head, _, _ = text.rstrip().rpartition(" ")
    return head or text[:-1]


def pc_save(mrn: str, note: str) -> None:
    fault = _fault("patient_chart")
    con = _conn("patient_chart")
    if fault == "drop":
        return
    if fault == "wrong_row":
        mrn = PATIENT_TWIN.get(mrn, mrn)
    if fault == "truncate":
        note = _cut_last_word(note)
    if fault == "stale":
        last = con.execute(
            "SELECT note FROM notes WHERE mrn=? ORDER BY id DESC LIMIT 1", (mrn,)
        ).fetchone()
        note = last["note"] if last else ""
    times = 2 if fault == "duplicate" else 1
    for _ in range(times):
        con.execute(
            "INSERT INTO notes (mrn, note, written_at) VALUES (?,?,?)",
            (mrn, note, _now()),
        )
    con.commit()


def cd_save(claim_no: str, status: str, amount: str, note: str) -> None:
    fault = _fault("claims_desk")
    con = _conn("claims_desk")
    if fault == "drop":
        return
    if fault == "wrong_row":
        claim_no = CLAIM_TWIN.get(claim_no, claim_no)
    if fault == "truncate":
        note = _cut_last_word(note)
    if fault == "stale":
        row = con.execute(
            "SELECT billed FROM claims WHERE claim_no=?", (claim_no,)
        ).fetchone()
        amount = row["billed"] if row else amount
    times = 2 if fault == "duplicate" else 1
    for _ in range(times):
        con.execute(
            "INSERT INTO adjudications (claim_no, status, allowed_amount, note, written_at) "
            "VALUES (?,?,?,?,?)",
            (claim_no, status, amount, note, _now()),
        )
    con.execute("UPDATE claims SET status=? WHERE claim_no=?", (status, claim_no))
    con.commit()


def _b64(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "seed":
        app = args[0]
        opts = dict(zip(args[1::2], args[2::2]))
        seed(app, opts.get("--fault", "ok"), opts.get("--variant", "none"))
        print(json.dumps({"seeded": app, "digest": digest(app)}))
    elif cmd == "digest":
        print(json.dumps({"digest": digest(args[0])}))
    elif cmd == "dump":
        print(json.dumps(dump(args[0])))
    elif cmd == "pc-list":
        like = f"%{args[0] if args else ''}%"
        rows = _conn("patient_chart").execute(
            "SELECT mrn, first, last, dob FROM patients WHERE first LIKE ? OR last LIKE ? "
            "OR mrn LIKE ? ORDER BY sort",
            (like, like, like),
        )
        print(json.dumps([dict(r) for r in rows]))
    elif cmd == "pc-get":
        con = _conn("patient_chart")
        p = con.execute("SELECT * FROM patients WHERE mrn=?", (args[0],)).fetchone()
        last = con.execute(
            "SELECT note FROM notes WHERE mrn=? ORDER BY id DESC LIMIT 1", (args[0],)
        ).fetchone()
        count = con.execute(
            "SELECT COUNT(*) FROM notes WHERE mrn=?", (args[0],)
        ).fetchone()[0]
        out = dict(p) if p else {}
        out["last_note"] = last["note"] if last else ""
        out["note_count"] = count
        print(json.dumps(out))
    elif cmd == "pc-save":
        pc_save(args[0], _b64(args[1]))
        print("OK")
    elif cmd == "cd-list":
        like = f"%{args[0] if args else ''}%"
        rows = _conn("claims_desk").execute(
            "SELECT claim_no, member, member_id, service_date, billed, status FROM claims "
            "WHERE claim_no LIKE ? OR member LIKE ? OR member_id LIKE ? ORDER BY sort",
            (like, like, like),
        )
        print(json.dumps([dict(r) for r in rows]))
    elif cmd == "cd-get":
        con = _conn("claims_desk")
        row = con.execute(
            "SELECT * FROM claims WHERE claim_no=?", (args[0],)
        ).fetchone()
        last = con.execute(
            "SELECT status, allowed_amount, note FROM adjudications WHERE claim_no=? "
            "ORDER BY id DESC LIMIT 1",
            (args[0],),
        ).fetchone()
        count = con.execute(
            "SELECT COUNT(*) FROM adjudications WHERE claim_no=?", (args[0],)
        ).fetchone()[0]
        out = dict(row) if row else {}
        out["last_decision"] = dict(last) if last else None
        out["decision_count"] = count
        print(json.dumps(out))
    elif cmd == "cd-save":
        cd_save(args[0], args[1], args[2], _b64(args[3]))
        print("OK")
    else:
        print(f"unknown command {cmd!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
