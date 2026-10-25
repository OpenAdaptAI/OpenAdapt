"""``record.surface``: a record contract read from one of several surfaces.

The record shorthand (what ``qualify`` and ``verify-run`` read) can now name
the surface its record comes from, so a contract can also read other tables
for its frame: a stray write there refutes. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run


def _cfg(db: Path, surface: str | None = "patient") -> dict:
    record = {
        "name": "patient",
        "match": ["email"],
        "fields": {"email": "email", "lname": "lname"},
    }
    if surface:
        record["surface"] = surface
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "surfaces",
            "surfaces": {
                "patient": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT * FROM patient",
                },
                "notes": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT * FROM notes",
                },
            },
        },
        "record": record,
    }


def _run(tmp_path: Path, stray_note: bool) -> str:
    db = tmp_path / "s.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE patient (id INTEGER PRIMARY KEY, lname TEXT, email TEXT)")
    con.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, patient_id INTEGER, body TEXT)"
    )
    con.execute("INSERT INTO patient VALUES (1, 'Old', 'old@example.test')")
    con.commit()
    cfg = parse_verify_config(_cfg(db))
    params = {"email": "new@example.test", "lname": "New"}
    capture_baseline(cfg, params, tmp_path / "b.json")
    con.execute("INSERT INTO patient VALUES (2, 'New', 'new@example.test')")
    if stray_note:
        con.execute("INSERT INTO notes VALUES (1, 1, 'stray')")
    con.commit()
    con.close()
    return verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="t",
        baseline_path=tmp_path / "b.json",
    )["receipt"]["outcome"]


def test_the_record_is_read_from_its_surface(tmp_path: Path) -> None:
    assert _run(tmp_path, stray_note=False) == "verified"


def test_a_stray_write_in_another_surface_refutes(tmp_path: Path) -> None:
    assert _run(tmp_path, stray_note=True) == "reconciliation_required"


def test_an_unset_surface_keeps_the_contract_digest(tmp_path: Path) -> None:
    from openadapt_flow.qualify.campaign import record_effects  # noqa: F401
    from openadapt_flow.verify_run import parse_verify_config as parse

    single = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "sql", "sqlite_database": "x.db", "sql_query": "SELECT 1"},
        "record": {"name": "p", "match": ["email"], "fields": {"email": "email"}},
    }
    effects = parse(single).effects
    assert all("surface:" not in (e.probe or "") for e in effects)
