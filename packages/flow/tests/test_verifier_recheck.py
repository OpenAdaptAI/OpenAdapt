"""Late writes: the recheck after a verified save (``recheck_after_s``).

The frame reads the database twice after the save, ``settle_s`` apart. A
write that lands later lands after the verdict. With
``frame.database.recheck_after_s`` set, a verified write schedules a recheck,
and :func:`openadapt_flow.verifier.recheck` judges every change since the
post-save read. Each test below is one case, on SQLite, through the public
API. Records are made up.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.runtime.effects.frame import FrameSpec
from openadapt_flow.verifier import (
    Actor,
    Contract,
    RecheckNotDue,
    RecheckResult,
    RecheckState,
    VerifierError,
    WriteResult,
    capture,
    recheck,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")

SCHEMA = """
CREATE TABLE patient_data (id INTEGER PRIMARY KEY, pid INTEGER, fname TEXT,
  lname TEXT, email TEXT, phone_cell TEXT DEFAULT '', regdate TEXT);
CREATE TABLE history_data (id INTEGER PRIMARY KEY, pid INTEGER, date TEXT,
  tobacco TEXT DEFAULT '');
CREATE TABLE calendar (id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, title TEXT);
CREATE TABLE api_log (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT);
CREATE TABLE log (id INTEGER PRIMARY KEY, user TEXT, patient_id INTEGER,
  event TEXT, tbl TEXT);
INSERT INTO patient_data (id, pid, fname, lname, email, regdate)
  VALUES (1, 1, 'Wendell', 'Prior', 'w@example.test', '2026-01-01');
INSERT INTO history_data VALUES (1, 1, '2026-01-01', '');
"""


def _params(n: int) -> dict[str, str]:
    return {"fname": "Ada", "lname": f"Quill{n}", "email": f"ada{n}@example.test"}


@pytest.fixture(autouse=True)
def _used_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    used = tmp_path / "used"
    monkeypatch.setattr(verifier, "USED_DIR", used)
    return used


@pytest.fixture
def due_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recheck is due: the tests don't wait out the window."""

    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path


def _doc(db: Path, recheck_after_s: float | None = 60) -> dict[str, Any]:
    database: dict[str, Any] = {
        "dialect": "sqlite",
        "read": {"sqlite_database": str(db)},
        "allow_tables": ["log", "api_log"],
        "views": {"patient_data": ""},
        "new_row_fields": {
            "patient_data": ["pid", "regdate"],
            "history_data": ["date"],
        },
        "settle_s": 0,
    }
    if recheck_after_s is not None:
        database["recheck_after_s"] = recheck_after_s
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(db),
            "sql_query": "SELECT * FROM patient_data",
        },
        "record": {
            "name": "patient",
            "match": ["email", "lname"],
            "fields": {"fname": "fname", "lname": "lname", "email": "email"},
        },
        "frame": {
            "identity_field": "id",
            "side_records": [
                {
                    "surface": "history_data",
                    "link_field": "pid",
                    "of_surface": "patient_data",
                    "of_field": "pid",
                }
            ],
            "attribution": {
                "kind": "audit_read",
                "actor_field": "user",
                "self": ["bot"],
                "record_field": "patient_id",
                "record_key": "pid",
                "event_field": "event",
                "write_events": ["insert", "update", "delete"],
                "insert_events": ["insert"],
                "delete_events": ["delete"],
                "table_field": "tbl",
                "read": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT * FROM log",
                },
            },
            "database": database,
        },
    }


def _sql(db: Path, *statements: str) -> None:
    con = sqlite3.connect(db)
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


def _save_sql(n: int) -> list[str]:
    """The app's own save of patient ``n``: the chart, its history row and
    the bot's log lines."""

    p = _params(n)
    return [
        "INSERT INTO patient_data (id, pid, fname, lname, email, regdate) VALUES "
        f"({n}, {n}, '{p['fname']}', '{p['lname']}', '{p['email']}', '2026-10-29')",
        f"INSERT INTO history_data (id, pid, date) VALUES ({n}, {n}, '2026-10-29')",
        "INSERT INTO log (user, patient_id, event, tbl) VALUES "
        f"('bot', {n}, 'insert', 'patient_data'), "
        f"('bot', {n}, 'insert', 'history_data')",
    ]


