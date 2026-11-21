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


# -- SQL verification read null binding (openadapt-internal#278 P7) ----------


@pytest.mark.parametrize(
    "sql_query",
    [
        "SELECT * FROM issue WHERE subject = :subject AND ref = :ref",
        "SELECT * FROM issue WHERE subject = :subject AND :ref = ref",
        'SELECT * FROM issue WHERE subject = :subject AND "issue"."ref" = :ref',
        "SELECT * FROM issue WHERE subject = :subject AND (ref = :ref)",
        "SELECT * FROM issue WHERE subject = :subject AND ref IS NOT DISTINCT FROM :ref",
    ],
)
def test_null_sql_query_equality_confirms_stored_null(
    tmp_path: Path, sql_query: str
) -> None:
    db = _store(tmp_path)
    contract = _contract(
        db,
        verifier={
            "sql_query": sql_query,
            "sql_query_params": {
                "subject": {"param": "subject"},
                "ref": {"param": "ref"},
            },
        },
    )
    pending = begin_write(contract, NULLS, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO issue (subject, description, ref) VALUES (?, ?, ?)",
        ("Tax rounding", None, None),
    )
    con.commit()
    con.close()
    result = verify_write(pending)
    assert result.outcome is Outcome.CONFIRMED, result.reasons


@pytest.mark.parametrize(
    "stored_ref",
    ["R-9", "", "None", "null", "0"],
)
def test_null_sql_query_equality_refutes_planted_non_null_value(
    tmp_path: Path, stored_ref: str
) -> None:
    """Planted null-vs-value fault: even if effect.match omits ``ref``, the
    SQL ``IS NULL`` filter alone refutes any non-null stored value."""
    db = _store(tmp_path)
    contract = _contract(
        db,
        verifier={
            "sql_query": "SELECT * FROM issue WHERE subject = :subject AND ref = :ref",
            "sql_query_params": {
                "subject": {"param": "subject"},
                "ref": {"param": "ref"},
            },
        },
        effect={
            "kind": "record_written",
            "match": {"subject": {"param": "subject"}},
            "expected_count": 1,
            "count_new_only": True,
        },
    )
    params = {"subject": "Tax rounding", "ref": None}
    pending = begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO issue (subject, description, ref) VALUES (?, ?, ?)",
        ("Tax rounding", None, stored_ref),
    )
    con.commit()
    con.close()
    result = verify_write(pending)
    assert result.outcome is not Outcome.CONFIRMED, result.reasons
    assert any("record_written was refuted" in r for r in result.reasons), (
        result.reasons
    )


def test_non_null_sql_query_equality_refutes_planted_stored_null(
    tmp_path: Path,
) -> None:
    """Planted value-vs-null fault: expecting ``ref='R-9'`` refutes stored NULL."""
    db = _store(tmp_path)
    contract = _contract(
        db,
        verifier={
            "sql_query": "SELECT * FROM issue WHERE subject = :subject AND ref = :ref",
            "sql_query_params": {
                "subject": {"param": "subject"},
                "ref": {"param": "ref"},
            },
        },
        effect={
            "kind": "record_written",
            "match": {"subject": {"param": "subject"}},
            "expected_count": 1,
            "count_new_only": True,
        },
    )
    params = {"subject": "Tax rounding", "ref": "R-9"}
    pending = begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO issue (subject, description, ref) VALUES (?, ?, ?)",
        ("Tax rounding", None, None),
    )
    con.commit()
    con.close()
    result = verify_write(pending)
    assert result.outcome is not Outcome.CONFIRMED, result.reasons
    assert any("record_written was refuted" in r for r in result.reasons), (
        result.reasons
    )


def test_null_sql_query_param_in_multi_surface_contract(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "surfaces",
                "surfaces": {
                    "issues": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": (
                            "SELECT * FROM issue "
                            "WHERE subject = :subject AND ref = :ref"
                        ),
                        "sql_query_params": {
                            "subject": {"param": "subject"},
                            "ref": {"param": "ref"},
                        },
                    }
                },
            },
            "effects": [
                {
                    "kind": "record_written",
                    "probe": "surface=issues",
                    "match": {
                        "subject": {"param": "subject"},
                        "ref": {"param": "ref"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                }
            ],
            "frame": {"identity_field": "id"},
        }
    )
    pending = begin_write(
        contract,
        {"subject": "Tax rounding", "ref": None},
        actor=BOT,
        ledger=MemoryLedger(),
    )
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO issue (subject, description, ref) VALUES (?, ?, ?)",
        ("Tax rounding", None, None),
    )
    con.commit()
    con.close()
    result = verify_write(pending)
    assert result.outcome is Outcome.CONFIRMED, result.reasons


