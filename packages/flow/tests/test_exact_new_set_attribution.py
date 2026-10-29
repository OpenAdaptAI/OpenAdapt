"""``exact_new_set`` sets aside a record the frame attributes to another user.

A colleague registering a different patient in the same window is not this
save's addition. Only an addition the audit log names another actor for is
set aside; an unattributed one still refutes. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run


def _db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE patient (id INTEGER PRIMARY KEY, pid INTEGER, lname TEXT, email TEXT)"
    )
    con.execute(
        "CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER, "
        "event TEXT, tbl TEXT)"
    )
    con.execute("INSERT INTO patient VALUES (1, 1, 'Old', 'old@example.test')")
    con.commit()
    return con


def _run(
    tmp_path: Path,
    colleague_logged: bool,
    *,
    colleague_event: str = "insert",
    bot_extra: bool = False,
    insert_events: bool = True,
    colleague_table: str = "patient",
    bot_name: str = "bot",
) -> str:
    tmp_path.mkdir(exist_ok=True)
    db = tmp_path / "s.db"
    con = _db(db)
    cfg = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM patient",
            },
            "record": {
                "name": "patient",
                "match": ["email"],
                "exact_new_set": True,
                "fields": {"email": "email", "lname": "lname"},
            },
            "frame": {
                "identity_field": "pid",
                "attribution": {
                    "kind": "audit_read",
                    "actor_field": "user",
                    "record_field": "patient_id",
                    "self": ["bot"],
                    "event_field": "event",
                    "write_events": ["insert", "update"],
                    "insert_events": ["insert"] if insert_events else [],
                    "table_field": "tbl",
                    "record_key": "pid",
                    "read": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": "SELECT * FROM log",
                    },
                },
                # The whole database, so an insert entry names its table.
                "database": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db)},
                    "allow_tables": ["log"],
                    "views": {"patient": ""},
                    "new_row_fields": {"patient": ["pid"]},
                },
            },
        }
    )
    params = {"email": "new@example.test", "lname": "New"}
    capture_baseline(cfg, params, tmp_path / "b.json")
    con.execute("INSERT INTO patient VALUES (2, 2, 'New', 'new@example.test')")
    con.execute(
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('bot', 2, 'insert', 'patient')"
    )
    # Patient 3: a colleague's new chart, or the bot's own extra record.
    con.execute("INSERT INTO patient VALUES (3, 3, 'Colleague', 'c@example.test')")
    if bot_extra:
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) "
            "VALUES (?, 3, 'insert', 'patient')",
            (bot_name,),
        )
    if colleague_logged:
        con.execute(
            "INSERT INTO log (user, patient_id, event, tbl) VALUES ('clinic2', 3, ?, ?)",
            (colleague_event, colleague_table),
        )
    con.commit()
    con.close()
    receipt = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="t",
        baseline_path=tmp_path / "b.json",
    )["receipt"]
    return receipt["outcome"]


def test_a_colleagues_new_patient_does_not_refute_the_save(tmp_path: Path) -> None:
    assert _run(tmp_path, colleague_logged=True) == "verified"


def test_an_unattributed_new_record_still_refutes(tmp_path: Path) -> None:
    assert _run(tmp_path, colleague_logged=False) == "reconciliation_required"


def test_only_an_insert_event_says_who_added_a_record(tmp_path: Path) -> None:
    """Adversarial review of #1571: a colleague's later edit of the bot's
    extra record, or a log without insert events, never sets it aside."""

    assert (
        _run(tmp_path / "a", colleague_logged=True, colleague_event="update")
        == "reconciliation_required"
    )
    assert (
        _run(tmp_path / "b", colleague_logged=True, insert_events=False)
        == "reconciliation_required"
    )
    assert (
        _run(tmp_path / "c", colleague_logged=True, bot_extra=True)
        == "reconciliation_required"
    )


def test_an_insert_into_another_table_or_by_the_bot_under_another_case_still_refutes(
    tmp_path: Path,
) -> None:
    """Re-review of #1571: a colleague's insert into another table, or the
    bot's own insert logged as "Bot", never sets the bot's record aside."""

    assert (
        _run(tmp_path / "a", colleague_logged=True, colleague_table="rx")
        == "reconciliation_required"
    )
    for i, name in enumerate(("Bot", "bot ")):
        assert (
            _run(
                tmp_path / f"case{i}",
                colleague_logged=True,
                bot_extra=True,
                bot_name=name,
            )
            == "reconciliation_required"
        )
