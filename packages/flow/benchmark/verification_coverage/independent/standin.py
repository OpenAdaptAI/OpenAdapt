"""An SQLite copy of the pinned OpenEMR fixture's baseline, and the ops the
fault suite runs on it.

The stand-in is built from the fixture's own MariaDB dump (``baseline.sql``),
so every table, column and default is OpenEMR's, and every row OpenEMR's
installer writes is there. What it can't reproduce is OpenEMR's PHP: the ops
below write the rows the New Patient path and the edit screens write, and
the audit-log rows OpenEMR's ``EventAuditLogger`` writes for them. The live
run (``live.py``) checks those emulations against the real app.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

_CREATE = re.compile(r"^CREATE TABLE `([A-Za-z0-9_]+)` \($")
_COLUMN = re.compile(r"^  `([A-Za-z0-9_]+)` (.*?),?$")
_DEFAULT = re.compile(
    r"DEFAULT ('(?:[^'\\]|\\.)*'|NULL|-?[0-9.]+|current_timestamp\(\))", re.I
)
_INSERT = re.compile(r"^INSERT INTO `([A-Za-z0-9_]+)` VALUES ?(.*)$", re.S)


def _values(text: str) -> Iterator[list[Any]]:
    """The rows of a mysqldump ``VALUES (..),(..)`` list."""

    i, n = 0, len(text)
    while i < n:
        if text[i] != "(":
            i += 1
            continue
        i += 1
        row: list[Any] = []
        while True:
            if text[i] == "'":
                i += 1
                buf = []
                while text[i] != "'":
                    if text[i] == "\\":
                        nxt = text[i + 1]
                        buf.append(
                            {"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(nxt, nxt)
                        )
                        i += 2
                    else:
                        buf.append(text[i])
                        i += 1
                i += 1
                row.append("".join(buf))
            else:
                j = i
                while text[j] not in ",)":
                    j += 1
                tok = text[i:j]
                i = j
                if tok == "NULL":
                    row.append(None)
                elif tok.startswith("_binary "):
                    row.append(tok)
                else:
                    try:
                        row.append(int(tok))
                    except ValueError:
                        try:
                            row.append(float(tok))
                        except ValueError:
                            row.append(tok)
            if text[i] == ",":
                i += 1
                continue
            if text[i] == ")":
                i += 1
                break
        yield row


def _blob(value: Any) -> Any:
    """Binary columns (uuid) come out of the dump as undecodable text."""

    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "surrogateescape")
    return value


def _affinity(definition: str) -> str:
    """The SQLite type affinity for a MariaDB column type.

    With it, ``'5' = 5`` holds in a query as it does in MariaDB; without it a
    parameter bound as text never matches an integer column.
    """

    kind = definition.split("(", 1)[0].split(" ", 1)[0].lower()
    if kind in ("int", "bigint", "tinyint", "smallint", "mediumint"):
        return "INTEGER"
    if kind in ("decimal", "float", "double", "real"):
        return "REAL"
    if kind in ("binary", "varbinary", "blob", "longblob", "mediumblob", "tinyblob"):
        return "BLOB"
    return "TEXT"


def _default_sql(definition: str) -> str:
    m = _DEFAULT.search(definition)
    if not m:
        return ""
    raw = m.group(1)
    if raw.lower() == "current_timestamp()":
        return " DEFAULT CURRENT_TIMESTAMP"
    if raw.startswith("'"):
        return " DEFAULT '" + raw[1:-1].replace("\\'", "''") + "'"
    return f" DEFAULT {raw}"


def build(baseline_sql: Path, out: Path) -> dict[str, int]:
    """Convert the dump into ``out`` (every table, every row). Row counts."""

    out.unlink(missing_ok=True)
    con = sqlite3.connect(out)
    table: Optional[str] = None
    columns: list[str] = []
    counts: dict[str, int] = {}
    pending: Optional[tuple[str, list[str]]] = None

    def flush(name: str, body: str) -> None:
        rows = [[_blob(v) for v in row] for row in _values(body)]
        if rows:
            marks = ",".join("?" * len(rows[0]))
            con.executemany(f'INSERT INTO "{name}" VALUES ({marks})', rows)
            counts[name] = counts.get(name, 0) + len(rows)

    with baseline_sql.open(encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if pending is not None:
                pending[1].append(line)
                if line.endswith(";"):
                    flush(pending[0], "\n".join(pending[1])[:-1])
                    pending = None
                continue
            if table is None:
                m = _CREATE.match(line)
                if m:
                    table, columns = m.group(1), []
                    continue
                m = _INSERT.match(line)
                if m:
                    if line.endswith(";"):
                        flush(m.group(1), m.group(2)[:-1])
                    else:
                        pending = (m.group(1), [m.group(2)])
                continue
            if line.startswith(")"):
                con.execute(f'CREATE TABLE "{table}" ({", ".join(columns)})')
                counts.setdefault(table, 0)
                table = None
                continue
            m = _COLUMN.match(line)
            if m:
                name, definition = m.groups()
                pk = (
                    " PRIMARY KEY AUTOINCREMENT"
                    if "AUTO_INCREMENT" in definition
                    else ""
                )
                columns.append(
                    f'"{name}" {_affinity(definition)}' + pk + _default_sql(definition)
                )
    con.commit()
    con.close()
    return counts


def keep_tables(path: Path, keep: tuple[str, ...]) -> None:
    """Drop every table not in ``keep`` (the clinical-slice stand-in)."""

    con = sqlite3.connect(path)
    names = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for name in names:
        if name not in keep:
            con.execute(f'DROP TABLE "{name}"')
    con.commit()
    con.execute("VACUUM")
    con.close()


def copy(src: Path, dst: Path) -> Path:
    shutil.copyfile(src, dst)
    return dst


class Store:
    """The ops of ``faults.yaml`` on one stand-in database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.con = sqlite3.connect(path, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.clock = 0

    def close(self) -> None:
        self.con.close()

    def _now(self) -> str:
        self.clock += 1
        return f"2026-10-24 12:{self.clock // 60:02d}:{self.clock % 60:02d}"

    def one(self, sql: str, args: tuple = ()) -> Any:
        row = self.con.execute(sql, args).fetchone()
        return None if row is None else row[0]

    def log(self, user: Optional[str], event: str, pid: int, statement: str) -> None:
        """An audit row as OpenEMR's EventAuditLogger writes it, or none.

        OpenEMR logs each statement base64-encoded in ``comments`` and pairs
        every row with a ``log_comment_encrypt`` row holding its checksum.
        """

        if user is None:
            return
        comment = base64.b64encode(statement.encode()).decode()
        cur = self.con.execute(
            "INSERT INTO log (date, event, category, user, groupname, comments, "
            "patient_id, success, log_from) VALUES (?,?,?,?,?,?,?,1,'open-emr')",
            (self._now(), event, "Patient Demographics", user, "Default", comment, pid),
        )
        self.con.execute(
            "INSERT INTO log_comment_encrypt (log_id, encrypt, checksum, checksum_api, "
            "version) VALUES (?, 'No', ?, '', 4)",
            (cur.lastrowid, hashlib.sha512(comment.encode()).hexdigest()),
        )

    def _uuid(self, table: str, table_id: int) -> bytes:
        uuid = os.urandom(16)
        self.con.execute(
            "INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, "
            "couchdb, document_drive, mapped, created) VALUES (?,?,?,'','',0,0,?)",
            (uuid, table, str(table_id), self._now()),
        )
        return uuid

    def user_id(self, username: str) -> int:
        return int(self.one("SELECT id FROM users WHERE username = ?", (username,)))

    def add_user(self, username: str, fname: str, lname: str, authorized: int) -> int:
        self.con.execute(
            "INSERT INTO users (username, fname, lname, authorized, active, facility_id, "
            "see_auth, cal_ui) VALUES (?,?,?,?,1,3,1,1)",
            (username, fname, lname, authorized),
        )
        return self.user_id(username)

    def register(self, user: Optional[str], v: Mapping[str, Any]) -> int:
        """The New Patient path: patient_data plus its history_data row."""

        pid = int(self.one("SELECT COALESCE(MAX(pid), 0) + 1 FROM patient_data"))
        now = self._now()
        cols = {
            "pid": pid,
            "pubpid": str(pid),
            "date": now,
            "regdate": now[:10],
            "title": v["title"],
            "fname": v["fname"],
            "lname": v["lname"],
            "DOB": v["DOB"],
            "sex": v["sex"],
            "street": v["street"],
            "city": v["city"],
            "state": v["state"],
            "postal_code": v["postal_code"],
            "phone_home": v["phone_home"],
            "email": v["email"],
            "country_code": v["country_code"],
            "providerID": v.get("providerID"),
        }
        names = ",".join(f'"{k}"' for k in cols)
        cur = self.con.execute(
            f"INSERT INTO patient_data ({names}) VALUES ({','.join('?' * len(cols))})",
            tuple(cols.values()),
        )
        self.con.execute(
            "UPDATE patient_data SET uuid = ? WHERE id = ?",
            (self._uuid("patient_data", cur.lastrowid), cur.lastrowid),
        )
        self.log(
            user, "patient-record-insert", pid, f"INSERT INTO patient_data pid={pid}"
        )
        cur = self.con.execute(
            "INSERT INTO history_data (pid, date) VALUES (?, ?)", (pid, now)
        )
        self.con.execute(
            "UPDATE history_data SET uuid = ? WHERE id = ?",
            (self._uuid("history_data", cur.lastrowid), cur.lastrowid),
        )
        self.log(
            user, "patient-record-insert", pid, f"INSERT INTO history_data pid={pid}"
        )
        # The rest of what the New Patient path writes on the pinned app
        # (benchmark/openemr_local/fixture.py EXPECTED_TABLE_DELTAS["compiled"]).
        self.con.execute(
            "INSERT INTO contact (foreign_table_name, foreign_id) VALUES ('patient_data', ?)",
            (pid,),
        )
        self.log(user, "other-insert", 0, f"INSERT INTO contact foreign_id={pid}")
        uid = self.one("SELECT id FROM users WHERE username = ?", (user or "",))
        if uid is not None:
            self.con.execute("DELETE FROM recent_patients WHERE user_id = ?", (uid,))
            self.con.execute(
                "INSERT INTO recent_patients (user_id, patients) VALUES (?, ?)",
                (uid, f'[{{"pid":{pid}}}]'),
            )
            self.con.execute(
                "INSERT OR REPLACE INTO user_settings VALUES (?, 'dem_expand', '1')",
                (uid,),
            )
            self.con.execute(
                "INSERT OR REPLACE INTO user_settings VALUES (?, 'his_expand', '1')",
                (uid,),
            )
            self.con.execute(
                "INSERT INTO clinical_rules_log (date, pid, uid, category, value, new_value, "
                "facility_id) VALUES (?, ?, ?, 'clinical_reminder_widget', '', '', 3)",
                (now, pid, uid),
            )
        self.log(
            user, "patient-record-select", pid, f"SELECT * FROM patient_data pid={pid}"
        )
        return pid

    def update_chart(
        self, user: Optional[str], pid: int, changes: Mapping[str, Any]
    ) -> None:
        sets = ", ".join(f'"{k}" = ?' for k in changes)
        self.con.execute(
            f"UPDATE patient_data SET {sets} WHERE pid = ?", (*changes.values(), pid)
        )
        self.log(user, "patient-record-update", pid, f"UPDATE patient_data pid={pid}")

    def update_history(
        self, user: Optional[str], pid: int, changes: Mapping[str, Any]
    ) -> None:
        sets = ", ".join(f'"{k}" = ?' for k in changes)
        self.con.execute(
            f"UPDATE history_data SET {sets} WHERE pid = ?", (*changes.values(), pid)
        )
        self.log(user, "patient-record-update", pid, f"UPDATE history_data pid={pid}")

    def delete_history(self, user: Optional[str], pid: int) -> None:
        self.con.execute("DELETE FROM history_data WHERE pid = ?", (pid,))
        self.log(
            user, "patient-record-delete", pid, f"DELETE FROM history_data pid={pid}"
        )

    def add_note(self, user: Optional[str], pid: int, body: str) -> None:
        self.con.execute(
            "INSERT INTO pnotes (date, body, pid, user, groupname, activity, authorized, "
            "title, assigned_to, deleted, message_status) "
            "VALUES (?,?,?,?, 'Default', 1, 1, 'Unassigned', '', 0, 'New')",
            (self._now(), body, pid, user or "admin"),
        )
        self.log(user, "patient-record-insert", pid, f"INSERT INTO pnotes pid={pid}")

    def add_allergy(self, user: Optional[str], pid: int, title: str) -> None:
        self.con.execute(
            "INSERT INTO lists (date, type, title, activity, pid, user, groupname) "
            "VALUES (?, 'allergy', ?, 1, ?, ?, 'Default')",
            (self._now(), title, pid, user or "admin"),
        )
        self.log(user, "patient-record-insert", pid, f"INSERT INTO lists pid={pid}")

    def add_insurance(self, user: Optional[str], pid: int, provider: str) -> None:
        self.con.execute(
            "INSERT INTO insurance_data (type, provider, pid, date) VALUES "
            "('primary', ?, ?, ?)",
            (provider, pid, self._now()[:10]),
        )
        self.log(
            user, "patient-record-insert", pid, f"INSERT INTO insurance_data pid={pid}"
        )

    def add_appointment(
        self, user: Optional[str], pid: int, provider_id: int, category: str
    ) -> None:
        """One calendar appointment, as the calendar's add form writes it."""

        catid = 10  # 'New Patient' in the pinned fixture's categories
        self.con.execute(
            "INSERT INTO openemr_postcalendar_events (pc_catid, pc_aid, pc_pid, "
            "pc_title, pc_time, pc_eventDate, pc_endDate, pc_duration, pc_startTime, "
            "pc_endTime, pc_apptstatus, pc_facility, pc_billing_location) "
            "VALUES (?,?,?,?,?, '2026-10-26', '0000-00-00', 1800, '09:00:00', "
            "'09:30:00', '-', 3, 3)",
            (catid, str(provider_id), str(pid), category, self._now()),
        )
        self.log(
            user,
            "patient-record-insert",
            pid,
            f"INSERT INTO openemr_postcalendar_events pid={pid}",
        )

    def duplicate_history(self, user: Optional[str], pid: int) -> None:
        """A second history_data row for ``pid``, a copy of the first."""

        cols = [
            r[1]
            for r in self.con.execute("PRAGMA table_info(history_data)")
            if r[1] not in ("id", "uuid")
        ]
        names = ",".join(f'"{c}"' for c in cols)
        cur = self.con.execute(
            f"INSERT INTO history_data ({names}) SELECT {names} FROM history_data "
            "WHERE pid = ? ORDER BY id LIMIT 1",
            (pid,),
        )
        self.con.execute(
            "UPDATE history_data SET uuid = ? WHERE id = ?",
            (self._uuid("history_data", cur.lastrowid), cur.lastrowid),
        )
        self.log(
            user, "patient-record-insert", pid, f"INSERT INTO history_data pid={pid}"
        )

    def add_history(self, user: Optional[str], pid: int) -> None:
        """A history_data row for ``pid``, empty except pid and date."""

        cur = self.con.execute(
            "INSERT INTO history_data (pid, date) VALUES (?, ?)", (pid, self._now())
        )
        self.con.execute(
            "UPDATE history_data SET uuid = ? WHERE id = ?",
            (self._uuid("history_data", cur.lastrowid), cur.lastrowid),
        )
        self.log(
            user, "patient-record-insert", pid, f"INSERT INTO history_data pid={pid}"
        )

    def delete_insurance(self, user: Optional[str], pid: int) -> None:
        """Delete ``pid``'s primary insurance_data row."""

        self.con.execute(
            "DELETE FROM insurance_data WHERE pid = ? AND type = 'primary'", (pid,)
        )
        self.log(
            user, "patient-record-delete", pid, f"DELETE FROM insurance_data pid={pid}"
        )