def test_null_sql_query_param_still_refused_if_also_used_in_idempotency_key(
    tmp_path: Path,
) -> None:
    db = _store(tmp_path)
    contract = _contract(
        db,
        verifier={
            "sql_query": "SELECT * FROM issue WHERE ref = :ref",
            "sql_query_params": {"ref": {"param": "ref"}},
        },
        effect={"idempotency_key": {"param": "ref"}, "key_field": "ref"},
    )
    with pytest.raises(VerifierError, match=r"ref \(used in effects\[0\]"):
        begin_write(contract, NULLS, actor=BOT, ledger=MemoryLedger())


def test_bind_null_sql_params_pyformat_and_multiple_occurrences() -> None:
    from openadapt_flow.runtime.effects.sql import bind_null_sql_params

    rewritten, remaining = bind_null_sql_params(
        "SELECT * FROM patient WHERE given = %(given)s AND middle = %(middle)s",
        {"given": "Ada", "middle": None},
    )
    assert (
        rewritten == "SELECT * FROM patient WHERE given = %(given)s AND middle IS NULL"
    )
    assert remaining == {"given": "Ada"}

    rewritten2, remaining2 = bind_null_sql_params(
        "SELECT * FROM t WHERE t.a = %(x)s AND t.b = %(x)s",
        {"x": None},
    )
    assert rewritten2 == "SELECT * FROM t WHERE t.a IS NULL AND t.b IS NULL"
    assert remaining2 == {}

    rewritten3, remaining3 = bind_null_sql_params(
        "SELECT * FROM t WHERE a=:a",
        {"a": None},
    )
    assert rewritten3 == "SELECT * FROM t WHERE a IS NULL"
    assert remaining3 == {}

    # Native null-safe operators keep None bound in query_params.
    rewritten4, remaining4 = bind_null_sql_params(
        "SELECT * FROM t WHERE a <=> %(a)s",
        {"a": None},
    )
    assert rewritten4 == "SELECT * FROM t WHERE a <=> %(a)s"
    assert remaining4 == {"a": None}


@pytest.mark.parametrize(
    "unsafe_query",
    [
        "SELECT * FROM issue WHERE ref = :ref OR :ref IS NULL",
        "SELECT * FROM issue WHERE ref LIKE :ref",
        "SELECT * FROM issue WHERE ref IN (:ref)",
        "SELECT * FROM issue WHERE ref != :ref",
        "SELECT * FROM issue WHERE ref <> :ref",
        "SELECT * FROM issue WHERE ref > :ref",
        "SELECT * FROM issue WHERE NOT ref = :ref",
        "SELECT * FROM issue WHERE subject = 'x' AND NOT ref = :ref",
        "SELECT * FROM issue WHERE NOT (ref = :ref)",
        "SELECT * FROM issue WHERE NOT (subject = 'x' AND ref = :ref)",
        "SELECT * FROM issue WHERE NOT :ref = ref",
        "SELECT * FROM issue WHERE NOT ref IS NOT DISTINCT FROM :ref",
        "SELECT * FROM issue WHERE ref = :ref + 1",
        "SELECT * FROM issue WHERE ref = :ref || 'x'",
        "SELECT * FROM issue WHERE subject = ref = :ref",
        "SELECT * FROM issue WHERE (ref = :ref) = 1",
        "SELECT * FROM issue WHERE COALESCE(ref = :ref, 0) = 1",
        "SELECT * FROM issue WHERE lower(ref = :ref)",
        "SELECT :ref FROM issue",
        "SELECT (ref = :ref) FROM issue",
        "SELECT * FROM issue LIMIT :ref",
        "SELECT * FROM issue",  # unused param
    ],
)
def test_unsafe_null_sql_query_positions_fail_closed(unsafe_query: str) -> None:
    from openadapt_flow.runtime.effects.sql import (
        bind_null_sql_params,
        sql_param_is_null_safe,
    )

    assert not sql_param_is_null_safe(unsafe_query, "ref")
    with pytest.raises(ValueError, match="null SQL query parameter 'ref'"):
        bind_null_sql_params(unsafe_query, {"ref": None})


def test_missing_or_unmapped_sql_query_param_still_fails_loud() -> None:
    from openadapt_flow.deployment import _resolve_sql_query_params

    with pytest.raises(ValueError, match="was not supplied"):
        _resolve_sql_query_params({"ref": ValueExpr(param="ref")}, {})

    with pytest.raises(ValueError, match="was not supplied"):
        _resolve_sql_query_params(
            {"status": ValueExpr(param="status", map={"Active": "1"})},
            {"status": "Unknown"},
        )

    assert _resolve_sql_query_params(
        {"ref": ValueExpr(param="ref")}, {"ref": None}
    ) == {"ref": None}
