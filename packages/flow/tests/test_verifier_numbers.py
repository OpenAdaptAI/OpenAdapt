"""Numbers compare by exact value only where the store typed them as numbers.

A run parameter ``hours: 2.0`` reaches the record check as the text ``"2"``
(the JSON rendering), while a SQL ``float8`` column reads back as ``2.0``.
Compared as text, ``"2"`` never equalled ``"2.0"``, so every whole number of
hours was stopped. Now a stored number (an ``int``, ``float`` or
``Decimal`` the driver returned for a numeric column, or a JSON number)
compares by exact decimal value with an expected number. Text columns and
JSON strings stay byte-exact. There is no tolerance: ``0.1 + 0.2`` is not
``0.3``. These run on a real SQLite store. Records are made up.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    ValueExpr,
    number_matches,
    plain_number,
    record_matches,
)
from openadapt_flow.verifier import (
    Actor,
    Contract,
    MemoryLedger,
    Outcome,
    begin_write,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")


@pytest.fixture(autouse=True)
def _ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE time_entry (id INTEGER PRIMARY KEY, issue TEXT, "
        "hours REAL, code TEXT);"
        "INSERT INTO time_entry VALUES (1, 'Old', 1.5, '7');"
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
                "sql_query": "SELECT * FROM time_entry",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {
                        "issue": {"param": "issue"},
                        "hours": {"param": "hours"},
                        "code": {"param": "code"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                }
            ],
            "frame": {"identity_field": "id"},
        }
    )


def _run(tmp_path: Path, params: dict[str, Any], row: tuple[Any, ...]) -> Any:
    db = _store(tmp_path)
    pending = begin_write(_contract(db), params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute("INSERT INTO time_entry (issue, hours, code) VALUES (?, ?, ?)", row)
    con.commit()
    con.close()
    return verify_write(pending)


@pytest.mark.parametrize("hours", [2.0, 2, 1.5, 0.25, 4.0])
def test_a_whole_or_fractional_number_of_hours_is_confirmed(
    tmp_path: Path, hours: float
) -> None:
    params = {"issue": "Backfill", "hours": hours, "code": "A1"}
    result = _run(tmp_path, params, ("Backfill", float(hours), "A1"))
    assert result.outcome is Outcome.CONFIRMED, result.reasons


@pytest.mark.parametrize(
    ("hours", "stored"),
    [(2.0, 2.5), (2.0, 20.0), (0.3, 0.1 + 0.2), (1.0, 1.0000001)],
)
def test_a_different_number_still_refutes(
    tmp_path: Path, hours: float, stored: float
) -> None:
    params = {"issue": "Backfill", "hours": hours, "code": "A1"}
    result = _run(tmp_path, params, ("Backfill", stored, "A1"))
    assert result.outcome is Outcome.REFUTED, result.reasons


def test_a_text_column_stays_exact(tmp_path: Path) -> None:
    # ``code`` is TEXT: a stored "2.0" is not the parameter 2 (text "2").
    params = {"issue": "Backfill", "hours": 2.0, "code": 2}
    result = _run(tmp_path, params, ("Backfill", 2.0, "2.0"))
    assert result.outcome is Outcome.REFUTED, result.reasons


# -- the comparison itself ----------------------------------------------------


def test_a_number_parameter_binds_as_a_number() -> None:
    bound = ValueExpr(param="h").resolved({"h": 2.0})
    assert bound.literal == "2" and bound.number is True
    assert ValueExpr(param="h").resolved({"h": "2"}).number is None
    assert ValueExpr(param="h").resolved({"h": True}).number is None
    mapped = ValueExpr(param="h", map={"2": "two"}).resolved({"h": 2})
    assert mapped.number is None and mapped.literal == "two"
    assert ValueExpr.model_validate(bound.model_dump()) == bound
    assert "number" not in ValueExpr(literal="2").model_dump()


@pytest.mark.parametrize("text", ["2", "2.00", "-0.25", "1e+21", "0", "0.5"])
def test_plain_numbers(text: str) -> None:
    assert plain_number(text) == Decimal(text)


@pytest.mark.parametrize(
    "text", ["", " 2", "2 ", "+2", "02", "1_000", "NaN", "Infinity", "2.", ".5", "0x10"]
)
def test_anything_else_is_text(text: str) -> None:
    assert plain_number(text) is None


NUMBER_2 = ValueExpr(literal="2", number=True)


@pytest.mark.parametrize(
    ("stored", "want", "text_numbers", "expected"),
    [
        (2.0, NUMBER_2, False, True),  # JSON number vs JSON number
        (2, NUMBER_2, False, True),
        (Decimal("2.00"), NUMBER_2, False, True),
        (2.5, NUMBER_2, False, False),
        (2.0, "2", False, None),  # a JSON body: text expected, text rule
        (2.0, "2", True, True),  # a SQL numeric column: by value
        (Decimal("2.00"), "2.0", True, True),
        (2.0, "02", True, None),  # not a plain decimal: text rule
        ("2.0", NUMBER_2, True, None),  # stored text: text rule
        (True, ValueExpr(literal="1", number=True), True, None),  # a bool
        (float("nan"), NUMBER_2, True, None),
        (float("inf"), NUMBER_2, True, None),
    ],
)
def test_number_matches(
    stored: Any, want: Any, text_numbers: bool, expected: bool | None
) -> None:
    assert number_matches(stored, want, text_numbers=text_numbers) is expected


@pytest.mark.parametrize(
    ("stored", "want", "expected"),
    [
        ("2.0", "2", False),  # text stays byte-exact
        ("2", "2", True),
        ("2.00", NUMBER_2, False),  # a stored text is never a number
        (2.0, "2", False),  # a JSON body without a number parameter
        (2.0, NUMBER_2, True),
    ],
)
def test_record_matches_json(stored: Any, want: Any, expected: bool) -> None:
    assert record_matches({"f": stored}, {"f": want}) is expected


def _state(records: list[dict[str, Any]], substrate: str) -> EffectState:
    return EffectState(substrate=substrate, reachable=True, records=records)


@pytest.mark.parametrize(
    ("substrate", "stored", "verdict"),
    [
        ("rest", 2.0, "confirmed"),  # JSON number vs a number parameter
        ("rest", "2.0", "refuted"),  # JSON string: exact text
        ("rest", "2", "confirmed"),  # unchanged: text "2" equals text "2"
        ("sql", Decimal("2.00"), "confirmed"),
        ("sql", 2.01, "refuted"),
    ],
)
def test_judged_by_substrate(substrate: str, stored: Any, verdict: str) -> None:
    effect = Effect(
        kind="record_written",
        match={"hours": {"param": "hours"}},
        expected_count=1,
        count_new_only=True,
    ).resolve({"hours": 2.0})
    before = [{"id": 1, "hours": 9}]
    after = [*before, {"id": 2, "hours": stored}]
    result = judge_records(
        effect, _state(before, substrate), after, substrate=substrate
    )
    assert result.verdict.value == verdict, result.reason


@pytest.mark.parametrize(
    ("stored", "verdict"), [(2.0, "confirmed"), (2.5, "refuted"), ("2.0", "refuted")]
)
def test_field_equals_by_value(stored: Any, verdict: str) -> None:
    effect = Effect(
        kind="field_equals", match={"id": "1"}, field="hours", value={"param": "h"}
    ).resolve({"h": 2.0})
    record = {"id": 1, "hours": stored}
    result = judge_records(effect, _state([record], "sql"), [record], substrate="sql")
    assert result.verdict.value == verdict, result.reason
