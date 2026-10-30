"""Review round 3 of null values (#1663): which params were null.

Ledgers once keyed ``None`` as ``""``, so a legacy-keyed attempt may be a
null write made before the upgrade or a ``""`` write. Each attempt now
records the names (never the values) of the params it wrote as null, when
it begins and when it resolves; an attempt begun before then is read from
its ``attempt.json``. A null write is handed back only an attempt with the
same null params, so a reconciled pre-upgrade null write is handed back
instead of held for good, and a landed ``""`` write is never handed to a
null write. Real SQLite store; records are made up.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.verifier import (
    Actor,
    AlreadyWritten,
    Contract,
    Ledger,
    Outcome,
    WriteHeld,
    begin_write,
    reconcile,
    verify_write,
)
from openadapt_flow.write_ledger import FileLedger

BOT = Actor(kind="agent", id="bot")
NULL = {"subject": "Tax", "description": None}
EMPTY = {"subject": "Tax", "description": ""}


@pytest.fixture(autouse=True)
def _dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def _setup(tmp_path: Path) -> tuple[Path, Contract, Ledger]:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE issue (id INTEGER PRIMARY KEY, subject TEXT, description TEXT);"
        "INSERT INTO issue VALUES (1,'Old','x');"
    )
    con.commit()
    con.close()
    contract = Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT id, subject, description FROM issue",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {
                        "subject": {"param": "subject"},
                        "description": {"param": "description"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                }
            ],
            "frame": {"identity_field": "id"},
        }
    )
    return db, contract, Ledger(FileLedger(tmp_path / "led"))


def _insert(db: Path, description: Any) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO issue (subject, description) VALUES ('Tax', ?)", (description,)
    )
    con.commit()
    con.close()


def _pre_upgrade_null_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Contract, Ledger, str]:
    """A null write made the way a pre-upgrade worker made it: keyed with
    None as "" and with no null_params on its begun event, read back
    UNCERTAIN. Its row reaches the store afterwards."""
    db, contract, ledger = _setup(tmp_path)
    key_for, begun = Ledger.key_for, Ledger.begun

    def old_key_for(
        self: Any, digest: str, values: Any, write_key: Any = None, **_: Any
    ) -> str:
        return key_for(self, digest, values, write_key, legacy_null=True)

    def old_begun(self: Any, key: str, attempt_id: str, **fields: Any) -> None:
        fields.pop("null_params", None)
        begun(self, key, attempt_id, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "key_for", old_key_for)
        patch.setattr(Ledger, "begun", old_begun)
        patch.setattr("openadapt_flow.verifier._hold_legacy_null", lambda *a: None)
        pending = begin_write(contract, NULL, actor=BOT, ledger=ledger)
        assert verify_write(pending).outcome is Outcome.UNCERTAIN
    _insert(db, None)
    legacy = ledger.key_for(contract.identity, NULL, None, legacy_null=True)
    attempt = ledger.latest(legacy)
    assert attempt is not None and "null_params" not in attempt.begun
    return db, contract, ledger, legacy


def test_a_reconciled_pre_upgrade_null_write_is_handed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db, contract, ledger, legacy = _pre_upgrade_null_write(tmp_path, monkeypatch)
    assert reconcile(contract, legacy, ledger=ledger) == "undelivered"
    resolved = ledger.latest(legacy).resolved  # type: ignore[union-attr]
    assert resolved is not None and resolved["null_params"] == ["description"]
    with pytest.raises(AlreadyWritten):
        begin_write(contract, NULL, actor=BOT, ledger=ledger)


def test_a_pre_upgrade_null_write_released_landed_is_handed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db, contract, ledger, legacy = _pre_upgrade_null_write(tmp_path, monkeypatch)
    with pytest.raises(WriteHeld):
        begin_write(contract, NULL, actor=BOT, ledger=ledger)
    ledger.release(legacy, by="ops", reason="row is there", found="landed")
    with pytest.raises(AlreadyWritten):
        begin_write(contract, NULL, actor=BOT, ledger=ledger)


def test_a_landed_empty_write_is_never_handed_to_a_null_write(tmp_path: Path) -> None:
    db, contract, ledger = _setup(tmp_path)
    pending = begin_write(contract, EMPTY, actor=BOT, ledger=ledger)
    assert verify_write(pending).outcome is Outcome.UNCERTAIN  # nothing yet
    _insert(db, "")
    key = ledger.key_for(contract.identity, EMPTY, None)
    ledger.release(key, by="ops", reason="the '' row is there", found="landed")
    resolved = ledger.latest(key).resolved  # type: ignore[union-attr]
    assert resolved is not None and resolved["null_params"] == []
    with pytest.raises(WriteHeld):
        begin_write(contract, NULL, actor=BOT, ledger=ledger)


def test_begun_records_null_param_names_only(tmp_path: Path) -> None:
    _db, contract, ledger = _setup(tmp_path)
    begin_write(contract, NULL, actor=BOT, ledger=ledger)
    attempt = ledger.latest(ledger.key_for(contract.identity, NULL))
    assert attempt is not None
    assert attempt.begun["null_params"] == ["description"]
    assert "Tax" not in json.dumps(attempt.begun)
