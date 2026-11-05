"""Native SQL origins cannot be inferred from matching result values."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openadapt_flow.runtime.effects.sql import SqlRecordVerifier, sqlite_read_connection


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "records.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO records VALUES (1, 'synthetic')")
    return path


def test_native_origin_belongs_to_the_executed_table(tmp_path: Path) -> None:
    path = _db(tmp_path)
    verifier = SqlRecordVerifier(
        lambda: sqlite_read_connection(str(path)),
        "SELECT * FROM main.\"records\" WHERE value != 'other'",
        source_metadata=True,
    )
    state = verifier.capture_pre_state()
    assert state.reachable and state.records == [{"id": 1, "value": "synthetic"}]
    assert verifier.source_relation == ("main", "records", 0)
    verifier.query = "SELECT * FROM records AS r"
    assert verifier.capture_pre_state().reachable
    assert verifier.source_relation is None
    verifier.query = "SELECT * FROM absent"
    assert not verifier.capture_pre_state().reachable
    assert verifier.source_relation is None


def test_an_existing_security_authorizer_is_never_replaced(tmp_path: Path) -> None:
    path = _db(tmp_path)

    def connect() -> sqlite3.Connection:
        con = sqlite3.connect(path)
        con.set_authorizer(
            lambda action, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                else sqlite3.SQLITE_OK
            )
        )
        return con

    verifier = SqlRecordVerifier(connect, "SELECT * FROM records", source_metadata=True)
    assert not verifier.capture_pre_state().reachable
    assert verifier.source_relation is None


def test_observed_connections_still_enforce_read_only(tmp_path: Path) -> None:
    path = _db(tmp_path)
    con = sqlite_read_connection(str(path))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            con.execute("INSERT INTO records VALUES (2, 'refused')")
    finally:
        con.close()
