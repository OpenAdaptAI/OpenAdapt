"""Review round 1 of the exact number rule (#1664): one rule everywhere.

The record check selected the save's record by value, but the duplicate-key
count still compared text, so a duplicate whose key holds a whole-number
float was CONFIRMED. Every comparison of a bound value with a stored one
now uses the same rule: record selection, the composite-key holders, undo's
record lookup, and the frame's side-row links. A number with an exponent
too large to compare is refused when the write begins, naming the
parameter. Adapted from the reviewer's attack tests. Records are made up.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.after_stop import _matching, _resolved_selector
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    SqlRecordVerifier,
    ValueExpr,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import number_matches, record_matches
from openadapt_flow.verifier import (
    Actor,
    Contract,
    MemoryLedger,
    Outcome,
    VerifierError,
    begin_write,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")


@pytest.fixture(autouse=True)
def _ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def _state(records: list[dict[str, Any]], substrate: str = "sql") -> EffectState:
    return EffectState(substrate=substrate, reachable=True, records=records)


def _keyed(hours: Any) -> Effect:
    key = {"issue": ValueExpr(param="issue"), "hours": ValueExpr(param="hours")}
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=dict(key),
        composite_key=dict(key),
        expected_count=1,
        count_new_only=True,
        timeout_s=0.05,
    ).resolve({"issue": "Backfill", "hours": hours})


@pytest.mark.parametrize("stored", [2.0, Decimal("2.00")])
def test_a_duplicate_with_a_numeric_key_is_refuted(stored: Any) -> None:
    before = [{"id": 1, "issue": "Backfill", "hours": stored}]
    after = [*before, {"id": 2, "issue": "Backfill", "hours": stored}]
    verdict = judge_records(_keyed(2.0), _state(before), after, substrate="sql")
    assert verdict.verdict.value == "refuted", verdict.reason
    assert "key" in verdict.reason


def test_a_duplicate_with_a_numeric_key_is_refuted_on_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE t(id INTEGER PRIMARY KEY, issue TEXT, hours REAL);"
        "INSERT INTO t VALUES(1,'Backfill',2.0);"
    )
    con.commit()
    con.close()
    verifier = SqlRecordVerifier(lambda: sqlite3.connect(db), "SELECT * FROM t")
    effect = _keyed(2)
    before = verifier.capture_pre_state()
    con = sqlite3.connect(db)
    con.execute("INSERT INTO t(issue,hours) VALUES('Backfill',2.0)")
    con.commit()
    con.close()
    assert verifier.verify(effect, before).verdict.value == "refuted"


def _db(tmp_path: Path, coltype: str, seed: str) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        f"CREATE TABLE time_entry (id INTEGER PRIMARY KEY, issue TEXT, "
        f"spent_on TEXT, hours {coltype}, code TEXT);"
        f"INSERT INTO time_entry VALUES (1, 'Backfill', '2026-10-30', {seed}, 'x');"
    )
    con.commit()
    con.close()
    return path


def _contract(db: Path, *, composite: bool = True) -> Contract:
    match = {
        "issue": {"param": "issue"},
        "spent_on": {"param": "spent_on"},
        "hours": {"param": "hours"},
    }
    effect: dict[str, Any] = {
        "kind": "record_written",
        "match": match,
        "expected_count": 1,
        "count_new_only": True,
    }
    if composite:
        effect["composite_key"] = dict(match)
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM time_entry",
            },
            "effects": [effect],
            "frame": {"identity_field": "id"},
        }
    )


@pytest.mark.parametrize(
    ("coltype", "seed", "hours"),
    [("REAL", "2.0", 2.0), ("NUMERIC(6,2)", "2.0", 2), ("REAL", "1.5", 1.5)],
)
def test_a_duplicate_time_entry_is_not_confirmed(
    tmp_path: Path, coltype: str, seed: str, hours: float
) -> None:
    db = _db(tmp_path, coltype, seed)
    params = {"issue": "Backfill", "spent_on": "2026-10-30", "hours": hours}
    pending = begin_write(_contract(db), params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO time_entry (issue, spent_on, hours, code) "
        "VALUES ('Backfill', '2026-10-30', ?, 'x')",
        (float(hours),),
    )
    con.commit()
    con.close()
    assert verify_write(pending).outcome is not Outcome.CONFIRMED


@pytest.mark.parametrize("text", ["1e999999999999999999999", "1e-1000000"])
def test_a_huge_exponent_is_refused_at_begin_naming_the_parameter(
    tmp_path: Path, text: str
) -> None:
    db = _db(tmp_path, "REAL", "2.0")
    params = {"issue": "Backfill", "spent_on": "2026-10-30", "hours": text}
    with pytest.raises(VerifierError, match="parameter 'hours'.*exponent"):
        begin_write(
            _contract(db, composite=False), params, actor=BOT, ledger=MemoryLedger()
        )


@pytest.mark.parametrize(
    "text", ["1e999999999999999999999", "1e-999999999999999999999", "1e1000000"]
)
def test_a_huge_exponent_never_raises_at_match_time(text: str) -> None:
    assert number_matches(2, text, text_numbers=True) is False
    assert number_matches(2.0, text, text_numbers=True) is False
    assert number_matches(2, "9" * 100000, text_numbers=True) is False


def test_a_huge_exponent_number_literal_is_refused_at_validate() -> None:
    with pytest.raises(ValueError, match="exponent"):
        ValueExpr(literal="1e9999999", number=True)
    with pytest.raises(ValueError, match="parameter 'h'"):
        ValueExpr(param="h").resolved({"h": "1e9999999"})


def test_the_number_flag_is_bound_in_the_contract_hash() -> None:
    marked = Effect(
        kind="record_written", match={"h": ValueExpr(literal="2", number=True)}
    )
    plain = Effect(kind="record_written", match={"h": ValueExpr(literal="2")})
    assert marked.contract_hash() != plain.contract_hash()
    # A contract without a number keeps its digest.
    assert "value_numbers" not in plain._contract_payload()


@pytest.mark.parametrize(
    ("want", "stored"), [("2", "2.0"), ("2139", "02139"), ("100", "1e2")]
)
def test_a_text_column_stays_text(want: str, stored: str) -> None:
    assert record_matches({"f": stored}, {"f": want}, text_numbers=True) is False


def test_float_binary_is_exact() -> None:
    assert number_matches(0.1, "0.1", text_numbers=True) is True
    long = "0.1000000000000000055511151231257827"
    assert number_matches(0.1, long, text_numbers=True) is False
    assert number_matches(1e16, "10000000000000001", text_numbers=True) is False
    assert number_matches(0.1 + 0.2, "0.3", text_numbers=True) is False


def test_special_values() -> None:
    assert number_matches(-0.0, "0", text_numbers=True)  # -0 == 0, as in SQL
    for stored in (Decimal("NaN"), Decimal("-Infinity"), Decimal("sNaN"), True, False):
        assert number_matches(stored, "0", text_numbers=True) is None


def test_a_rest_string_parameter_stays_text() -> None:
    effect = Effect(
        kind="record_written", match={"h": {"param": "h"}}, expected_count=1
    ).resolve({"h": "2"})
    result = judge_records(effect, _state([], "rest"), [{"h": 2.0}], substrate="rest")
    assert result.verdict.value == "refuted"


def test_field_equals_number_against_a_text_column() -> None:
    effect = Effect(
        kind="field_equals", match={"id": "1"}, field="code", value={"param": "c"}
    ).resolve({"c": 2})
    record = {"id": 1, "code": "2.0"}
    result = judge_records(effect, _state([record]), [record], substrate="sql")
    assert result.verdict.value == "refuted"


def test_undo_finds_a_whole_number_record_by_value() -> None:
    selector = _resolved_selector({"hours": {"param": "h"}}, {"h": 2.0})
    assert selector is not None
    records = [{"id": 1, "hours": 2.0}, {"id": 2, "hours": 2.5}]
    assert [r["id"] for r in _matching(records, selector)] == [1]
    assert _matching([{"id": 3, "hours": "2.0"}], selector) == []  # text stays text