def _save(contract: Contract, db: Path, n: int, *stray: str) -> WriteResult:
    before = capture(contract, _params(n))
    _sql(db, *_save_sql(n), *stray)
    return verify_write(contract, before, params=_params(n), actor=BOT)


def _first(tmp_path: Path, **doc: Any) -> tuple[Contract, Path, WriteResult]:
    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db, **doc))
    result = _save(contract, db, 2)
    assert result.verified, result.reasons
    return contract, db, result


def _recheck(
    contract: Contract, result: WriteResult | RecheckState, n: int = 2, **kw: Any
) -> RecheckResult:
    return recheck(contract, result, params=_params(n), actor=BOT, **kw)


# -- scheduling ----------------------------------------------------------------


def test_a_verified_write_says_when_its_recheck_is_due(tmp_path: Path) -> None:
    _, _, result = _first(tmp_path)
    schedule = result.evidence["frame"]["recheck"]
    assert result.recheck is not None
    assert result.recheck_due_at == result.receipt["recheck_due_at"]
    assert result.recheck_due_at == schedule["due_at"] == result.recheck.due_at
    assert schedule["compares"] == "database_since_post_save"
    assert schedule["after_s"] == 60
    saved = result.recheck.save(tmp_path / "recheck.json")
    assert os.stat(saved).st_mode & 0o777 == 0o600
    text = saved.read_text()
    # Keyed MACs only: no record values reach the disk.
    for value in ("Quill2", "ada2@example.test", "Wendell", "w@example.test"):
        assert value not in text
    assert "Quill2" not in repr(result.recheck)


def test_without_recheck_after_s_nothing_changes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db, recheck_after_s=None))
    result = _save(contract, db, 2)
    assert result.verified
    assert result.recheck is None and result.recheck_due_at is None
    assert "recheck_due_at" not in result.receipt
    assert "recheck" not in result.evidence["frame"]
    # Unset, the field stays out of the frame's digest.
    spec = contract.config.frame
    assert spec is not None and spec.database is not None
    assert "recheck_after_s" not in spec.database.model_dump()
    with pytest.raises(VerifierError, match="no recheck"):
        _recheck(contract, result)


def test_a_write_that_was_not_verified_has_nothing_to_recheck(tmp_path: Path) -> None:
    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db))
    stray = "UPDATE patient_data SET phone_cell = '555-0100' WHERE pid = 1"
    result = _save(contract, db, 2, stray)
    assert result.status == "refuted"
    assert result.recheck_due_at is None and "recheck_due_at" not in result.receipt
    with pytest.raises(VerifierError, match="no recheck"):
        _recheck(contract, result)


def test_a_recheck_before_its_due_time_is_refused(tmp_path: Path) -> None:
    contract, _, result = _first(tmp_path, recheck_after_s=3600)
    with pytest.raises(RecheckNotDue) as raised:
        _recheck(contract, result)
    assert raised.value.due_at == result.recheck_due_at
    assert raised.value.wait_s > 3000


def test_wait_sleeps_until_the_recheck_is_due(tmp_path: Path) -> None:
    contract, _, result = _first(tmp_path, recheck_after_s=1)
    late = _recheck(contract, result, wait=True)
    assert late.outcome == "CONFIRMED", late.reasons
    assert late.read_at >= late.due_at


# -- what a recheck catches and excuses ---------------------------------------------


def test_nothing_moved_since_the_verdict_confirms(
    tmp_path: Path, due_now: None
) -> None:
    contract, _, result = _first(tmp_path)
    late = _recheck(contract, result)
    assert late.outcome == "CONFIRMED" and late.verified and not late.late
    assert late.status == "verified"
    assert late.receipt_sha256 == result.receipt_sha256


@pytest.mark.parametrize(
    "collateral",
    [
        "UPDATE patient_data SET email = 'x@example.test' WHERE pid = 1",
        "UPDATE history_data SET tobacco = 'Never' WHERE pid = 2",
        "INSERT INTO calendar (pid, title) VALUES (2, 'New Patient')",
        "INSERT INTO history_data (id, pid, date) VALUES (9, 2, '2026-10-29')",
        "DELETE FROM history_data WHERE pid = 1",
    ],
)
def test_late_collateral_refutes_the_earlier_verdict(
    tmp_path: Path, due_now: None, collateral: str
) -> None:
    contract, db, result = _first(tmp_path)
    _sql(db, collateral)  # after the verdict: a background job, a trigger
    late = _recheck(contract, result)
    assert late.outcome == "REFUTED", collateral
    assert late.late and not late.verified and late.status == "refuted"
    assert late.evidence["collateral_count"] >= 1
    assert "no longer stands" in late.reasons[0]


