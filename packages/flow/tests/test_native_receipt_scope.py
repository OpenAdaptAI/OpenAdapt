"""Declared SQL projection and database proof through the shared native producer.

These are synthetic SQLite API/MCP controls. They do not qualify an application,
undeclared schema, an identifying search or intermediate record history.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.agent.verify_tools import VerifyBridge
from openadapt_flow.verifier import (
    Contract,
    Outcome,
    begin_write,
    recheck,
    verify_write,
)
from tests import test_verifier_api as api_cases
from tests import test_verifier_recheck as late_cases
from tests import test_verify_tools as mcp_cases
from tests.test_verify_tools import sqlite_bridge as sqlite_bridge


@pytest.fixture(autouse=True)
def isolated_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = tmp_path / "issued"
    monkeypatch.setattr(verifier, "USED_DIR", authority)
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(authority))
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def assert_undeclared_search(hard: dict[str, Any]) -> None:
    checks = {part["name"]: part for part in hard["checks"]}
    assert hard["status"] == "held"
    assert checks["declared_scope"]["status"] == "held"
    assert checks["no_undeclared_deletes"]["status"] == "held"
    assert checks["at_most_one"]["status"] == "not_checked"
    assert checks["at_most_one"]["required"] is False
    assert checks["at_most_one"]["reason"] == "identifying_search_not_declared"
    outside = {part["name"]: part for part in hard["outside_guarantee"]}
    assert (
        outside["identifying_search_duplicates_and_side_records"]["status"] == "UNKNOWN"
    )
    assert outside["receipt_record_intermediate_history"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("identity", [True, False])
def test_complete_declared_projection_needs_no_schema_or_global_snapshot(
    tmp_path: Path, identity: bool
) -> None:
    db = api_cases._store(tmp_path)
    doc = api_cases._doc(db)
    doc["verifier"]["sql_query"] = "SELECT id, fname, lname, email FROM patient"
    if not identity:
        doc.pop("frame")
    contract = Contract.from_dict(doc)
    pending = begin_write(contract, api_cases.PARAMS, actor=api_cases.BOT)
    api_cases._write(db, api_cases.SAVE)
    result = verify_write(pending)
    assert result.outcome is Outcome.CONFIRMED and result.verified
    hard = result.evidence["frame"]["hard_invariants"]
    assert_undeclared_search(hard)
    domain = hard["guarantee"]
    assert domain["scope"] == "declared_projection"
    assert domain["schema_sha256"] is None
    assert domain["identity_binding"] == (
        "declared_identity" if identity else "projected_tuple"
    )
    assert ("target_id_macs" in domain) is identity
    assert ("projection_tuple_macs" in domain) is not identity
    assert any(
        "identifying_search_duplicates_and_side_records: UNKNOWN" in note
        for note in result.scope.not_covered
    )
    assert result.recheck is None and "recheck_due_at" not in result.receipt


def test_a_missing_declared_identity_stays_unknown(tmp_path: Path) -> None:
    db = api_cases._store(tmp_path)
    doc = api_cases._doc(db)
    doc["verifier"]["sql_query"] = "SELECT fname, lname, email FROM patient"
    pending = begin_write(
        Contract.from_dict(doc), api_cases.PARAMS, actor=api_cases.BOT
    )
    api_cases._write(db, api_cases.SAVE)
    result = verify_write(pending)
    assert result.outcome is Outcome.UNCERTAIN and not result.verified
    assert result.evidence["frame"]["hard_invariants"]["status"] == "not_checked"


def test_an_unreadable_declared_projection_cannot_confirm(tmp_path: Path) -> None:
    db = api_cases._store(tmp_path)
    pending = begin_write(
        api_cases._contract(db), api_cases.PARAMS, actor=api_cases.BOT
    )
    api_cases._write(db, api_cases.SAVE)
    db.rename(db.with_suffix(".unavailable"))
    result = verify_write(pending)
    assert result.outcome is Outcome.UNCERTAIN and not result.verified
    assert result.evidence["frame"]["hard_invariants"]["status"] == "not_checked"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id, fname AS email, email, lname FROM patient",
        'SELECT id, fname AS "", email, lname FROM patient',
    ],
)
def test_ambiguous_native_projection_labels_refuse_before_write(
    tmp_path: Path, query: str
) -> None:
    db = api_cases._store(tmp_path)
    doc = api_cases._doc(db)
    doc["verifier"]["sql_query"] = query
    with pytest.raises(verifier.BaselineUnreadable):
        begin_write(Contract.from_dict(doc), api_cases.PARAMS, actor=api_cases.BOT)


def test_dbapi_width_mismatch_is_a_structural_refusal() -> None:
    """A DB-API shape control; this is not an actual native database trial."""
    from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

    class Cursor:
        description = [("id",), ("value",)]

        def execute(self, query: str) -> None:
            pass

        def fetchall(self) -> list[tuple[int]]:
            return [(1,)]

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            pass

    state = SqlRecordVerifier(
        Connection, "SELECT id, value FROM synthetic"
    ).capture_pre_state()
    assert state.reachable is False and state.records == []


def test_declared_database_uses_existing_row_commitments_at_write_and_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, _db, result = late_cases._first(tmp_path)
    assert result.verified and result.provisional and result.recheck is not None
    assert_undeclared_search(result.evidence["frame"]["hard_invariants"])
    checkpoint = result.recheck.payload["post_state"]["receipt_records"]
    assert checkpoint["targets"] and all(
        part["row_key_mac"] for part in checkpoint["targets"]
    )
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    late = late_cases._recheck(contract, result)
    assert late.outcome == "CONFIRMED" and late.verified
    assert_undeclared_search(late.evidence["hard_invariants"])
    guarantee = late.evidence["hard_invariants"]["guarantee"]
    assert guarantee["scope"] == "declared_database"
    assert guarantee["identity_binding"] == "native_primary_key"
    assert guarantee["receipt_sha256"] == result.receipt_sha256
    assert guarantee["phase"] == "late"
    encoded = json.dumps(result.recheck.payload, sort_keys=True)
    for canary in ("Quill2", "ada2@example.test", "Wendell", "w@example.test"):
        assert canary not in encoded
        assert canary not in json.dumps(result.receipt, sort_keys=True)


def test_actual_declared_database_row_budget_failure_is_unknown(tmp_path: Path) -> None:
    db = late_cases._db(tmp_path)
    doc = late_cases._doc(db)
    doc["frame"]["database"]["read_row_budget"] = 20
    contract = Contract.from_dict(doc)
    pending = begin_write(contract, late_cases._params(2), actor=late_cases.BOT)
    late_cases._sql(
        db,
        *late_cases._save_sql(2),
        *[
            f"INSERT INTO api_log (note) VALUES ('synthetic-{index}')"
            for index in range(21)
        ],
    )
    result = verify_write(pending)
    assert result.outcome is Outcome.UNCERTAIN and not result.verified
    assert result.evidence["frame"]["database"]["read_status"] == "too_large"
    assert result.evidence["frame"]["hard_invariants"]["status"] == "not_checked"


def test_a_snapshot_from_another_source_cannot_bind_a_projected_record(
    tmp_path: Path,
) -> None:
    db = late_cases._db(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = late_cases._db(other_dir)
    doc = late_cases._doc(db)
    doc["frame"]["database"]["read"]["sqlite_database"] = str(other)
    pending = begin_write(
        Contract.from_dict(doc), late_cases._params(2), actor=late_cases.BOT
    )
    late_cases._sql(db, *late_cases._save_sql(2))
    late_cases._sql(other, *late_cases._save_sql(2))
    result = verify_write(pending)
    assert result.outcome is Outcome.UNCERTAIN and not result.verified
    assert result.evidence["frame"]["hard_invariants"]["status"] == "not_checked"


def test_tampered_native_row_anchor_refuses_before_database_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, _db, result = late_cases._first(tmp_path)
    assert result.recheck is not None
    payload = json.loads(json.dumps(result.recheck.payload))
    payload["post_state"]["receipt_records"]["targets"][0]["row_mac"] = "0" * 32
    calls: list[bool] = []
    original = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    late = recheck(
        contract,
        verifier.RecheckState(payload),
        params=late_cases._params(2),
        actor=late_cases.BOT,
    )
    assert late.outcome == "UNCERTAIN" and not late.verified
    assert calls == [] and not late.read_at
    assert not list((tmp_path / "issued").rglob("*.inflight"))


def test_native_projection_value_change_never_becomes_an_unknown_search_waiver(
    tmp_path: Path,
) -> None:
    db = api_cases._store(tmp_path)
    pending = begin_write(
        api_cases._contract(db), api_cases.PARAMS, actor=api_cases.BOT
    )
    api_cases._write(db, api_cases.SAVE.replace("'Ada'", "'Different'"))
    result = verify_write(pending)
    assert result.outcome is Outcome.REFUTED and not result.verified


def test_existing_native_mcp_route_keeps_undeclared_scope_visible(
    sqlite_bridge: tuple[VerifyBridge, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, db = sqlite_bridge
    checked = mcp_cases._provisional(bridge, db)
    _, result = bridge._rechecks[checked["write_id"]]
    assert_undeclared_search(result.evidence["frame"]["hard_invariants"])
    assert any(
        "identifying_search_duplicates_and_side_records: UNKNOWN" in note
        for note in result.scope.not_covered
    )
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    late = bridge.dispatch("verify_recheck", {"write_id": checked["write_id"]})
    assert late["outcome"] == "CONFIRMED" and late["verified"]
