"""``frame.database.judged_by``: a table the contract's own read covers.

A contract that reads its main record's table in full (every row and every
column) judges the target there, with the fields the write may change. The
whole-database frame compares that table too, and leaves to the read only
the rows it saw move. A recheck reads no surface, so there the whole table is
compared: a late change to a look-alike row stops the write. Each test is one
case, on SQLite, through the public API. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.verifier import (
    Actor,
    Contract,
    VerifierError,
    begin_write,
    recheck,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")

SCHEMA = """
CREATE TABLE assets (id INTEGER PRIMARY KEY, tag TEXT, assigned_to INTEGER,
  notes TEXT DEFAULT '', updated_at TEXT);
INSERT INTO assets VALUES (1, 'LT-1', NULL, '', '2026-01-01'),
  (2, 'LT-1A', NULL, '', '2026-01-01');
"""
COLUMNS = (
    "CAST(id AS TEXT) AS id, tag, CAST(assigned_to AS TEXT) AS assigned_to, "
    "notes, updated_at"
)
PARAMS = {"tag": "LT-1", "user": "7"}
CHECKOUT = (
    "UPDATE assets SET assigned_to = 7, updated_at = '2026-10-31' WHERE tag = '{}'"
)


@pytest.fixture(autouse=True)
def _used_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setattr(verifier, "USED_DIR", tmp_path / "used")
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path


def _sql(db: Path, *statements: str) -> None:
    con = sqlite3.connect(db)
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


def _contract(db: Path, columns: str = COLUMNS, **database: Any) -> Contract:
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "surfaces",
                "surfaces": {
                    "record": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": f"SELECT {columns} FROM assets",
                    }
                },
            },
            "effects": [
                {
                    "kind": "record_written",
                    "probe": "surface=record|the asset is checked out",
                    "match": {
                        "tag": {"param": "tag"},
                        "assigned_to": {"param": "user"},
                    },
                    "expected_count": 1,
                }
            ],
            "frame": {
                "target_fields_by_surface": {"record": ["assigned_to", "updated_at"]},
                "database": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db)},
                    "judged_by": {"assets": "record"},
                    "settle_s": 0,
                    "recheck_after_s": 60,
                    **database,
                },
            },
        }
    )


def _write(contract: Contract, db: Path, *statements: str) -> Any:
    pending = begin_write(contract, PARAMS, actor=BOT)
    _sql(db, *statements)
    return verify_write(pending)


def test_a_correct_checkout_holds_and_its_recheck_confirms(tmp_path: Path) -> None:
    db = _db(tmp_path)
    contract = _contract(db)
    result = _write(contract, db, CHECKOUT.format("LT-1"))
    assert result.verified, result.reasons
    late = recheck(contract, result, params=PARAMS, actor=BOT)
    assert late.outcome == "CONFIRMED", late.reasons


def test_a_look_alike_checked_out_in_the_window_refutes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    contract = _contract(db)
    result = _write(contract, db, CHECKOUT.format("LT-1"), CHECKOUT.format("LT-1A"))
    assert not result.verified


def test_a_look_alike_checked_out_after_the_verdict_refutes_at_the_recheck(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    contract = _contract(db)
    result = _write(contract, db, CHECKOUT.format("LT-1"))
    assert result.verified, result.reasons
    _sql(db, CHECKOUT.format("LT-1A"))
    late = recheck(contract, result, params=PARAMS, actor=BOT)
    assert late.outcome == "REFUTED", late.reasons


def test_a_late_change_to_the_target_refutes_at_the_recheck(tmp_path: Path) -> None:
    db = _db(tmp_path)
    contract = _contract(db)
    result = _write(contract, db, CHECKOUT.format("LT-1"))
    assert result.verified, result.reasons
    _sql(db, "UPDATE assets SET notes = 'moved' WHERE tag = 'LT-1'")
    late = recheck(contract, result, params=PARAMS, actor=BOT)
    assert late.outcome == "REFUTED", late.reasons


def test_a_read_that_misses_a_column_leaves_the_table_to_the_frame(
    tmp_path: Path,
) -> None:
    """The read leaves out ``notes``: it can't judge the whole row, so the
    table judges every moved row itself. The target's change is declared
    only in the read, so the write stops (fail closed)."""

    db = _db(tmp_path)
    narrow = "CAST(id AS TEXT) AS id, tag, CAST(assigned_to AS TEXT) AS assigned_to, updated_at"
    contract = _contract(db, columns=narrow)
    result = _write(contract, db, CHECKOUT.format("LT-1"))
    assert not result.verified


def test_without_judged_by_the_table_judges_the_target_itself(tmp_path: Path) -> None:
    """The same contract without ``judged_by``: the whole-database frame
    sees the target change with nothing declaring it there, and stops."""

    db = _db(tmp_path)
    contract = _contract(db, judged_by={})
    result = _write(contract, db, CHECKOUT.format("LT-1"))
    assert not result.verified


def test_judged_by_must_name_a_surface(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(VerifierError, match="judged_by names no such surface"):
        _contract(db, judged_by={"assets": "nope"})


def test_judged_by_cant_also_be_allow_listed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(VerifierError, match="views or allow_tables also names"):
        _contract(db, allow_tables=["assets"])
