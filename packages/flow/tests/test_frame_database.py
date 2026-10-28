"""The whole-database frame: every table and column, not a read-list.

A frame over the surfaces a contract reads passes any write outside them.
``frame.database`` diffs the whole database instead, with a reviewed
allow-list for the app's own bookkeeping. Each test below is one way a write
could slip past (most from the adversarial review of #1582), judged through
``verify_run`` on SQLite. Records are made up.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from openadapt_flow.runtime.effects.db_snapshot import (
    CLOCK_DEFAULT,
    is_default,
    take_snapshot,
)
from openadapt_flow.verify_run import (
    capture_baseline,
    parse_verify_config,
    verify_run,
)

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE patient_data (id INTEGER PRIMARY KEY, pid INTEGER, fname TEXT,
  lname TEXT, email TEXT, phone_cell TEXT DEFAULT '', regdate TEXT,
  active INTEGER DEFAULT 1, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE history_data (id INTEGER PRIMARY KEY, pid INTEGER, date TEXT,
  tobacco TEXT DEFAULT '');
CREATE TABLE calendar (id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, title TEXT);
CREATE TABLE access (pid INTEGER, user TEXT, role TEXT DEFAULT '',
  PRIMARY KEY (pid, user));
CREATE TABLE codes (id INTEGER PRIMARY KEY, code TEXT);
CREATE TABLE contact (id INTEGER PRIMARY KEY, foreign_id INTEGER);
CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER,
  event TEXT, tbl TEXT);
INSERT INTO patient_data (id, pid, fname, lname, email, regdate)
  VALUES (1, 1, 'Wendell', 'Prior', 'w@example.test', '2026-01-01');
INSERT INTO history_data VALUES (1, 1, '2026-01-01', '');
INSERT INTO codes (code) VALUES ('A'), ('B'), ('C');
"""
NEW = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}
ATTRIBUTION = {
    "kind": "audit_read",
    "actor_field": "user",
    "self": ["bot"],
    "record_field": "patient_id",
    "record_key": "pid",
    "event_field": "event",
    "write_events": ["insert", "update", "delete"],
    "insert_events": ["insert"],
    "table_field": "tbl",
}


def _db(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path


def _doc(db: Path, frame: Optional[dict] = None, **database: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "identity_field": "id",
        "side_records": [
            {
                "surface": "history_data",
                "link_field": "pid",
                "of_surface": "patient_data",
                "of_field": "pid",
            }
        ],
        "attribution": {
            **ATTRIBUTION,
            "read": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM log",
            },
        },
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
    }
    spec.update(frame or {})
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
        "frame": spec,
    }


def _save(con: sqlite3.Connection, cols: str = "", vals: str = "") -> None:
    """The app's own save: the chart, its history row, the bot's log lines."""

    con.execute(
        "INSERT INTO patient_data (id, pid, fname, lname, email, regdate"
        + cols
        + ") VALUES (2, 2, 'Ada', 'Quill', 'ada@example.test', '2026-10-25'"
        + vals
        + ")"
    )
    con.execute("INSERT INTO history_data (pid, date) VALUES (2, '2026-10-25')")
    con.execute(
        "INSERT INTO log (user, patient_id, event, tbl) VALUES "
        "('bot', 2, 'insert', 'patient_data'), ('bot', 2, 'insert', 'history_data')"
    )


def _run(
    tmp_path: Path,
    after: Callable[[sqlite3.Connection], Any] = lambda con: None,
    save: Callable[[sqlite3.Connection], None] = _save,
    frame: Optional[dict] = None,
    **database: Any,
) -> dict[str, Any]:
    db = _db(tmp_path)
    cfg = parse_verify_config(_doc(db, frame, **database))
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    con = sqlite3.connect(db)
    save(con)
    after(con)
    con.commit()
    con.close()
    return verify_run(
        cfg,
        params=NEW,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )


def _frame(result: dict[str, Any]) -> str:
    return result["receipt"]["checked"]["frame"]


