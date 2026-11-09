"""Synthetic temporal inventory controls; no real-app qualification claim."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow import verifier
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from openadapt_flow.runtime.effects.history import (
    FrameHistory,
    checkpoint_problem,
    instant,
    judge_history,
    judge_initial_history,
    read_history,
)
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier
from openadapt_flow.verifier import (
    Actor,
    Contract,
    MemoryLedger,
    begin_write,
    recheck,
    verify_write,
)
from tests.test_history_recheck import (
    DUE,
    KEY,
    SINCE,
    Session,
    _bundle,
    _event,
    _read,
    _spec,
    _sql_contract,
)
from tests.test_history_recheck import (
    database as database,
)


def _time(offset: int) -> str:
    return (instant(SINCE) + timedelta(seconds=offset)).isoformat()


def _reader(db: Path):
    spec = FrameHistory.model_validate(_sql_contract(db)["frame"]["history"])
    reader = SqlRecordVerifier(lambda: sqlite3.connect(db), "SELECT * FROM changes")
    return spec, lambda: read_history(spec, SINCE, lambda read: reader, KEY)


def _schema(db: Path) -> None:
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT); CREATE TABLE changes(event_id INTEGER PRIMARY KEY, record_id TEXT, operation TEXT, author TEXT, occurred_at TEXT); CREATE TABLE coverage(retained_since TEXT, through TEXT, event_count INTEGER); INSERT INTO coverage VALUES('2000-01-01T00:00:00Z','2026-11-05T00:03:00Z',0);"
        )


def _append(
    db: Path,
    event_id: int,
    at: str,
    operation: str = "UPDATE",
    record: str = "1",
    author: str | None = "bot",
) -> None:
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO changes VALUES(?,?,?,?,?)",
            (event_id, record, operation, author, at),
        )
        con.execute("UPDATE coverage SET event_count = event_count + 1")


@settings(max_examples=20, deadline=None)
@given(offset=st.integers(min_value=-864000, max_value=120))
def test_sql_inventory_never_discards_an_event_by_its_timestamp(offset: int) -> None:
    with TemporaryDirectory(prefix="history-inventory-") as directory:
        db = Path(directory) / "synthetic.sqlite"
        _schema(db)
        _append(db, 1, _time(offset))
        spec, read = _reader(db)
        checkpoint, raw = read()
        assert len(raw) == len(checkpoint["feeds"]["records"]["events"]) == 1
        assert checkpoint_problem(checkpoint, spec) is None
        assert "bot" not in json.dumps(checkpoint)


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE"])
@settings(max_examples=12, deadline=None)
@given(offset=st.integers(min_value=-864000, max_value=-1))
def test_new_backdated_identity_is_not_lost_from_late_judgment(
    operation: str, offset: int
) -> None:
    with TemporaryDirectory(prefix="history-arrival-") as directory:
        db = Path(directory) / "synthetic.sqlite"
        _schema(db)
        spec, read = _reader(db)
        before, _ = read()
        _append(db, 1, _time(offset), operation)
        now, raw = read()
        verdict = judge_history(spec, before, now, raw, {}, DUE, "bot")
        assert verdict.status == "refuted"
        assert len(verdict.collateral) == 1


@pytest.mark.parametrize("mutation", ["changed", "deleted"])
def test_known_prestart_event_is_retained_and_cannot_change(mutation: str) -> None:
    with TemporaryDirectory(prefix="history-retention-") as directory:
        db = Path(directory) / "synthetic.sqlite"
        _schema(db)
        _append(db, 1, _time(-60))
        spec, read = _reader(db)
        before, _ = read()
        unchanged, raw = read()
        assert judge_history(spec, before, unchanged, raw, {}, DUE, "bot").held
        with sqlite3.connect(db) as con:
            if mutation == "changed":
                con.execute("UPDATE changes SET operation='DELETE'")
            else:
                con.execute("DELETE FROM changes")
                con.execute("UPDATE coverage SET event_count=0")
        now, raw = read()
        assert (
            judge_history(spec, before, now, raw, {}, DUE, "bot").status
            == "not_checked"
        )


@pytest.mark.parametrize("kind", ["sql", "fhir"])
@pytest.mark.parametrize("offset", [-86400, -1])
@pytest.mark.parametrize("author", ["self", "unattributed", "other"])
def test_new_prestart_target_event_cannot_authorize_initial_write(
    kind: str, offset: int, author: str
) -> None:
    with TemporaryDirectory(prefix="history-attribution-") as directory:
        db = Path(directory) / "synthetic.sqlite"
        _schema(db)
        if kind == "sql":
            spec, read = _reader(db)
            before, _ = read()
            _append(
                db,
                1,
                _time(offset),
                author={"self": "bot", "unattributed": None, "other": "colleague"}[
                    author
                ],
            )
            now, raw = read()
            target = ("records", "1")
        else:
            spec = _spec()
            before, _, _ = _read(spec, [_bundle([])])
            entry = _event("saved", at=_time(offset))
            entry["resource"]["meta"]["author"]["reference"] = {
                "self": "actor/bot",
                "unattributed": None,
                "other": "actor/colleague",
            }[author]
            now, raw, _ = _read(spec, [_bundle([entry])])
            target = ("thing", "saved")
        verdict = judge_initial_history(spec, before, now, raw, {}, "bot", {target})
        assert verdict.status == "not_checked"
        assert not verdict.held


@pytest.mark.parametrize("which", ["before", "after"])
@pytest.mark.parametrize("marker", [None, "unknown-inventory"])
def test_legacy_or_unknown_sql_inventory_cannot_authorize_a_judgment(
    which: str, marker: str | None
) -> None:
    with TemporaryDirectory(prefix="history-legacy-") as directory:
        db = Path(directory) / "synthetic.sqlite"
        _schema(db)
        spec, read = _reader(db)
        before, _ = read()
        now, raw = read()
        target = before if which == "before" else now
        if marker is None:
            target["feeds"]["records"].pop("inventory")
        else:
            target["feeds"]["records"]["inventory"] = marker
        assert checkpoint_problem(target, spec) is not None
        assert (
            judge_history(spec, before, now, raw, {}, DUE, "bot").status
            == "not_checked"
        )
        assert (
            judge_initial_history(spec, before, now, raw, {}, "bot", set()).status
            == "not_checked"
        )


def test_history_cannot_join_different_attempt_boundaries() -> None:
    spec = _spec()
    before, _, _ = _read(spec, [_bundle([])])
    now = copy.deepcopy(before)
    now["since"] = _time(1)
    assert (
        judge_history(spec, before, now, {}, {}, DUE, "actor/bot").status
        == "not_checked"
    )
    assert (
        judge_initial_history(spec, before, now, {}, {}, "actor/bot", set()).status
        == "not_checked"
    )


def test_fhir_version_time_does_not_adopt_old_provenance_recorded_time() -> None:
    spec = _spec()
    spec.feeds["thing"].read["resource_type"] = "Provenance"
    entry = _event("saved")
    entry["resource"]["resourceType"] = "Provenance"
    entry["resource"]["recorded"] = "2020-01-01T00:00:00Z"
    entry["request"]["url"] = "Provenance/saved"
    session = Session([_bundle([entry])])
    reader = FhirEffectVerifier(
        "https://source.test/fhir",
        resource_type="Provenance",
        field_paths={"id": "id"},
        session=session,
    )
    now, raw = read_history(spec, SINCE, lambda read: reader, KEY)
    assert session.calls[0][0].endswith("/Provenance/_history")
    assert session.calls[0][1]["params"] == {"_since": SINCE, "_count": "1000"}
    row = next(iter(raw.values()))
    assert instant(row["occurred_at"]) > instant(SINCE)
    before = copy.deepcopy(now)
    before["feeds"]["thing"]["events"] = {}
    assert judge_initial_history(
        spec, before, now, raw, {}, "actor/bot", {("thing", "saved")}
    ).held


@pytest.mark.parametrize("fault", ["update", "delete", "roundtrip", "insert_other"])
@pytest.mark.parametrize("restart", [False, True])
def test_public_api_backdated_late_mutation_holds_the_ledger(
    database: Path, tmp_path: Path, fault: str, restart: bool
) -> None:
    contract = Contract.from_dict(_sql_contract(database))
    ledger = verifier.Ledger(MemoryLedger())
    actor = Actor("agent", "bot")
    pending = begin_write(
        contract,
        {"value": "expected"},
        actor=actor,
        ledger=ledger,
        workdir=tmp_path / "writes",
    )
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO records VALUES(1,'expected')")
    _append(database, 1, _time(1), "INSERT")
    result = verify_write(pending)
    assert result.verified and result.provisional and not result.final
    assert result.recheck is not None
    state = (
        verifier.RecheckState.load(
            result.recheck.save(tmp_path / "restart.json"), params={"value": "expected"}
        )
        if restart
        else result.recheck
    )
    with sqlite3.connect(database) as con:
        if fault == "delete":
            con.execute("DELETE FROM records WHERE id=1")
        elif fault == "insert_other":
            con.execute("INSERT INTO records VALUES(2,'unexpected')")
        else:
            con.execute("UPDATE records SET value='unexpected' WHERE id=1")
    _append(
        database,
        2,
        _time(-86400),
        "DELETE"
        if fault == "delete"
        else "INSERT"
        if fault == "insert_other"
        else "UPDATE",
        "2" if fault == "insert_other" else "1",
    )
    if fault == "roundtrip":
        with sqlite3.connect(database) as con:
            con.execute("UPDATE records SET value='expected' WHERE id=1")
        _append(database, 3, _time(-86400), "UPDATE")
    late = recheck(
        contract, state, params={"value": "expected"}, actor=actor, ledger=ledger
    )
    assert late.outcome == "REFUTED"
    assert not late.verified
    assert ledger.state(ledger.latest(result.write_key)) == "held"


def test_public_api_backdated_initial_roundtrip_cannot_hide_extra_versions(
    database: Path, tmp_path: Path
) -> None:
    contract = Contract.from_dict(_sql_contract(database))
    ledger = verifier.Ledger(MemoryLedger())
    actor = Actor("agent", "bot")
    pending = begin_write(
        contract,
        {"value": "expected"},
        actor=actor,
        ledger=ledger,
        workdir=tmp_path / "writes",
    )
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO records VALUES(1,'expected')")
    _append(database, 1, _time(1), "INSERT")
    for event_id, value in [(2, "unexpected"), (3, "expected")]:
        with sqlite3.connect(database) as con:
            con.execute("UPDATE records SET value=? WHERE id=1", (value,))
        _append(database, event_id, _time(-86400))
    result = verify_write(pending)
    assert not result.verified
    assert result.outcome is verifier.Outcome.REFUTED
    assert ledger.state(ledger.latest(result.write_key)) == "held"


def _initial(database: Path, tmp_path: Path, ledger: verifier.Ledger):
    contract = Contract.from_dict(_sql_contract(database))
    actor = Actor("agent", "bot")
    pending = begin_write(
        contract,
        {"value": "expected"},
        actor=actor,
        ledger=ledger,
        write_key="history-retry",
        workdir=tmp_path / "writes",
    )
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO records VALUES(1,'expected')")
    _append(database, 1, _time(1), "INSERT")
    result = verify_write(pending, delivered=False)
    assert result.verified and result.provisional and result.recheck is not None
    return contract, actor, result


@pytest.mark.parametrize("which", ["baseline", "post"])
@pytest.mark.parametrize("fault", ["legacy_inventory", "different_start"])
def test_recheck_preflight_validates_actual_spec_before_binding(
    database: Path, tmp_path: Path, which: str, fault: str
) -> None:
    from openadapt_flow import verify_run
    from openadapt_flow.runtime.effects.history import DETAIL

    ledger = verifier.Ledger(MemoryLedger())
    contract, actor, result = _initial(database, tmp_path, ledger)
    state = copy.deepcopy(result.recheck.payload)
    checkpoint = (
        state["baseline"]["state"]["detail"][DETAIL]
        if which == "baseline"
        else state["post_state"]["history"]
    )
    if fault == "legacy_inventory":
        checkpoint["feeds"]["records"].pop("inventory")
    else:
        checkpoint["since"] = _time(1)
    problem = verify_run.recheck_problem(
        contract.config,
        state,
        params={"value": "expected"},
        run_record=actor.run_record(),
    )
    assert problem == (
        "the history checkpoint lacks a complete retained-event inventory"
        if fault == "legacy_inventory"
        else "history checkpoints name different attempt boundaries"
    )


def test_begin_write_refuses_legacy_sql_checkpoint_before_handoff(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow import verify_run

    original = verify_run._read_history

    def legacy(*args, **kwargs):
        checkpoint, rows = original(*args, **kwargs)
        checkpoint["feeds"]["records"].pop("inventory")
        return checkpoint, rows

    monkeypatch.setattr(verify_run, "_read_history", legacy)
    with pytest.raises(verifier.BaselineUnreadable, match="complete change history"):
        begin_write(
            Contract.from_dict(_sql_contract(database)),
            {"value": "expected"},
            actor=Actor("agent", "bot"),
            ledger=MemoryLedger(),
            workdir=tmp_path / "writes",
        )
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


@pytest.mark.parametrize("kind", [None, "sqlite", "unknown"])
def test_unknown_feed_kind_cannot_default_to_fhir_inventory_rules(
    kind: str | None,
) -> None:
    with TemporaryDirectory(prefix="history-kind-") as directory:
        db = Path(directory) / "synthetic.sqlite"
        _schema(db)
        spec, read = _reader(db)
        checkpoint, _ = read()
        unknown = spec.model_copy(deep=True)
        if kind is None:
            unknown.feeds["records"].read.pop("kind")
        else:
            unknown.feeds["records"].read["kind"] = kind
        assert checkpoint_problem(checkpoint, unknown) is not None
        assert (
            judge_history(unknown, checkpoint, checkpoint, {}, {}, DUE, "bot").status
            == "not_checked"
        )


def test_opaque_feed_name_does_not_change_sql_inventory_requirements() -> None:
    with TemporaryDirectory(prefix="history-name-") as directory:
        db = Path(directory) / "synthetic.sqlite"
        _schema(db)
        doc = _sql_contract(db)["frame"]["history"]
        doc["feeds"]["opaque-source"] = doc["feeds"].pop("records")
        doc["surfaces"][""] = "opaque-source"
        spec = FrameHistory.model_validate(doc)
        reader = SqlRecordVerifier(lambda: sqlite3.connect(db), "SELECT * FROM changes")
        checkpoint, _ = read_history(spec, SINCE, lambda read: reader, KEY)
        assert checkpoint_problem(checkpoint, spec) is None
        checkpoint["feeds"]["opaque-source"].pop("inventory")
        assert checkpoint_problem(checkpoint, spec) is not None


@pytest.mark.parametrize("proof", ["current", "legacy", "wrong_model", "wrong_frame"])
@pytest.mark.parametrize("restart", [False, True])
def test_cached_final_handoff_requires_current_writer_produced_history_proof(
    database: Path, tmp_path: Path, proof: str, restart: bool
) -> None:
    root = tmp_path / "durable-ledger"
    ledger = verifier.Ledger(verifier.FileLedger(root))
    contract, actor, result = _initial(database, tmp_path, ledger)
    if proof == "legacy":
        # Genuine old journal shape, deliberately without a new-model tag.
        # This is a negative fixture, never an upgraded accepted proof.
        ledger.rechecked(
            result.write_key,
            result.write_id,
            outcome="CONFIRMED",
            read_at=DUE,
            receipt_sha256=result.receipt_sha256,
        )
    else:
        late = recheck(
            contract, result, params={"value": "expected"}, actor=actor, ledger=ledger
        )
        assert late.verified and late.final
        event = ledger.latest(result.write_key).rechecked
        assert event is not None
        coverage = copy.deepcopy(event["history_coverage"])
        assert coverage["model"] == "all-retained-events/v1"
        assert coverage["frame_sha256"] == contract.config.frame.digest()
        if proof != "current":
            coverage["model" if proof == "wrong_model" else "frame_sha256"] = "wrong"
            ledger.rechecked(
                result.write_key,
                result.write_id,
                outcome="CONFIRMED",
                read_at=DUE,
                receipt_sha256=result.receipt_sha256,
                history_coverage=coverage,
            )
    if restart:
        ledger = verifier.Ledger(verifier.FileLedger(root))
    before = ledger.backend.events(result.write_key)
    if proof == "current":
        with pytest.raises(verifier.AlreadyWritten) as caught:
            begin_write(
                contract,
                {"value": "expected"},
                actor=actor,
                ledger=ledger,
                write_key="history-retry",
                workdir=tmp_path / "retry",
            )
        assert caught.value.result["verified"] is True
        assert caught.value.result["final"] is True
        assert caught.value.result["provisional"] is False
        assert caught.value.result["not_sent_again"] is True
    else:
        with pytest.raises(verifier.WriteHeld) as caught:
            begin_write(
                contract,
                {"value": "expected"},
                actor=actor,
                ledger=ledger,
                write_key="history-retry",
                workdir=tmp_path / "retry",
            )
        assert not isinstance(caught.value, verifier.AlreadyWritten)
        assert ledger.backend.events(result.write_key) == before
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT id,value FROM records").fetchall() == [
            (1, "expected")
        ]


def test_generic_restart_loading_is_shape_only_and_never_confirms_legacy_state(
    database: Path, tmp_path: Path
) -> None:
    ledger = verifier.Ledger(MemoryLedger())
    contract, actor, result = _initial(database, tmp_path, ledger)
    payload = copy.deepcopy(result.recheck.payload)
    payload["post_state"]["history"]["feeds"]["records"].pop("inventory")
    path = tmp_path / "legacy-shape.json"
    path.write_text(json.dumps(payload))
    loaded = verifier.RecheckState.load(path, params={"value": "expected"})
    # Deserialization has no contract. This altered fixture is also unissued;
    # its public refusal is not credit for migrating a genuine old receipt.
    late = recheck(
        contract, loaded, params={"value": "expected"}, actor=actor, ledger=ledger
    )
    assert not late.verified and not late.final


@pytest.mark.parametrize("which", ["baseline", "post"])
def test_direct_recheck_run_refuses_legacy_inventory_before_read(
    database: Path, tmp_path: Path, which: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow import verify_run
    from openadapt_flow.runtime.effects.history import DETAIL

    contract, actor, result = _initial(
        database, tmp_path, verifier.Ledger(MemoryLedger())
    )
    state = copy.deepcopy(result.recheck.payload)
    checkpoint = (
        state["baseline"]["state"]["detail"][DETAIL]
        if which == "baseline"
        else state["post_state"]["history"]
    )
    checkpoint["feeds"]["records"].pop("inventory")
    calls = []

    def forbidden_read(*args, **kwargs):
        calls.append(True)
        raise AssertionError("a legacy checkpoint must refuse before a new read")

    monkeypatch.setattr(verify_run, "_read_history", forbidden_read)
    with pytest.raises(verify_run.VerifyRunError, match="retained-event inventory"):
        verify_run.recheck_run(
            contract.config,
            state,
            params={"value": "expected"},
            run_record=actor.run_record(),
        )
    assert calls == []
