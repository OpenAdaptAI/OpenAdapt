"""Synthetic history contract controls; real-app qualification is separate."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from openadapt_flow.runtime.effects.history import (
    FrameHistory,
    checkpoint_problem,
    judge_history,
    judge_initial_history,
    read_history,
)
from openadapt_flow.verifier import (
    Actor,
    Contract,
    MemoryLedger,
    begin_write,
    recheck,
    verify_write,
)

SINCE = "2026-11-05T00:00:00Z"
DUE = "2026-11-05T00:02:00Z"
KEY = b"history-synthetic-key" * 2


def _spec(**changes: Any) -> FrameHistory:
    doc = {
        "feeds": {
            "thing": {
                "read": {
                    "kind": "fhir",
                    "base_url": "https://source.test/fhir",
                    "resource_type": "Basic",
                    "field_paths": {"id": "id"},
                },
                "author_path": "resource.meta.author.reference",
                "author_server_set": True,
            }
        },
        "surfaces": {"": "thing"},
        "quiet_s": 1,
        "setup_evidence_sha256": "sha256:" + "a" * 64,
        "self": ["actor/bot"],
    }
    doc.update(changes)
    return FrameHistory.model_validate(doc)


def _event(
    record: str,
    version: str = "1",
    author: str = "actor/bot",
    method: str = "PUT",
    at: str = "2026-11-05T00:00:01Z",
) -> dict[str, Any]:
    entry = {
        "resource": {
            "resourceType": "Basic",
            "id": record,
            "meta": {
                "versionId": version,
                "lastUpdated": at,
                "author": {"reference": author},
            },
        },
        "request": {"method": method, "url": f"Basic/{record}"},
        "response": {"lastModified": at},
    }
    if method == "DELETE":
        entry.pop("resource")
    return entry


def _bundle(entries: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "history",
        "total": len(entries),
        "timestamp": "2026-11-05T00:03:00Z",
        "entry": entries,
        **changes,
    }


class Session:
    def __init__(self, bundles: list[dict[str, Any]]) -> None:
        self.bundles = bundles
        self.calls: list[Any] = []

    def get(self, url: str, **kw: Any) -> Any:
        self.calls.append((url, kw))
        bundle = self.bundles.pop(0)
        return SimpleNamespace(
            status_code=200,
            url=url,
            headers={"Date": "Thu, 05 Nov 2026 00:03:00 GMT"},
            json=lambda: bundle,
        )


def _read(
    spec: FrameHistory, bundles: list[dict[str, Any]]
) -> tuple[Any, Any, Session]:
    session = Session(bundles)
    reader = FhirEffectVerifier(
        "https://source.test/fhir",
        resource_type="Basic",
        field_paths={"id": "id"},
        session=session,
    )
    checkpoint, raw = read_history(spec, SINCE, lambda read: reader, KEY)
    return checkpoint, raw, session


def test_fhir_reads_whole_type_all_pages_and_seals() -> None:
    spec = _spec()
    first = _bundle(
        [_event("a")],
        total=2,
        link=[
            {
                "relation": "next",
                "url": "https://source.test/fhir/Basic/_history?page=2",
            }
        ],
    )
    state, raw, session = _read(spec, [first, _bundle([_event("b")], total=2)])
    assert len(raw) == 2
    assert session.calls[0][0].endswith("/Basic/_history")
    assert session.calls[0][1]["params"] == {"_since": SINCE, "_count": "1000"}
    assert session.calls[1][1]["params"] is None
    assert "actor/bot" not in json.dumps(state)
    assert checkpoint_problem(state) is None


@pytest.mark.parametrize(
    "fault", ["update_other", "new_version", "delete_other", "update_revert"]
)
def test_new_versions_and_resource_less_tombstones_refute(fault: str) -> None:
    spec = _spec()
    original = _event("saved")
    old, _, _ = _read(spec, [_bundle([original])])
    events = [original]
    if fault == "delete_other":
        events.append(_event("other", method="DELETE", at="2026-11-05T00:01:00Z"))
    elif fault == "update_revert":
        events.extend([_event("other", "2"), _event("other", "3")])
    else:
        events.append(_event("saved" if fault == "new_version" else "other", "2"))
    now, raw, _ = _read(spec, [_bundle(events)])
    verdict = judge_history(spec, old, now, raw, {}, DUE, "actor/bot")
    assert verdict.status == "refuted"
    assert verdict.collateral


def test_authoritative_other_user_edit_does_not_refute() -> None:
    spec = _spec()
    old, _, _ = _read(spec, [_bundle([_event("saved")])])
    now, raw, _ = _read(
        spec, [_bundle([_event("saved"), _event("saved", "2", "actor/colleague")])]
    )
    result = judge_history(spec, old, now, raw, {}, DUE, "actor/bot")
    assert result.held
    assert len(result.concurrent) == 1


def test_untrusted_actor_or_executor_alias_never_excuses() -> None:
    spec = _spec()
    old, _, _ = _read(spec, [_bundle([])])
    now, raw, _ = _read(spec, [_bundle([_event("a", author="actor/executor")])])
    assert (
        judge_history(spec, old, now, raw, {}, DUE, "actor/executor").status
        == "refuted"
    )
    untrusted = spec.model_copy(deep=True)
    untrusted.feeds["thing"].author_server_set = False
    now, raw, _ = _read(untrusted, [_bundle([_event("a", author="actor/colleague")])])
    assert (
        judge_history(untrusted, old, now, raw, {}, DUE, "actor/bot").status
        == "refuted"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "truncated",
        "searchset",
        "no_total",
        "cycle",
        "cross_origin",
        "duplicate",
        "bad_next",
        "future_event",
    ],
)
def test_incomplete_fhir_history_never_confirms(mutation: str) -> None:
    bundle = _bundle([_event("a")])
    if mutation == "truncated":
        bundle["total"] = 2
    elif mutation == "searchset":
        bundle["type"] = "searchset"
    elif mutation == "no_total":
        bundle.pop("total")
    elif mutation == "cycle":
        bundle["link"] = [
            {"relation": "next", "url": "https://source.test/fhir/Basic/_history"}
        ]
    elif mutation == "cross_origin":
        bundle["link"] = [
            {"relation": "next", "url": "https://elsewhere.test/Basic/_history"}
        ]
    elif mutation == "duplicate":
        bundle = _bundle([_event("a"), _event("a")])
    elif mutation == "bad_next":
        bundle["link"] = [{"relation": "next", "url": None}]
    else:
        bundle = _bundle([_event("a", at="2026-11-05T00:04:00Z")])
    with pytest.raises((ValueError, TypeError)):
        _read(_spec(), [bundle])


def test_disappearing_changed_or_stale_history_never_confirms() -> None:
    spec = _spec()
    old, _, _ = _read(spec, [_bundle([_event("a")])])
    for bundle in [
        _bundle([]),
        _bundle([_event("a", author="actor/other")]),
        _bundle([_event("a")], timestamp="2026-11-05T00:01:00Z"),
    ]:
        now, raw, _ = _read(spec, [bundle])
        assert (
            judge_history(spec, old, now, raw, {}, DUE, "actor/bot").status
            == "not_checked"
        )


def _sql_contract(db: Path) -> dict[str, Any]:
    read = {
        "kind": "sql",
        "sqlite_database": str(db),
        "sql_query": "SELECT * FROM changes",
    }
    return {
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(db),
            "sql_query": "SELECT * FROM records",
        },
        "record": {"name": "record", "match": ["value"], "fields": {"value": "value"}},
        "frame": {
            "identity_field": "id",
            "history": {
                "feeds": {
                    "records": {
                        "read": read,
                        "dialect": "sqlite",
                        "coverage_query": "SELECT retained_since, through, event_count FROM coverage",
                        "author_path": "author",
                        "author_server_set": True,
                    }
                },
                "surfaces": {"": "records"},
                "quiet_s": 1,
                "setup_evidence_sha256": "sha256:" + "a" * 64,
                "self": ["bot"],
            },
        },
    }


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setattr(verifier, "USED_DIR", tmp_path / "used")
    monkeypatch.setattr(verifier, "seconds_until", lambda due: 0)
    from openadapt_flow import verify_run

    monkeypatch.setattr(verify_run, "_now", lambda: SINCE)
    db = tmp_path / "app.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT); CREATE TABLE changes(event_id INTEGER PRIMARY KEY, record_id TEXT, operation TEXT, author TEXT, occurred_at TEXT); CREATE TABLE coverage(retained_since TEXT, through TEXT, event_count INTEGER); INSERT INTO coverage VALUES('2000-01-01T00:00:00Z','2026-11-05T00:03:00Z',0);"
        )
    return db


def test_sql_public_api_provisional_late_hold(database: Path, tmp_path: Path) -> None:
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
        con.execute(
            "INSERT INTO changes VALUES(1,'1','INSERT','bot',?)",
            ("2026-11-05T00:00:01Z",),
        )
        con.execute("UPDATE coverage SET event_count=1")
    result = verify_write(pending)
    assert result.verified and result.provisional and not result.final
    assert result.to_dict()["final"] is False
    assert len(ledger.rechecks_owed()) == 1
    assert result.recheck is not None
    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO changes VALUES(2,'2','UPDATE','bot',?)",
            ("2026-11-05T00:00:01Z",),
        )
        con.execute("UPDATE coverage SET event_count=2")
    late = recheck(contract, result, params={"value": "expected"}, actor=actor)
    assert late.outcome == "REFUTED"
    assert not ledger.rechecks_owed()
    assert ledger.state(ledger.latest(result.write_key)) == "held"


def test_sql_clean_recheck_confirms_without_changing_the_store(
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
        con.execute(
            "INSERT INTO changes VALUES(1,'1','INSERT','bot',?)",
            ("2026-11-05T00:00:01Z",),
        )
        con.execute("UPDATE coverage SET event_count=1")
    initial = verify_write(pending)
    assert initial.verified and initial.provisional and not initial.final
    assert initial.to_dict()["final"] is False
    assert len(ledger.rechecks_owed()) == 1
    with sqlite3.connect(database) as con:
        before = list(con.iterdump())
    late = recheck(contract, initial, params={"value": "expected"}, actor=actor)
    assert late.verified and late.final
    assert late.frame == "held" and late.read_at
    assert not ledger.rechecks_owed()
    with sqlite3.connect(database) as con:
        assert list(con.iterdump()) == before


def test_sql_incomplete_coverage_prevents_provisional_success(
    database: Path, tmp_path: Path
) -> None:
    contract = Contract.from_dict(_sql_contract(database))
    pending = begin_write(
        contract,
        {"value": "expected"},
        actor=Actor("agent", "bot"),
        ledger=MemoryLedger(),
        workdir=tmp_path / "writes",
    )
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO records VALUES(1,'expected')")
        con.execute("UPDATE coverage SET event_count=1")
    result = verify_write(pending)
    assert not result.verified and result.outcome.value == "UNCERTAIN"
    assert not result.provisional and not result.final
    assert result.to_dict()["final"] is False


def test_history_contract_requires_every_read_surface(database: Path) -> None:
    doc = _sql_contract(database)
    doc["frame"]["history"]["surfaces"] = {"wrong": "records"}
    with pytest.raises(ValueError, match="every verifier surface"):
        Contract.from_dict(doc)


def test_version_read_entries_require_matching_version_identity() -> None:
    entry = _event("a")
    entry["request"] = {"method": "GET", "url": "Basic/a/_history/1"}
    old, _, _ = _read(_spec(), [_bundle([])])
    now, raw, _ = _read(_spec(), [_bundle([entry])])
    assert (
        judge_history(_spec(), old, now, raw, {}, DUE, "actor/bot").status == "refuted"
    )
    entry["request"]["url"] = "Basic/a/_history/different"
    with pytest.raises(ValueError):
        _read(_spec(), [_bundle([entry])])


def test_mutated_resource_with_same_version_never_confirms() -> None:
    spec = _spec()
    entry = _event("a")
    old, _, _ = _read(spec, [_bundle([entry])])
    entry["resource"]["code"] = {"text": "changed history"}
    now, raw, _ = _read(spec, [_bundle([entry])])
    assert (
        judge_history(spec, old, now, raw, {}, DUE, "actor/bot").status == "not_checked"
    )


def test_initial_history_proves_target_and_all_collateral() -> None:
    spec = _spec()
    old, _, _ = _read(spec, [_bundle([])])
    now, raw, _ = _read(spec, [_bundle([_event("saved")])])
    assert judge_initial_history(
        spec, old, now, raw, {}, "actor/bot", {("thing", "saved")}
    ).held
    now, raw, _ = _read(spec, [_bundle([_event("saved"), _event("other")])])
    assert (
        judge_initial_history(
            spec, old, now, raw, {}, "actor/bot", {("thing", "saved")}
        ).status
        == "refuted"
    )
    now, raw, _ = _read(spec, [_bundle([_event("saved"), _event("saved", "2")])])
    assert (
        judge_initial_history(
            spec, old, now, raw, {}, "actor/bot", {("thing", "saved")}
        ).status
        == "refuted"
    )
    now, raw, _ = _read(spec, [_bundle([])])
    assert (
        judge_initial_history(
            spec, old, now, raw, {}, "actor/bot", {("thing", "saved")}
        ).status
        == "not_checked"
    )


def test_colleague_fix_before_provisional_cannot_hide_wrong_agent_version() -> None:
    spec = _spec()
    old, _, _ = _read(spec, [_bundle([])])
    now, raw, _ = _read(
        spec, [_bundle([_event("saved"), _event("saved", "2", "actor/colleague")])]
    )
    assert (
        judge_initial_history(
            spec, old, now, raw, {}, "actor/bot", {("thing", "saved")}
        ).status
        == "refuted"
    )


def test_sql_history_restart_and_legitimate_late_target_edit(
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
    at = "2026-11-05T00:00:01Z"
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO records VALUES(1,'expected')")
        con.execute("INSERT INTO changes VALUES(1,'1','INSERT','bot',?)", (at,))
        con.execute("UPDATE coverage SET event_count=1")
    result = verify_write(pending)
    assert result.provisional and result.recheck is not None
    path = result.recheck.save(tmp_path / "restart.json")
    restored = verifier.RecheckState.load(path, params={"value": "expected"})
    with sqlite3.connect(database) as con:
        con.execute("UPDATE records SET value='colleague change' WHERE id=1")
        con.execute("INSERT INTO changes VALUES(2,'1','UPDATE','colleague',?)", (at,))
        con.execute("UPDATE coverage SET event_count=2")
    late = recheck(
        contract, restored, params={"value": "expected"}, actor=actor, ledger=ledger
    )
    assert late.verified and late.final
    assert not ledger.rechecks_owed()


def test_history_baseline_failure_blocks_before_write(
    database: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(database) as con:
        con.execute("UPDATE coverage SET event_count=1")
    with pytest.raises(verifier.BaselineUnreadable, match="complete change history"):
        begin_write(
            Contract.from_dict(_sql_contract(database)),
            {"value": "expected"},
            actor=Actor("agent", "bot"),
            ledger=MemoryLedger(),
            workdir=tmp_path / "writes",
        )


def test_http_date_precision_does_not_extend_final_coverage() -> None:
    spec = _spec()
    bundle = _bundle([_event("a", at="2026-11-05T00:03:00.500Z")])
    bundle.pop("timestamp")
    state, _, _ = _read(spec, [bundle])
    assert state["feeds"]["thing"]["through"] == "2026-11-05T00:03:00+00:00"
    future = _bundle([_event("a", at="2026-11-05T00:03:02Z")])
    future.pop("timestamp")
    with pytest.raises(ValueError, match="newer than its server response"):
        _read(spec, [future])


def test_future_source_coverage_never_confirms() -> None:
    with pytest.raises(ValueError, match="coverage frontier is in the future"):
        _read(_spec(), [_bundle([], timestamp="2200-01-01T00:00:00Z")])
