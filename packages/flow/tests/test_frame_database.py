"""The whole-database frame: every table and column, not a read-list.

A frame over the surfaces a contract reads passes any write outside them: a
column the effects don't name on the new record, a field on its side row, a
row in a table nobody reads. ``frame.database`` diffs the whole database
instead, with a reviewed allow-list for the app's own bookkeeping. Records
are made up; the database is SQLite, judged through ``verify_run``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from openadapt_flow.runtime.effects.db_snapshot import is_default, take_snapshot
from openadapt_flow.verify_run import (
    capture_baseline,
    parse_verify_config,
    verify_run,
)

SCHEMA = """
CREATE TABLE patient_data (id INTEGER PRIMARY KEY, pid INTEGER, fname TEXT,
  lname TEXT, email TEXT, phone_cell TEXT DEFAULT '', regdate TEXT);
CREATE TABLE history_data (id INTEGER PRIMARY KEY, pid INTEGER, date TEXT,
  tobacco TEXT DEFAULT '');
CREATE TABLE calendar (id INTEGER PRIMARY KEY, pid INTEGER, title TEXT);
CREATE TABLE codes (code TEXT);
CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER);
INSERT INTO patient_data VALUES (1, 1, 'Wendell', 'Prior', 'w@example.test', '', '2026-01-01');
INSERT INTO history_data VALUES (1, 1, '2026-01-01', '');
INSERT INTO codes VALUES ('A'), ('B'), ('C');
"""
NEW = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path


def _doc(db: Path, **database: Any) -> dict[str, Any]:
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(db),
            "sql_query": "SELECT * FROM patient_data",
        },
        "record": {
            "name": "patient",
            "match": ["email", "lname"],
            "fields": {"fname": "fname", "lname": "lname", "email": "email"},
        },
        "frame": {
            "identity_field": "id",
            "side_records": [
                {
                    "surface": "history_data",
                    "link_field": "pid",
                    "of_surface": "patient_data",
                    "of_field": "pid",
                }
            ],
            "database": {
                "dialect": "sqlite",
                "read": {"sqlite_database": str(db)},
                "allow_tables": ["log"],
                "views": {"patient_data": ""},
                "new_row_fields": {
                    "patient_data": ["pid", "regdate"],
                    "history_data": ["date"],
                },
                **database,
            },
        },
    }


def _save(con: sqlite3.Connection) -> None:
    """The app's own save: the chart, its history row, a log line."""

    con.execute(
        "INSERT INTO patient_data (id, pid, fname, lname, email, regdate) "
        "VALUES (2, 2, 'Ada', 'Quill', 'ada@example.test', '2026-10-25')"
    )
    con.execute("INSERT INTO history_data (pid, date) VALUES (2, '2026-10-25')")
    con.execute("INSERT INTO log (user, patient_id) VALUES ('bot', 2)")


def _outcome(
    tmp_path: Path,
    after: Callable[[sqlite3.Connection], None],
    **database: Any,
) -> dict[str, Any]:
    db = _db(tmp_path)
    cfg = parse_verify_config(_doc(db, **database))
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    con = sqlite3.connect(db)
    _save(con)
    after(con)
    con.commit()
    con.close()
    result = verify_run(
        cfg,
        params=NEW,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )
    return result


def _status(result: dict[str, Any]) -> str:
    return result["receipt"]["checked"]["frame"]


def test_a_clean_save_holds(tmp_path: Path) -> None:
    result = _outcome(tmp_path, lambda con: None)
    assert _status(result) == "held"
    assert result["receipt"]["outcome"] == "verified"


@pytest.mark.parametrize(
    "stray",
    [
        # A column the effects don't name, set on the new record.
        "UPDATE patient_data SET phone_cell = '617-555-0142' WHERE pid = 2",
        # A field on the new record's side row.
        "UPDATE history_data SET tobacco = 'Never' WHERE pid = 2",
        # A row in a table no surface reads.
        "INSERT INTO calendar (pid, title) VALUES (2, 'New Patient')",
        # A second side row.
        "INSERT INTO history_data (pid, date) VALUES (2, '2026-10-25')",
        # A table fingerprinted rather than read row by row.
        "INSERT INTO codes VALUES ('D')",
    ],
)
def test_any_write_outside_the_declared_delta_refutes(
    tmp_path: Path, stray: str
) -> None:
    result = _outcome(tmp_path, lambda con: con.execute(stray), max_rows=2)
    assert _status(result) == "refuted", stray
    assert result["receipt"]["outcome"] != "verified"


def test_the_allow_list_is_the_only_exemption(tmp_path: Path) -> None:
    # Bookkeeping the app writes on its own: allowed by name.
    more_log = "INSERT INTO log (user, patient_id) VALUES ('bot', 2)"
    assert _status(_outcome(tmp_path, lambda con: con.execute(more_log))) == "held"


def test_an_unreadable_database_is_not_checked(tmp_path: Path) -> None:
    db = _db(tmp_path)
    doc = _doc(db)
    doc["frame"]["database"]["read"] = {"sqlite_database": str(tmp_path / "gone")}
    cfg = parse_verify_config(doc)
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    result = verify_run(
        cfg,
        params=NEW,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )
    assert _status(result) == "not_checked"
    assert result["receipt"]["outcome"] != "verified"


def test_the_snapshot_reads_every_table_in_three_statements(tmp_path: Path) -> None:
    db = _db(tmp_path)
    statements: list[str] = []

    class Spy:
        def __init__(self) -> None:
            self.con = sqlite3.connect(db)

        def cursor(self) -> Any:
            outer = self

            class Cur:
                def __init__(self) -> None:
                    self.cur = outer.con.cursor()

                def execute(self, sql: str) -> None:
                    statements.append(sql)
                    self.cur.execute(sql)

                def fetchall(self) -> list:
                    return self.cur.fetchall()

            return Cur()

        def close(self) -> None:
            self.con.close()

    snap = take_snapshot(Spy, "sqlite", skip=["log"])
    assert set(snap.tables) == {"patient_data", "history_data", "calendar", "codes"}
    assert snap.tables["calendar"].rows == []
    reads = [s for s in statements if not s.startswith(("PRAGMA", "SELECT name"))]
    assert len(reads) == 2  # counts, then every row


def test_default_values_are_unset() -> None:
    assert is_default("", None)
    assert is_default("0000-00-00", None)
    assert is_default("NO", "'NO'")
    assert not is_default("617-555-0142", "''")
    # Set by the database at insert: any value is its default.
    assert is_default("2026-10-25 11:00:00", "CURRENT_TIMESTAMP")
    assert is_default("2026-10-25 11:00:00", "current_timestamp()")


def test_a_table_wider_than_one_json_call_is_read(tmp_path: Path) -> None:
    # SQLite caps a function at 127 arguments; OpenEMR's chart has more
    # columns than that.
    cols = ", ".join(f"c{i} TEXT" for i in range(150))
    path = tmp_path / "wide.sqlite"
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE wide (id INTEGER PRIMARY KEY, {cols})")
    con.execute("INSERT INTO wide (id, c149) VALUES (1, 'last')")
    con.commit()
    con.close()
    snap = take_snapshot(lambda: sqlite3.connect(path), "sqlite")
    (row,) = snap.tables["wide"].rows or []
    assert row["c149"] == "last" and len(row) == 151