def test_a_later_verified_write_excuses_its_own_rows(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, first = _first(tmp_path)
    second = _save(contract, db, 3)
    assert second.verified, second.reasons
    # Without the excuse, the next patient's rows are unexplained.
    assert _recheck(contract, first).outcome == "REFUTED"
    late = _recheck(contract, first, excused_by=[second])
    assert late.outcome == "CONFIRMED", late.reasons
    assert late.excused == 1


def test_a_later_write_excuses_only_its_own_rows(tmp_path: Path, due_now: None) -> None:
    contract, db, first = _first(tmp_path)
    second = _save(contract, db, 3)
    assert second.verified
    _sql(db, "UPDATE patient_data SET phone_cell = '555-0101' WHERE pid = 1")
    late = _recheck(contract, first, excused_by=[second])
    assert late.outcome == "REFUTED"


def test_a_later_refuted_write_accounts_for_what_it_reported(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, first = _first(tmp_path)
    stray = "UPDATE patient_data SET phone_cell = '555-0102' WHERE pid = 1"
    second = _save(contract, db, 3, stray)
    assert second.status == "refuted"  # reported, and stopped, against it
    late = _recheck(contract, first, excused_by=[second])
    assert late.outcome == "CONFIRMED", late.reasons
    # A change no later write accounts for still refutes the earlier one.
    _sql(db, "INSERT INTO calendar (pid, title) VALUES (1, 'Follow-up')")
    assert _recheck(contract, first, excused_by=[second]).outcome == "REFUTED"


def test_a_row_changed_again_after_the_later_write_is_not_excused(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, first = _first(tmp_path)
    stray = "UPDATE patient_data SET phone_cell = '555-0103' WHERE pid = 1"
    second = _save(contract, db, 3, stray)
    assert second.status == "refuted"
    _sql(db, "UPDATE patient_data SET phone_cell = '555-0104' WHERE pid = 1")
    assert _recheck(contract, first, excused_by=[second]).outcome == "REFUTED"


def test_another_actors_attributed_write_is_excused(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, first = _first(tmp_path)
    _sql(
        db,
        "UPDATE patient_data SET phone_cell = '555-0105' WHERE pid = 1",
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('nurse', 1, 'update', 'patient_data')",
    )
    late = _recheck(contract, first)
    assert late.outcome == "CONFIRMED", late.reasons
    assert late.evidence["concurrent_count"] == 1


def test_the_bots_own_unattributed_write_is_not_excused(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, first = _first(tmp_path)
    _sql(
        db,
        "UPDATE patient_data SET phone_cell = '555-0106' WHERE pid = 1",
        "INSERT INTO log (user, patient_id, event, tbl) "
        "VALUES ('bot', 1, 'update', 'patient_data')",
    )
    assert _recheck(contract, first).outcome == "REFUTED"


def test_reviewed_bookkeeping_is_excused(tmp_path: Path, due_now: None) -> None:
    contract, db, first = _first(tmp_path)
    _sql(db, "INSERT INTO api_log (note) VALUES ('token refresh')")
    late = _recheck(contract, first)
    assert late.outcome == "CONFIRMED", late.reasons


# -- fail-closed ------------------------------------------------------------------


def test_an_unreadable_database_is_uncertain(tmp_path: Path, due_now: None) -> None:
    contract, db, first = _first(tmp_path)
    db.rename(db.with_suffix(".moved"))
    late = _recheck(contract, first)
    assert late.outcome == "UNCERTAIN" and not late.verified
    assert late.status == "unconfirmed"
    assert late.frame == "not_checked"


def test_a_database_still_moving_is_uncertain(
    tmp_path: Path, due_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow import verify_run

    contract, db, first = _first(tmp_path)
    real = verify_run._read_database
    calls = {"n": 0}

    def moving(cfg: Any, salt: bytes, before: Any = None) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # between the two reads, a job writes
            _sql(db, "INSERT INTO calendar (pid, title) VALUES (1, 'x')")
        return real(cfg, salt, before=before)

    monkeypatch.setattr(verify_run, "_read_database", moving)
    assert _recheck(contract, first).outcome == "UNCERTAIN"


def test_a_saved_state_round_trips(tmp_path: Path, due_now: None) -> None:
    contract, db, first = _first(tmp_path)
    assert first.recheck is not None
    path = first.recheck.save(tmp_path / "first.json")
    second = _save(contract, db, 3)
    assert second.recheck is not None
    second_path = second.recheck.save(tmp_path / "second.json")
    loaded = RecheckState.load(path)
    later = RecheckState.load(second_path, params=_params(3))
    assert _recheck(contract, loaded, excused_by=[later]).outcome == "CONFIRMED"
    with pytest.raises(VerifierError, match="params"):
        _recheck(contract, loaded, excused_by=[RecheckState.load(second_path)])


@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: s["post_state"].update(read_at="2001-01-01T00:00:00Z"),
        lambda s: s["post_state"]["snapshot"]["tables"].pop("calendar"),
        lambda s: s.update(salt="00" * 32),
        lambda s: s["receipt"].update(outcome="verified", effect_verdict="REFUTED"),
        lambda s: s["evidence"]["frame"]["recheck"].update(due_at=None),
        lambda s: s["baseline"].update(baseline_read_at="2001-01-01T00:00:00Z"),
    ],
)
def test_a_mismatched_state_is_uncertain(
    tmp_path: Path, due_now: None, tamper: Any
) -> None:
    contract, _, first = _first(tmp_path)
    assert first.recheck is not None
    payload = json.loads(json.dumps(first.recheck.payload))
    tamper(payload)
    late = _recheck(contract, RecheckState(payload))
    assert late.outcome == "UNCERTAIN", late.reasons
    assert "can't be rechecked" in late.reasons[0]


def test_another_contract_actor_or_params_is_uncertain(
    tmp_path: Path, due_now: None
) -> None:
    contract, db, first = _first(tmp_path)
    other = Contract.from_dict(_doc(db, recheck_after_s=120))
    assert _recheck(other, first).outcome == "UNCERTAIN"
    assert _recheck(contract, first, n=7).outcome == "UNCERTAIN"
    assert (
        recheck(
            contract, first, params=_params(2), actor=Actor("agent", "someone-else")
        ).outcome
        == "UNCERTAIN"
    )


def test_an_unreadable_state_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(VerifierError):
        RecheckState.load(path)
    path.write_text(json.dumps({"schema_version": "openadapt.verify-recheck/v1"}))
    with pytest.raises(VerifierError):
        RecheckState.load(path)


def test_an_excuse_that_began_before_the_post_save_read_is_refused(
    tmp_path: Path, due_now: None
) -> None:
    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db))
    early = capture(contract, _params(3))  # before the first write's verdict
    time.sleep(1.1)  # whole-second times: a clear second apart
    first = _save(contract, db, 2)
    assert first.verified
    _sql(db, *_save_sql(3))
    second = verify_write(contract, early, params=_params(3), actor=BOT)
    with pytest.raises(VerifierError, match="started before"):
        _recheck(contract, first, excused_by=[second])


def test_an_excuse_by_another_actor_is_refused(tmp_path: Path, due_now: None) -> None:
    contract, db, first = _first(tmp_path)
    before = capture(contract, _params(3))
    _sql(db, *_save_sql(3))
    theirs = verify_write(
        contract, before, params=_params(3), actor=Actor("agent", "other-bot")
    )
    assert theirs.recheck is not None
    with pytest.raises(VerifierError, match="different actor"):
        _recheck(contract, first, excused_by=[theirs])


def test_frame_digest_is_unchanged_without_the_field() -> None:
    spec = FrameSpec.model_validate(
        {"database": {"dialect": "sqlite", "read": {"sqlite_database": "x"}}}
    )
    with_field = FrameSpec.model_validate(
        {
            "database": {
                "dialect": "sqlite",
                "read": {"sqlite_database": "x"},
                "recheck_after_s": 30,
            }
        }
    )
    assert spec.digest() != with_field.digest()
    assert "recheck_after_s" not in json.dumps(spec.model_dump())