def test_a_clean_save_holds(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert _frame(result) == "held"
    assert result["receipt"]["outcome"] == "verified"


@pytest.mark.parametrize(
    "stray",
    [
        "UPDATE patient_data SET phone_cell = '617-555-0142' WHERE pid = 2",
        "UPDATE history_data SET tobacco = 'Never' WHERE pid = 2",
        "INSERT INTO calendar (pid, title) VALUES (2, 'New Patient')",
        "INSERT INTO history_data (pid, date) VALUES (2, '2026-10-25')",
        "UPDATE patient_data SET email = 'x@example.test' WHERE pid = 1",
        "DELETE FROM codes WHERE code = 'A'",
    ],
)
def test_any_write_outside_the_declared_delta_refutes(
    tmp_path: Path, stray: str
) -> None:
    result = _run(tmp_path, lambda con: con.execute(stray))
    assert _frame(result) == "refuted", stray
    assert result["receipt"]["outcome"] != "verified"


# -- the review's silent passes, one test each ------------------------------------


def test_1_a_value_counts_as_default_only_if_it_is_the_declared_one(
    tmp_path: Path,
) -> None:
    # active defaults to 1; a new chart saved inactive is a write.
    result = _run(tmp_path, save=lambda con: _save(con, ", active", ", 0"))
    assert _frame(result) == "refuted"


def test_2_a_clock_default_must_fall_in_the_window(tmp_path: Path) -> None:
    backdated = ", '2001-01-01 00:00:00'"
    result = _run(tmp_path, save=lambda con: _save(con, ", created", backdated))
    assert _frame(result) == "refuted"


def test_3_ignore_fields_apply_to_surfaces_not_tables(tmp_path: Path) -> None:
    other = "UPDATE patient_data SET regdate = '1999-01-01' WHERE pid = 1"
    result = _run(
        tmp_path, lambda con: con.execute(other), frame={"ignore_fields": ["regdate"]}
    )
    assert _frame(result) == "refuted"


def test_4_a_row_written_and_removed_refutes(tmp_path: Path) -> None:
    def write_then_delete(con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO calendar (pid, title) VALUES (1, 'x')")
        con.execute("DELETE FROM calendar")

    assert _frame(_run(tmp_path, write_then_delete)) == "refuted"


def test_5_a_trigger_created_in_the_window_refutes(tmp_path: Path) -> None:
    trigger = (
        "CREATE TRIGGER t AFTER INSERT ON history_data BEGIN "
        "UPDATE patient_data SET email = 'x@example.test'; END"
    )
    assert _frame(_run(tmp_path, lambda con: con.execute(trigger))) == "refuted"


def test_6_only_a_generated_key_may_hold_any_value(tmp_path: Path) -> None:
    side = [
        {
            "surface": "history_data",
            "link_field": "pid",
            "of_surface": "patient_data",
            "of_field": "pid",
        },
        {
            "surface": "access",
            "link_field": "pid",
            "of_surface": "patient_data",
            "of_field": "pid",
        },
    ]
    outside = "INSERT INTO access (pid, user) VALUES (2, 'outside_user')"
    result = _run(
        tmp_path, lambda con: con.execute(outside), frame={"side_records": side}
    )
    assert _frame(result) == "refuted"


def test_7_a_view_entry_never_excuses_a_write(tmp_path: Path) -> None:
    def viewed_then_written(con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) "
            "VALUES ('clinic2', 1, 'view', 'history_data')"
        )
        con.execute("UPDATE history_data SET tobacco = 'Never' WHERE pid = 1")

    assert _frame(_run(tmp_path, viewed_then_written)) == "refuted"


def test_7_a_write_entry_for_another_table_never_excuses_a_write(
    tmp_path: Path,
) -> None:
    def other_table(con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) "
            "VALUES ('clinic2', 1, 'update', 'patient_data')"
        )
        con.execute("UPDATE history_data SET tobacco = 'Never' WHERE pid = 1")

    assert _frame(_run(tmp_path, other_table)) == "refuted"


def test_another_users_write_to_another_chart_is_concurrent(tmp_path: Path) -> None:
    def colleague(con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) "
            "VALUES ('clinic2', 1, 'update', 'history_data')"
        )
        con.execute("UPDATE history_data SET tobacco = 'Never' WHERE pid = 1")

    assert _frame(_run(tmp_path, colleague)) == "held"


def test_another_users_later_write_to_the_new_chart_is_theirs(tmp_path: Path) -> None:
    def colleague(con: sqlite3.Connection) -> None:
        con.execute("UPDATE patient_data SET phone_cell = '617-555-0199' WHERE pid = 2")
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) "
            "VALUES ('clinic2', 2, 'update', 'patient_data')"
        )

    assert _frame(_run(tmp_path, colleague)) == "held"


