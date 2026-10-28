"""The stable verifier API: capture, write, verify_write.

MCP skills, the RL reward and the registry call ``openadapt_flow.verifier``.
These tests pin its public signatures and check it end to end on a SQLite
store. Records are made up.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.verifier import (
    Actor,
    BaselineUnreadable,
    Contract,
    Snapshot,
    VerifierError,
    WriteResult,
    capture,
    capture_database,
    diff_database,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")

PARAMS = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE patient (id INTEGER PRIMARY KEY, fname TEXT, lname TEXT, "
        "email TEXT, phone TEXT);"
        "INSERT INTO patient VALUES (1, 'Wendell', 'Prior', 'w@example.test', '1');"
    )
    con.commit()
    con.close()
    return path


def _contract(db: Path) -> Contract:
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM patient",
            },
            "record": {
                "name": "patient",
                "match": ["email", "lname"],
                "fields": {"fname": "fname", "lname": "lname", "email": "email"},
            },
            "frame": {"identity_field": "id"},
        }
    )


def _write(db: Path, *statements: str) -> None:
    con = sqlite3.connect(db)
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


SAVE = (
    "INSERT INTO patient (fname, lname, email) "
    "VALUES ('Ada', 'Quill', 'ada@example.test')"
)


def _check(tmp_path: Path, *statements: str) -> WriteResult:
    db = _store(tmp_path)
    contract = _contract(db)
    before = capture(contract, PARAMS)
    _write(db, *statements)
    return verify_write(contract, before, params=PARAMS, actor=BOT)


def test_a_correct_write_is_verified(tmp_path: Path) -> None:
    result = _check(tmp_path, SAVE)
    assert result.verified and result.outcome == "verified"
    assert result.status == "verified"
    assert len(result.receipt_sha256) == 64
    assert result.checked["delta"] == "held" and result.checked["frame"] == "held"
    assert result.reasons == []
    assert result.receipt["outcome"] == "verified"


def test_a_wrong_value_is_not_verified(tmp_path: Path) -> None:
    wrong = SAVE.replace("'Ada', 'Quill'", "'Eda', 'Quill'")
    result = _check(tmp_path, wrong)
    assert not result.verified
    assert result.checked["delta"] != "held"
    assert result.reasons


def test_a_stray_write_is_not_verified(tmp_path: Path) -> None:
    result = _check(tmp_path, SAVE, "UPDATE patient SET phone = '2' WHERE id = 1")
    assert not result.verified and result.status == "refuted"
    assert result.checked["frame"] == "refuted"
    assert any("outside the declared write" in r for r in result.reasons)


def test_no_write_is_not_verified(tmp_path: Path) -> None:
    result = _check(tmp_path)
    assert not result.verified
    # The store doesn't show the write: it may still land, so reconcile.
    assert result.status == "unconfirmed"


def test_an_unreadable_store_stops_before_the_write(tmp_path: Path) -> None:
    contract = _contract(tmp_path / "missing" / "app.sqlite")
    with pytest.raises(BaselineUnreadable):
        capture(contract, PARAMS)


def test_an_unknown_actor_kind_is_refused(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(db)
    before = capture(contract, PARAMS)
    with pytest.raises(VerifierError):
        verify_write(contract, before, params=PARAMS, actor=Actor("robot", "x"))


def test_the_database_diff_names_what_moved_without_values(tmp_path: Path) -> None:
    db = _store(tmp_path)
    read = {"sqlite_database": str(db)}
    before = capture_database(read, "sqlite")
    _write(db, SAVE, "UPDATE patient SET phone = '2' WHERE id = 1")
    moved = diff_database(before, capture_database(read, "sqlite"))
    assert set(moved) == {"patient"}
    assert (moved["patient"].added, moved["patient"].changed) == (1, 1)
    assert moved["patient"].changed_columns == ["phone"]
    # Undone: back where it was.
    _write(db, "DELETE FROM patient WHERE id = 2", "UPDATE patient SET phone = '1'")
    assert diff_database(before, capture_database(read, "sqlite")) == {}


def test_a_snapshot_verifies_one_write(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(db)
    before = capture(contract, PARAMS)
    _write(db, SAVE)
    verify_write(contract, before, params=PARAMS, actor=BOT)
    with pytest.raises(VerifierError):
        verify_write(contract, before, params=PARAMS, actor=BOT)


def test_a_snapshot_crosses_a_process_boundary(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(db)
    path = capture(contract, PARAMS).save(tmp_path / "before.json")
    assert path.stat().st_mode & 0o777 == 0o600
    _write(db, SAVE)
    result = verify_write(contract, Snapshot.load(path), params=PARAMS, actor=BOT)
    assert result.verified


def test_a_bad_contract_raises(tmp_path: Path) -> None:
    with pytest.raises(VerifierError):
        Contract.from_dict({"schema_version": "openadapt.verify/v1"})


def test_the_public_api_is_stable() -> None:
    assert sorted(verifier.__all__) == [
        "Actor",
        "BaselineUnreadable",
        "Contract",
        "DatabaseSnapshot",
        "Snapshot",
        "Source",
        "TableDiff",
        "VerifierError",
        "WriteResult",
        "capture",
        "capture_database",
        "diff_database",
        "verify_write",
    ]

    def params(fn: Any) -> list[tuple[str, str]]:
        return [
            (name, p.kind.name) for name, p in inspect.signature(fn).parameters.items()
        ]

    assert params(capture) == [
        ("contract", "POSITIONAL_OR_KEYWORD"),
        ("params", "POSITIONAL_OR_KEYWORD"),
    ]
    assert params(verify_write) == [
        ("contract", "POSITIONAL_OR_KEYWORD"),
        ("before", "POSITIONAL_OR_KEYWORD"),
        ("params", "KEYWORD_ONLY"),
        ("actor", "KEYWORD_ONLY"),
        ("source", "KEYWORD_ONLY"),
        ("authorization_ref", "KEYWORD_ONLY"),
        ("write_attempted", "KEYWORD_ONLY"),
    ]
    assert [f for f in WriteResult.__dataclass_fields__] == [
        "verified",
        "status",
        "outcome",
        "checked",
        "reasons",
        "receipt",
        "receipt_sha256",
        "evidence",
    ]
