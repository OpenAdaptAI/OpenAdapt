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
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    # No ledger: some tests make the same write twice on purpose.
    pending = begin_write(contract, PARAMS, actor=BOT, ledger=None)
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


def test_a_read_that_misses_a_column_still_has_every_column_compared(
    tmp_path: Path,
) -> None:
    """The read leaves out ``notes``. It only says which rows are declared;
    the table compares every column of them, so a change to ``notes`` on the
    target stops the write and the correct checkout alone passes."""

    narrow = "CAST(id AS TEXT) AS id, tag, CAST(assigned_to AS TEXT) AS assigned_to, updated_at"
    db = _db(tmp_path / "a")
    assert _write(_contract(db, columns=narrow), db, CHECKOUT.format("LT-1")).verified
    db = _db(tmp_path / "b")
    stray = "UPDATE assets SET notes = 'moved' WHERE tag = 'LT-1'"
    result = _write(_contract(db, columns=narrow), db, CHECKOUT.format("LT-1"), stray)
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


# -- review round 1 (#1681): each attack, as a test ----------------------------


def _raw(db: Path, query: str, **frame: Any) -> dict[str, Any]:
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "surfaces",
            "surfaces": {
                "record": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": query,
                }
            },
        },
        "effects": [
            {
                "kind": "record_written",
                "probe": "surface=record|x",
                "match": {"tag": {"param": "tag"}, "assigned_to": {"param": "user"}},
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
            },
            **frame,
        },
    }


LOSSY = (
    "CAST(id AS TEXT) AS id, tag, CAST(assigned_to AS TEXT) AS assigned_to, "
    "substr(notes,1,3) AS notes, updated_at"
)


@pytest.mark.parametrize("tag", ["LT-1", "LT-1A"])
def test_b1_a_lossy_read_never_hides_a_change(tmp_path: Path, tag: str) -> None:
    """``substr`` reads 'abcdef' and 'abcXXX' alike. The table compares the
    stored value, on the target and on the look-alike."""

    db = _db(tmp_path)
    _sql(db, "UPDATE assets SET notes='abcdef' WHERE id IN (1,2)")
    stray = f"UPDATE assets SET notes='abcXXX' WHERE tag='{tag}'"
    result = _write(_contract(db, columns=LOSSY), db, CHECKOUT.format("LT-1"), stray)
    assert not result.verified


def test_b1_a_coalesced_read_never_hides_a_null(tmp_path: Path) -> None:
    db = _db(tmp_path)
    query = (
        "SELECT CAST(id AS TEXT) AS id, tag, CAST(assigned_to AS TEXT) AS assigned_to, "
        "COALESCE(notes,'') AS notes, updated_at FROM assets"
    )
    contract = Contract.from_dict(_raw(db, query))
    stray = "UPDATE assets SET notes=NULL WHERE tag='LT-1'"
    assert not _write(contract, db, CHECKOUT.format("LT-1"), stray).verified


def test_b1_a_surface_may_not_ignore_what_the_table_compares(tmp_path: Path) -> None:
    db = _db(tmp_path)
    raw = _raw(db, f"SELECT {COLUMNS} FROM assets", ignore_fields=["notes"])
    with pytest.raises(VerifierError, match="ignores notes, which the table doesn't"):
        Contract.from_dict(raw)
    raw["frame"]["database"]["ignore_columns"] = {"assets": ["notes"]}
    Contract.from_dict(raw)  # the table ignores it too: allowed


def test_sf2_a_composite_key_with_id_is_matched_by_both_columns(tmp_path: Path) -> None:
    """The generator reads ``id`` and ``rev`` as they are; a correct update
    passes, and a late change to the look-alike is refuted at the recheck."""

    db = tmp_path / "c.sqlite"
    _sql(
        db,
        "CREATE TABLE assets (id INTEGER, rev INTEGER, tag TEXT, assigned_to INTEGER, "
        "updated_at TEXT, PRIMARY KEY (id, rev))",
        "INSERT INTO assets VALUES (1,1,'LT-1',NULL,'x'),(2,1,'LT-1A',NULL,'x')",
    )
    query = (
        "SELECT CAST(t.id || '|' || t.rev AS TEXT) AS oa_key, CAST(t.id AS TEXT) AS id, "
        "CAST(t.rev AS TEXT) AS rev, t.tag AS tag, CAST(t.assigned_to AS TEXT) AS assigned_to, "
        "t.updated_at AS updated_at FROM assets t"
    )
    contract = Contract.from_dict(_raw(db, query))
    result = _write(contract, db, CHECKOUT.format("LT-1"))
    assert result.verified, result.reasons
    _sql(db, CHECKOUT.format("LT-1A"))
    assert recheck(contract, result, params=PARAMS, actor=BOT).outcome == "REFUTED"


def test_sf2_a_real_id_column_beside_another_key_is_compared(tmp_path: Path) -> None:
    db = tmp_path / "u.sqlite"
    _sql(
        db,
        "CREATE TABLE assets (uuid TEXT PRIMARY KEY, id INTEGER, tag TEXT, "
        "assigned_to INTEGER, updated_at TEXT)",
        "INSERT INTO assets VALUES ('a',1,'LT-1',NULL,'x'),('b',2,'LT-1A',NULL,'x')",
    )
    query = (
        "SELECT t.uuid AS oa_key, t.uuid AS uuid, CAST(t.id AS TEXT) AS id, t.tag AS tag, "
        "CAST(t.assigned_to AS TEXT) AS assigned_to, t.updated_at AS updated_at FROM assets t"
    )
    contract = Contract.from_dict(_raw(db, query))
    assert _write(contract, db, CHECKOUT.format("LT-1")).verified
    _sql(db, "UPDATE assets SET assigned_to=NULL, updated_at='x'")
    stray = "UPDATE assets SET id=99 WHERE tag='LT-1'"
    assert not _write(contract, db, CHECKOUT.format("LT-1"), stray).verified


def _create(db: Path, **database: Any) -> Contract:
    raw = _raw(db, f"SELECT {COLUMNS} FROM assets")
    raw["effects"][0]["match"] = {"tag": {"literal": "LT-9"}}
    raw["frame"]["database"].update(database)
    return Contract.from_dict(raw)


def test_sf3_a_declared_insert_may_set_only_declared_columns(tmp_path: Path) -> None:
    db = _db(tmp_path)
    contract = _create(db)
    pending = begin_write(contract, {}, actor=BOT, ledger=None)
    _sql(db, "INSERT INTO assets (id, tag) VALUES (3, 'LT-9')")
    assert verify_write(pending).verified
    _sql(db, "DELETE FROM assets WHERE id=3")
    pending = begin_write(contract, {}, actor=BOT, ledger=None)
    _sql(db, "INSERT INTO assets (id, tag, notes) VALUES (3, 'LT-9', 'evil')")
    assert not verify_write(pending).verified
    # Listed in new_row_fields, the column may be set.
    _sql(db, "DELETE FROM assets WHERE id=3")
    contract = _create(db, new_row_fields={"assets": ["notes"]})
    pending = begin_write(contract, {}, actor=BOT, ledger=None)
    _sql(db, "INSERT INTO assets (id, tag, notes) VALUES (3, 'LT-9', 'ok')")
    assert verify_write(pending).verified


def test_sf4_one_surface_cant_judge_two_tables(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(VerifierError, match="one surface for two tables"):
        _contract(db, judged_by={"assets": "record", "other": "record"})