def test_the_bots_own_later_write_to_the_new_chart_still_fails(tmp_path: Path) -> None:
    def both(con: sqlite3.Connection) -> None:
        con.execute("UPDATE patient_data SET phone_cell = '617-555-0199' WHERE pid = 2")
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) VALUES "
            "('clinic2', 2, 'update', 'patient_data'), "
            "('bot', 2, 'update', 'patient_data')"
        )

    assert _frame(_run(tmp_path, both)) == "refuted"


# -- append-only bookkeeping, size, consistency, PHI -------------------------------


def test_the_audit_log_is_append_only(tmp_path: Path) -> None:
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO log (user, patient_id, event, tbl) VALUES ('x', 1, 'view', 'p')"
    )
    con.commit()
    con.close()
    cfg = parse_verify_config(_doc(db))
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    con = sqlite3.connect(db)
    _save(con)
    con.execute("DELETE FROM log WHERE user = 'x'")
    con.commit()
    con.close()
    result = verify_run(
        cfg,
        params=NEW,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )
    assert _frame(result) == "refuted"


def test_skipping_a_table_needs_a_reason(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(Exception, match="reason"):
        parse_verify_config(_doc(db, allow_tables=[{"table": "log", "mode": "skip"}]))
    cfg = parse_verify_config(
        _doc(db, allow_tables=[{"table": "log", "mode": "skip", "reason": "test"}])
    )
    assert cfg.frame is not None and cfg.frame.database is not None


def test_a_large_table_holds_a_clean_save_and_catches_a_stray_one(
    tmp_path: Path,
) -> None:
    # Over max_rows, existing rows are compared by key bucket and the rows
    # the save adds are read in full.
    assert _frame(_run(tmp_path / "clean", max_rows=1)) == "held"
    stray = "UPDATE patient_data SET email = 'x@example.test' WHERE pid = 1"
    result = _run(tmp_path / "stray", lambda con: con.execute(stray), max_rows=1)
    assert _frame(result) == "refuted"
    cell = "UPDATE patient_data SET phone_cell = '1' WHERE pid = 2"
    result = _run(tmp_path / "cell", lambda con: con.execute(cell), max_rows=1)
    assert _frame(result) == "refuted"


def test_the_snapshot_is_one_consistent_read(tmp_path: Path) -> None:
    db = _db(tmp_path)
    opened: list[int] = []

    class Interrupted:
        """A connection whose reads let another writer commit midway."""

        def __init__(self) -> None:
            opened.append(1)
            self.con = sqlite3.connect(db)
            self.reads = 0

        def cursor(self) -> Any:
            outer = self

            class Cur:
                description: Any = None

                def __init__(self) -> None:
                    self.cur = outer.con.cursor()

                def execute(self, sql: str) -> None:
                    outer.reads += 1
                    if outer.reads == 5:
                        w = sqlite3.connect(db)
                        w.execute("INSERT INTO calendar (pid, title) VALUES (1, 'x')")
                        w.commit()
                        w.close()
                    self.cur.execute(sql)
                    self.description = self.cur.description

                def fetchall(self) -> list:
                    return self.cur.fetchall()

            return Cur()

        def close(self) -> None:
            self.con.close()

    snap = take_snapshot(Interrupted, "sqlite", key=b"k")
    assert len(opened) == 1
    calendar = snap.tables["calendar"]
    assert calendar.count == len(calendar.rows or {})


def test_the_baseline_holds_no_values(tmp_path: Path) -> None:
    _run(tmp_path)
    detail = json.loads((tmp_path / "baseline.json").read_text())["state"]["detail"]
    snapshot = json.dumps(detail["openadapt:frame_database"])
    assert "Wendell" not in snapshot and "w@example.test" not in snapshot
    assert "clinic2" not in json.dumps(detail["openadapt:frame_audit"])


def test_the_receipt_counts_tables_compared_and_allowed(tmp_path: Path) -> None:
    frame = _run(tmp_path)["evidence"]["frame"]
    counts = frame["database"]
    assert counts["tables_compared"] == 6 and counts["tables_append_only"] == 1
    assert counts["tables_skipped"] == 0
    assert counts["allow_list_sha256"].startswith("sha256:")


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
    assert _frame(result) == "not_checked"
    assert result["receipt"]["outcome"] != "verified"


def test_frame_database_needs_write_attribution(tmp_path: Path) -> None:
    doc = _doc(_db(tmp_path))
    for key in ("event_field", "write_events", "insert_events", "table_field"):
        doc["frame"]["attribution"].pop(key)
    with pytest.raises(Exception, match="write"):
        parse_verify_config(doc)


def test_a_table_wider_than_one_json_call_is_read(tmp_path: Path) -> None:
    cols = ", ".join(f"c{i} TEXT" for i in range(150))
    path = tmp_path / "wide.sqlite"
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE wide (id INTEGER PRIMARY KEY, {cols})")
    con.execute("INSERT INTO wide (id, c149) VALUES (1, 'last')")
    con.commit()
    con.close()
    snap = take_snapshot(lambda: sqlite3.connect(path), "sqlite", key=b"k")
    (row,) = snap.tables["wide"].values.values()
    assert row["c149"] == "last" and len(row) == 151
    assert "last" not in json.dumps(snap.to_json())


def test_declared_defaults() -> None:
    assert is_default(None, None)
    assert not is_default("0", "1") and is_default("1", "1") and is_default(1, "1")
    assert not is_default("", None)
    assert is_default("NO", "NO")
    assert is_default(
        "2026-10-25 11:00:00",
        CLOCK_DEFAULT,
        ("2026-10-25 10:59:59", "2026-10-25 11:00:05"),
    )
    assert not is_default(
        "2001-01-01 00:00:00",
        CLOCK_DEFAULT,
        ("2026-10-25 10:59:59", "2026-10-25 11:00:05"),
    )


def test_a_null_default_is_no_default(tmp_path: Path) -> None:
    from openadapt_flow.runtime.effects.db_snapshot import _default_text

    assert _default_text("NULL", "sqlite") is None
    assert _default_text("NULL", "mysql") is None
    assert _default_text("'NULL'", "sqlite") == "NULL"  # the text NULL


def test_a_side_row_of_another_users_new_chart_is_theirs(tmp_path: Path) -> None:
    contact = {
        "surface": "contact",
        "link_field": "foreign_id",
        "of_surface": "patient_data",
        "of_field": "pid",
    }
    history = {
        "surface": "history_data",
        "link_field": "pid",
        "of_surface": "patient_data",
        "of_field": "pid",
    }

    def save(con: sqlite3.Connection) -> None:
        _save(con)
        con.execute("INSERT INTO contact (foreign_id) VALUES (2)")

    def colleague(con: sqlite3.Connection) -> None:
        # Another user's new chart: logged per table, but its contact row is
        # logged with no patient (as OpenEMR does).
        con.execute(
            "INSERT INTO patient_data (id, pid, fname, lname, email, regdate) "
            "VALUES (3, 3, 'Theo', 'Marsh', 't@example.test', '2026-10-25')"
        )
        con.execute("INSERT INTO history_data (pid, date) VALUES (3, '2026-10-25')")
        con.execute("INSERT INTO contact (foreign_id) VALUES (3)")
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) VALUES "
            "('clinic2', 3, 'insert', 'patient_data'), "
            "('clinic2', 3, 'insert', 'history_data'), "
            "('clinic2', 0, 'insert', 'contact')"
        )

    frame = {"side_records": [history, contact]}
    assert _frame(_run(tmp_path / "a", colleague, save=save, frame=frame)) == "held"
    # A contact row for a chart nobody created in the window still refutes.
    stray = "INSERT INTO contact (foreign_id) VALUES (1)"
    result = _run(
        tmp_path / "b", lambda con: con.execute(stray), save=save, frame=frame
    )
    assert _frame(result) == "refuted"


def test_a_write_between_the_two_reads_leaves_the_frame_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app writes again a moment after the save (a background job): the
    # two reads after the save differ, so nothing is claimed yet.
    import openadapt_flow.verify_run as vr

    db = tmp_path / "app.sqlite"

    def late_write(seconds: float) -> None:
        con = sqlite3.connect(db)
        con.execute("INSERT INTO calendar (pid, title) VALUES (2, 'late')")
        con.commit()
        con.close()

    monkeypatch.setattr(vr.time, "sleep", late_write)
    result = _run(tmp_path)
    assert _frame(result) == "not_checked"
    assert result["receipt"]["outcome"] != "verified"
