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
        "CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER)"
    )
    con.execute("INSERT INTO patient VALUES (1, 1, 'Old', 'old@example.test')")
    con.commit()
    return con


def _run(tmp_path: Path, colleague_logged: bool) -> str:
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
                    "read": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": "SELECT * FROM log",
                    },
                },
            },
        }
    )
    params = {"email": "new@example.test", "lname": "New"}
    capture_baseline(cfg, params, tmp_path / "b.json")
    con.execute("INSERT INTO patient VALUES (2, 2, 'New', 'new@example.test')")
    con.execute("INSERT INTO log (user, patient_id) VALUES ('bot', 2)")
    con.execute("INSERT INTO patient VALUES (3, 3, 'Colleague', 'c@example.test')")
    if colleague_logged:
        con.execute("INSERT INTO log (user, patient_id) VALUES ('clinic2', 3)")
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
