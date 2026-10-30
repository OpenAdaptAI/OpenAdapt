"""A parameter set to ``None`` binds to a null value (issue #1660).

A null value matches a stored SQL ``NULL`` (and, for a REST record, an
absent or null field) exactly. It never matches ``""``, the text
``"None"``, or any other stored value, so a wrong write still stops. Where
a null has nothing to bind to (a query parameter, a key), ``begin_write``
refuses it before anything is read. These run on a real SQLite store.
Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    ValueExpr,
    record_matches,
)
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


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE issue (id INTEGER PRIMARY KEY, subject TEXT, "
        "description TEXT, ref TEXT);"
        "INSERT INTO issue VALUES (1, 'Old', 'kept', 'R-1');"
    )
    con.commit()
    con.close()
    return path


def _contract(db: Path, **extra: Any) -> Contract:
    verifier: dict[str, Any] = {
        "kind": "sql",
        "sqlite_database": str(db),
        "sql_query": "SELECT * FROM issue",
    }
    verifier.update(extra.pop("verifier", {}))
    effect: dict[str, Any] = {
        "kind": "record_written",
        "match": {
            "subject": {"param": "subject"},
            "description": {"param": "description"},
            "ref": {"param": "ref"},
        },
        "expected_count": 1,
        "count_new_only": True,
    }
    effect.update(extra.pop("effect", {}))
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": verifier,
            "effects": [effect],
            "frame": {"identity_field": "id"},
        }
    )


def _run(
    tmp_path: Path,
    params: dict[str, Any],
    insert: Optional[tuple[Any, ...]],
) -> Any:
    db = _store(tmp_path)
    pending = begin_write(_contract(db), params, actor=BOT, ledger=MemoryLedger())
    if insert is not None:
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO issue (subject, description, ref) VALUES (?, ?, ?)", insert
        )
        con.commit()
        con.close()
    return verify_write(pending)


NULLS = {"subject": "Tax rounding", "description": None, "ref": None}


def test_a_correct_write_with_nulls_is_confirmed(tmp_path: Path) -> None:
    result = _run(tmp_path, NULLS, ("Tax rounding", None, None))
    assert result.outcome is Outcome.CONFIRMED, result.reasons


def test_no_write_with_nulls_is_read_back_not_unreadable(tmp_path: Path) -> None:
    # Nothing landed yet: the record was read and is absent (a write that
    # may still land stays UNCERTAIN), not "could not be read back".
    result = _run(tmp_path, NULLS, None)
    assert result.outcome is not Outcome.CONFIRMED
    assert result.reasons == [
        "record_written was refuted: observed absent, 0 of 1 expected record(s)"
    ]


@pytest.mark.parametrize(
    "stored",
    [
        ("Tax rounding", "", None),  # "" is not NULL
        ("Tax rounding", "None", None),  # nor is the text "None"
        ("Tax rounding", "null", None),  # nor the text "null"
        ("Tax rounding", "a description", None),  # a value where none was asked
        ("Tax rounding", None, "R-9"),  # the other null field holds a value
        ("Tax roundin", None, None),  # a wrong non-null value
    ],
)
def test_a_wrong_write_on_a_null_task_still_stops(
    tmp_path: Path, stored: tuple[Any, ...]
) -> None:
    result = _run(tmp_path, NULLS, stored)
    assert result.outcome is Outcome.REFUTED, result.reasons
    assert "VerifyRunError" not in " ".join(result.reasons)


@pytest.mark.parametrize("text", ["", "None"])
def test_a_text_parameter_never_matches_a_stored_null(
    tmp_path: Path, text: str
) -> None:
    params = {"subject": "Tax rounding", "description": text, "ref": None}
    result = _run(tmp_path, params, ("Tax rounding", None, None))
    assert result.outcome is Outcome.REFUTED, result.reasons


def test_a_null_query_parameter_is_refused_at_begin_write(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(
        db,
        verifier={
            "sql_query": "SELECT * FROM issue WHERE ref = :ref OR :ref IS NULL",
            "sql_query_params": {"ref": {"param": "ref"}},
        },
    )
    with pytest.raises(VerifierError, match=r"null value can't be bound for: ref"):
        begin_write(contract, NULLS, actor=BOT, ledger=MemoryLedger())


def test_a_null_idempotency_key_is_refused_at_begin_write(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(
        db,
        effect={"idempotency_key": {"param": "ref"}, "key_field": "ref"},
    )
    with pytest.raises(VerifierError, match=r"ref \(used in effects\[0\]"):
        begin_write(contract, NULLS, actor=BOT, ledger=MemoryLedger())


# -- the comparison itself ----------------------------------------------------

NULL = ValueExpr(null=True)


def test_a_bound_null_parameter_is_a_null_value() -> None:
    bound = ValueExpr(param="x").resolved({"x": None})
    assert bound.null and bound.literal is None and bound.param is None
    assert ValueExpr(param="x").resolve({"x": None}) is None
    assert bound != "None" and bound != "" and bound != "null"
    assert ValueExpr.model_validate(bound.model_dump()) == bound
    assert "null" not in ValueExpr(literal="a").model_dump()


def test_a_null_value_takes_no_other_source() -> None:
    with pytest.raises(ValueError):
        ValueExpr(null=True, literal="a")
    with pytest.raises(ValueError):
        ValueExpr(null=True, param="a")


@pytest.mark.parametrize(
    ("record", "absent_is_null", "expected"),
    [
        ({"f": None}, False, True),
        ({"f": None}, True, True),
        ({}, False, False),  # missing: may be misspelled or unprojected
        ({}, True, True),  # declared: the store leaves a null field out
        ({"f": ""}, True, False),
        ({"f": "None"}, True, False),
        ({"f": 0}, True, False),
        ({"f": False}, True, False),
        ({"f": []}, True, False),
    ],
)
def test_record_matches_a_null_value_exactly(
    record: dict[str, Any], absent_is_null: bool, expected: bool
) -> None:
    want = ValueExpr(null=True, absent_is_null=True if absent_is_null else None)
    assert record_matches(record, {"f": want}) is expected


@pytest.mark.parametrize("want", ["None", "", "null"])
def test_a_stored_null_never_matches_text(want: str) -> None:
    assert not record_matches({"f": None}, {"f": want})
    assert not record_matches({}, {"f": want})
    assert not record_matches({"f": None}, {"f": ValueExpr(literal=want)})


def _state(records: list[dict[str, Any]]) -> EffectState:
    return EffectState(substrate="rest", reachable=True, records=records)


@pytest.mark.parametrize(
    ("declared", "after", "verdict"),
    [
        (False, {"id": 2, "name": "n"}, "refuted"),  # absent is not null
        (True, {"id": 2, "name": "n"}, "confirmed"),  # unless declared
        (False, {"id": 2, "name": "n", "note": None}, "confirmed"),
        (True, {"id": 2, "name": "n", "note": ""}, "refuted"),
        (True, {"id": 2, "name": "n", "note": "x"}, "refuted"),
    ],
)
@pytest.mark.parametrize("substrate", ["rest", "fhir", "sql"])
def test_absent_is_null_only_where_the_contract_declares_it(
    substrate: str, declared: bool, after: dict[str, Any], verdict: str
) -> None:
    note: dict[str, Any] = {"param": "note"}
    if declared:
        note["absent_is_null"] = True
    effect = Effect(
        kind="record_written",
        match={"name": {"param": "name"}, "note": note},
        expected_count=1,
        count_new_only=True,
    ).resolve({"name": "n", "note": None})
    before = [{"id": 1, "name": "old", "note": "x"}]
    result = judge_records(
        effect, _state(before), [*before, after], substrate=substrate
    )
    assert result.verdict.value == verdict, result.reason


def test_absent_is_null_is_bound_in_the_contract_hash() -> None:
    plain = Effect(kind="record_written", match={"f": {"param": "f"}})
    marked = Effect(
        kind="record_written", match={"f": {"param": "f", "absent_is_null": True}}
    )
    assert plain.contract_hash() != marked.contract_hash()
    assert "absent_is_null" not in plain._contract_payload()
    with pytest.raises(ValueError):
        ValueExpr(literal="a", absent_is_null=True)


@pytest.mark.parametrize(
    ("stored", "verdict"),
    [(None, "confirmed"), ("", "refuted"), ("x", "refuted")],
)
def test_field_equals_a_null_value(stored: Any, verdict: str) -> None:
    effect = Effect(
        kind="field_equals",
        match={"id": "1"},
        field="note",
        value={"param": "note"},
    ).resolve({"note": None})
    record = {"id": 1, "note": stored}
    result = judge_records(effect, _state([record]), [record], substrate="sql")
    assert result.verdict.value == verdict, result.reason


def test_field_equals_empty_text_is_not_a_stored_null() -> None:
    effect = Effect(kind="field_equals", match={"id": "1"}, field="note", value="")
    record = {"id": 1, "note": None}
    result = judge_records(effect, _state([record]), [record], substrate="sql")
    assert result.verdict.value == "refuted"
