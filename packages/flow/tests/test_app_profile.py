"""One per-app profile of the app's own bookkeeping, shared by the frame,
rollback and the RL grader.

A rule is append-only, column-scoped or per-session state; none may excuse
patient data. Records are made up; the database is SQLite with OpenEMR-like
table names.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from openadapt_flow.app_profiles import AppProfile, load_profile
from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run

SCHEMA = """
CREATE TABLE patient_data (id INTEGER PRIMARY KEY, pid INTEGER, fname TEXT,
  lname TEXT, email TEXT, uuid TEXT);
CREATE TABLE history_data (id INTEGER PRIMARY KEY, pid INTEGER, tobacco TEXT DEFAULT '',
  uuid TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, uuid TEXT,
  last_updated TEXT);
CREATE TABLE session_tracker (uuid TEXT PRIMARY KEY, number_scripts INTEGER);
CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER,
  event TEXT, tbl TEXT);
INSERT INTO patient_data VALUES (1, 1, 'Wendell', 'Prior', 'w@example.test', 'u-1');
INSERT INTO history_data VALUES (1, 1, '', NULL);
INSERT INTO users VALUES (1, 'clinic2', NULL, '2026-01-01 00:00:00');
INSERT INTO session_tracker VALUES ('s1', 1);
"""
NEW = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}


def _run(tmp_path: Path, after: Callable[[sqlite3.Connection], Any]) -> dict:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    cfg = parse_verify_config(
        {
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
                "database": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db)},
                    "profile": "openemr",
                    "views": {"patient_data": ""},
                    "new_row_fields": {"patient_data": ["pid", "uuid"]},
                    "settle_s": 0,
                },
            },
        }
    )
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO patient_data (pid, fname, lname, email, uuid) "
        "VALUES (2, 'Ada', 'Quill', 'ada@example.test', 'u-2')"
    )
    con.execute(
        "INSERT INTO log (user, patient_id, event, tbl) VALUES ('bot', 2, 'x', 'p')"
    )
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


def _frame(result: dict) -> str:
    return result["receipt"]["checked"]["frame"]


def test_a_rule_that_would_excuse_patient_data_is_refused() -> None:
    base = load_profile("openemr").model_dump(mode="json")
    for rule in (
        {"id": "x", "table": "history_data", "mode": "session", "reason": "no"},
        {
            "id": "x",
            "table": "patient_data",
            "mode": "columns",
            "columns": ["fname"],
            "reason": "no",
        },
        {
            "id": "x",
            "table": "*",
            "mode": "columns",
            "columns": ["email"],
            "reason": "no",
        },
    ):
        with pytest.raises(ValueError, match="patient data"):
            AppProfile.model_validate({**base, "rules": [*base["rules"], rule]})


def test_bookkeeping_holds_and_the_receipt_names_the_rules(tmp_path: Path) -> None:
    def bookkeeping(con: sqlite3.Connection) -> None:
        con.execute("UPDATE history_data SET uuid = 'h-1' WHERE id = 1")  # backfill
        con.execute(
            "UPDATE users SET uuid = 'x', last_updated = '2026-10-28' WHERE id = 1"
        )
        con.execute("INSERT INTO session_tracker VALUES ('s2', 1)")

    result = _run(tmp_path, bookkeeping)
    assert _frame(result) == "held"
    profile = result["evidence"]["frame"]["database"]["profile"]
    assert profile["version"] == "openemr-frame-profile/2026-10-28.2"
    assert {"uuid-backfill", "users-uuid-backfill", "log", "session-tracker"} <= set(
        profile["rules_used"]
    )


def test_a_patient_column_beside_a_backfill_still_refutes(tmp_path: Path) -> None:
    def both(con: sqlite3.Connection) -> None:
        con.execute(
            "UPDATE history_data SET uuid = 'h-1', tobacco = 'Never' WHERE id = 1"
        )

    assert _frame(_run(tmp_path, both)) == "refuted"


def test_a_column_rule_never_excuses_an_added_row(tmp_path: Path) -> None:
    def extra(con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO users (username, uuid) VALUES ('new', 'x')")

    assert _frame(_run(tmp_path, extra)) == "refuted"
