"""A growing audit log must refuse an oversized frame, never pass a prefix."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from openadapt_flow.rollback_state import DatabaseReader, StateUnreadable
from openadapt_flow.runtime.effects.db_snapshot import (
    DEFAULT_READ_ROW_BUDGET,
    SnapshotError,
    SnapshotTooLarge,
    take_snapshot,
)
from openadapt_flow.runtime.effects.frame import FrameDatabase
from openadapt_flow.verifier import BaselineUnreadable, capture_database
from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run
from tests.test_frame_database import NEW, _db, _doc, _run, _save

KEY = b"k" * 32


def _store(tmp_path: Path) -> tuple[Callable[[], sqlite3.Connection], list[str]]:
    path = tmp_path / "audit.sqlite"
    with sqlite3.connect(path) as con:
        con.executescript(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, body TEXT);"
            "INSERT INTO audit VALUES (1, 'old'), (2, 'old'), (3, 'old');"
        )
    queries: list[str] = []

    def connect() -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.set_trace_callback(queries.append)
        return con

    return connect, queries


def test_at_the_budget_every_row_is_hashed_even_above_max_rows(tmp_path: Path) -> None:
    connect, _ = _store(tmp_path)
    snap = take_snapshot(connect, "sqlite", key=KEY, max_rows=1, read_row_budget=3)
    assert snap.tables["audit"].count == len(snap.tables["audit"].rows) == 3
    assert snap.tables["audit"].cell_columns == []


@pytest.mark.parametrize("modes", [{}, {"audit": "append_only"}])
def test_oversized_tables_refuse_before_fetching_their_payloads(
    tmp_path: Path, modes: dict[str, Any]
) -> None:
    connect, queries = _store(tmp_path)
    with pytest.raises(SnapshotTooLarge, match="frame too large.*budget of 2"):
        take_snapshot(connect, "sqlite", key=KEY, modes=modes, read_row_budget=2)
    assert not any("json_object(" in q.lower() for q in queries)
    assert queries[-1] == "ROLLBACK"


def test_the_default_budget_is_finite(tmp_path: Path) -> None:
    connect, queries = _store(tmp_path)
    with sqlite3.connect(tmp_path / "audit.sqlite") as con:
        con.execute("DELETE FROM audit")
        con.execute(
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<?) "
            "INSERT INTO audit SELECT x, 'synthetic' FROM n",
            (DEFAULT_READ_ROW_BUDGET + 1,),
        )
    with pytest.raises(SnapshotTooLarge, match="frame too large"):
        take_snapshot(connect, "sqlite", key=KEY)
    assert not any("json_object(" in q.lower() for q in queries)


@pytest.mark.parametrize("budget,refused", [(6, False), (5, True)])
def test_auxiliary_audit_rows_share_the_budget(
    tmp_path: Path, budget: int, refused: bool
) -> None:
    connect, queries = _store(tmp_path)
    args: dict[str, Any] = {
        "key": KEY,
        "read_row_budget": budget,
        "extra_queries": {"audit": "SELECT * FROM audit;"},
    }
    if refused:
        with pytest.raises(SnapshotTooLarge):
            take_snapshot(connect, "sqlite", **args)
    else:
        snap = take_snapshot(connect, "sqlite", **args)
        assert len(snap.extra["audit"]) == len(snap.tables["audit"].rows) == 3
    assert any("AS frame_read_rows LIMIT" in q for q in queries)


def test_multiple_auxiliary_queries_do_not_each_get_a_fresh_budget(
    tmp_path: Path,
) -> None:
    connect, _ = _store(tmp_path)
    with pytest.raises(SnapshotTooLarge):
        take_snapshot(
            connect,
            "sqlite",
            key=KEY,
            read_row_budget=8,
            extra_queries={
                "first": "SELECT * FROM audit",
                "second": "SELECT * FROM audit",
            },
        )


def test_an_auxiliary_query_is_bounded_before_the_driver_buffers_its_result(
    tmp_path: Path,
) -> None:
    base, _ = _store(tmp_path)
    visited: list[int] = []

    def connect() -> sqlite3.Connection:
        con = base()
        con.create_function("visit", 1, lambda n: visited.append(n) or n)
        return con

    with pytest.raises(SnapshotTooLarge):
        take_snapshot(
            connect,
            "sqlite",
            key=KEY,
            read_row_budget=5,
            extra_queries={
                "audit": "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL "
                "SELECT x+1 FROM n WHERE x<10000) SELECT visit(x) AS id FROM n"
            },
        )
    # Three table rows leave room for two audit rows. The third is the
    # over-budget sentinel; the other 9,997 rows never reach the driver.
    assert visited == [1, 2, 3]


def test_skipped_tables_do_not_consume_the_budget_but_audit_queries_still_do(
    tmp_path: Path,
) -> None:
    connect, _ = _store(tmp_path)
    snap = take_snapshot(
        connect, "sqlite", key=KEY, modes={"audit": "skip"}, read_row_budget=1
    )
    assert snap.tables["audit"].rows == {}
    with pytest.raises(SnapshotTooLarge):
        take_snapshot(
            connect,
            "sqlite",
            key=KEY,
            modes={"audit": "skip"},
            read_row_budget=1,
            extra_queries={"audit": "SELECT * FROM audit"},
        )


def test_a_log_that_grows_past_the_budget_never_verifies(tmp_path: Path) -> None:
    def grow(con: sqlite3.Connection) -> None:
        con.executemany(
            "INSERT INTO log (user, event) VALUES ('viewer', ?)", [("view",)] * 20
        )

    result = _run(tmp_path, grow, read_row_budget=11, settle_s=0)
    assert result["receipt"]["checked"]["frame"] == "not_checked"
    assert result["receipt"]["outcome"] != "verified"
    assert result["evidence"]["frame"]["database"]["read_status"] == "too_large"


def test_growth_during_the_settling_read_is_also_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("openadapt_flow.verify_run")

    def grow(_: float) -> None:
        with sqlite3.connect(tmp_path / "app.sqlite") as con:
            con.executemany(
                "INSERT INTO log (user, event) VALUES ('viewer', ?)", [("view",)] * 20
            )

    monkeypatch.setattr(module.time, "sleep", grow)
    result = _run(tmp_path, read_row_budget=11, settle_s=0)
    assert result["receipt"]["checked"]["frame"] == "not_checked"
    assert result["receipt"]["outcome"] != "verified"
    assert result["evidence"]["frame"]["database"]["read_status"] == "too_large"


def test_a_clean_save_at_the_combined_budget_still_verifies(tmp_path: Path) -> None:
    result = _run(tmp_path, read_row_budget=11, settle_s=0)
    assert result["receipt"]["outcome"] == "verified"
    assert result["receipt"]["checked"]["frame"] == "held"


@pytest.mark.parametrize(
    "sql", ["UPDATE log SET event='forged' WHERE id=1", "DELETE FROM log WHERE id=1"]
)
def test_an_old_audit_row_cannot_be_changed_behind_new_appends(
    tmp_path: Path, sql: str
) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO log (id,user,patient_id,event,tbl) VALUES (1,'viewer',1,'view','patient_data')"
        )
    cfg = parse_verify_config(_doc(db, read_row_budget=20, settle_s=0))
    capture_baseline(cfg, NEW, tmp_path / "baseline.json")
    with sqlite3.connect(db) as con:
        _save(con)
        con.execute(sql)
    result = verify_run(
        cfg,
        params=NEW,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert result["receipt"]["outcome"] != "verified"


def test_an_agent_write_into_the_audit_table_still_refutes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        lambda con: con.execute(
            "INSERT INTO log (user,patient_id,event,tbl) VALUES ('bot',99,'insert','log')"
        ),
        read_row_budget=20,
        settle_s=0,
    )
    assert result["receipt"]["checked"]["frame"] == "refuted"
    assert result["receipt"]["outcome"] != "verified"


def test_an_oversized_baseline_does_not_retry_the_audit_without_a_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("openadapt_flow.verify_run")

    def unbounded_retry(*args: Any, **kw: Any) -> None:
        pytest.fail(
            "failed bounded snapshot must not fall back to an unbounded audit read"
        )

    monkeypatch.setattr(module, "_read_audit", unbounded_retry)
    result = _run(tmp_path, read_row_budget=4, settle_s=0)
    assert result["receipt"]["checked"]["frame"] == "not_checked"
    assert result["receipt"]["outcome"] != "verified"
    assert result["evidence"]["frame"]["database"]["read_status"] == "too_large"


def test_snapshot_and_rollback_apis_share_the_budget(tmp_path: Path) -> None:
    connect, _ = _store(tmp_path)
    with pytest.raises(BaselineUnreadable, match="frame too large"):
        capture_database(connect, "sqlite", key=KEY, read_row_budget=2)
    database = FrameDatabase(
        dialect="sqlite",
        read={"sqlite_database": str(tmp_path / "audit.sqlite")},
        read_row_budget=2,
    )
    reader = DatabaseReader.from_frame_database(database)
    assert reader.read_row_budget == 2
    with pytest.raises(StateUnreadable) as exc:
        reader.read()
    assert isinstance(exc.value.__cause__.__cause__, SnapshotTooLarge)


@pytest.mark.parametrize("budget", [0, -1])
def test_an_invalid_budget_is_rejected_before_connecting(budget: int) -> None:
    def connect() -> Any:
        pytest.fail("invalid row budget must not open the database")

    with pytest.raises(SnapshotError, match="at least 1"):
        take_snapshot(connect, "sqlite", key=KEY, read_row_budget=budget)
    with pytest.raises(ValueError):
        FrameDatabase(
            dialect="sqlite",
            read={"sqlite_database": "synthetic.sqlite"},
            read_row_budget=budget,
        )


def test_the_default_budget_preserves_old_contract_digests_and_custom_budgets_are_bound() -> (
    None
):
    database = FrameDatabase(
        dialect="sqlite", read={"sqlite_database": "synthetic.sqlite"}
    )
    payload = database.model_dump(mode="json")
    assert "read_row_budget" not in payload
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # Captured from the model on main before read_row_budget existed.
    assert (
        hashlib.sha256(canonical.encode()).hexdigest()
        == "029545e0fc3b46632f43ee16a4d62faab9cb3aa2bd9462fd709887b708b84867"
    )
    changed = FrameDatabase(**{**payload, "read_row_budget": 42}).model_dump(
        mode="json"
    )
    assert changed["read_row_budget"] == 42
    assert FrameDatabase(**changed).model_dump(mode="json") == changed


def test_a_recheck_over_budget_cannot_confirm_the_earlier_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow import verifier
    from tests.test_verifier_recheck import BOT, _db, _doc, _params, _save

    monkeypatch.setattr(verifier, "USED_DIR", tmp_path / "used")
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0.0)
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    db = _db(tmp_path)
    doc = _doc(db)
    doc["frame"]["database"]["read_row_budget"] = 10
    contract = verifier.Contract.from_dict(doc)
    first = _save(contract, db, 2)
    assert first.verified, first.reasons
    with sqlite3.connect(db) as con:
        con.executemany("INSERT INTO api_log (note) VALUES (?)", [("synthetic",)] * 20)
    late = verifier.recheck(contract, first, params=_params(2), actor=BOT)
    assert late.outcome == "UNCERTAIN"
    assert "frame too large" in " ".join(late.reasons)


def test_the_public_write_result_explains_an_oversized_frame(tmp_path: Path) -> None:
    from openadapt_flow.verifier import Actor, Contract, begin_write, verify_write

    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db, read_row_budget=11, settle_s=0))
    actor = Actor(kind="agent", id="bot")
    before = begin_write(contract, NEW, actor=actor)
    with sqlite3.connect(db) as con:
        _save(con)
        con.executemany(
            "INSERT INTO log (user,event) VALUES ('viewer',?)", [("view",)] * 20
        )
    result = verify_write(before)
    assert not result.verified
    assert result.checked["frame"] == "not_checked"
    assert any(
        "frame too large: row read budget of 11 exceeded" in r for r in result.reasons
    )
    assert result.evidence["frame"]["database"] == {
        "read_status": "too_large",
        "read_row_budget": 11,
    }
