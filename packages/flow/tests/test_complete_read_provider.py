"""Actual injected readers retain the full write-verification checks."""

from __future__ import annotations

import json

import httpx
import pytest

from openadapt_flow import verify_run
from openadapt_flow.complete_read import CompleteReadProvider
from openadapt_flow.deployment import build_effect_verifier
from openadapt_flow.verifier import (
    Actor,
    BaselineUnreadable,
    Contract,
    Ledger,
    MemoryLedger,
    Outcome,
    WriteBlocked,
    begin_write,
    recheck,
    verify_write,
)
from tests.test_quiescent_read_setup import LockedAuthority, learn
from tests.test_quiescent_read_setup import store as store
from tests.test_verifier_api import BOT, PARAMS, SAVE, _contract, _store, _write
from tests.test_verifier_recheck import (
    _db,
    _doc,
    _params,
    _save_sql,
    _sql,
)
from tests.test_verifier_recheck import (
    due_now as due_now,
)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def provider(contract, params):
    reader = build_effect_verifier(contract.config.verifier, params)
    return CompleteReadProvider(contract.config.verifier, params, reader)


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("change", ["correct", "wrong", "duplicate", "collateral"])
def test_sql_effect_and_frame_checks_remain(tmp_path, injected, change):
    db = _store(tmp_path)
    contract = _contract(db)
    supplied = provider(contract, PARAMS) if injected else None
    pending = begin_write(
        contract,
        PARAMS,
        actor=BOT,
        ledger=MemoryLedger(),
        read_provider=supplied,
    )
    statements = [SAVE]
    if change == "wrong":
        statements = [SAVE.replace("'Ada'", "'Eda'")]
    elif change == "duplicate":
        statements.append(SAVE)
    elif change == "collateral":
        statements.append("UPDATE patient SET phone='2' WHERE id=1")
    _write(db, *statements)
    result = verify_write(pending)
    assert (result.outcome is Outcome.CONFIRMED) == (change == "correct")
    if change == "collateral":
        assert result.checked["frame"] == "refuted"
    assert result.receipt_sha256


def test_same_bound_provider_reads_baseline_and_post_over_real_http(store, monkeypatch):
    store.seed(1)
    authority = LockedAuthority(store)
    params = {"id": "1", "value": "v1"}
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
        assert setup.observations_match
        contract = Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": setup.config,
                "record": {
                    "name": "measurement",
                    "match": ["id"],
                    "fields": {"id": "id", "value": "value"},
                },
                "frame": {"identity_field": "id"},
            }
        )
        reader = build_effect_verifier(
            contract.config.verifier,
            params,
            session=client,
            quiescence=authority,
        )
        supplied = CompleteReadProvider(contract.config.verifier, params, reader)

        def no_rebuild(*args, **kwargs):
            raise AssertionError("the injected reader must not be rebuilt")

        monkeypatch.setattr(verify_run, "build_effect_verifier", no_rebuild)
        calls = store.calls
        pending = begin_write(
            contract,
            params,
            actor=Actor("agent", "synthetic"),
            ledger=MemoryLedger(),
            read_provider=supplied,
        )
        assert store.calls >= calls + 2
        assert (
            pending._read_provider.resolve(contract.config.verifier, params) is reader
        )
        assert reader._session is client
        baseline_calls = store.calls
        store.seed(2)
        result = verify_write(pending)
        assert store.calls >= baseline_calls + 2
        assert result.outcome is Outcome.CONFIRMED
        assert result.checked["frame"] == "held"
        assert reader._session is client


@pytest.mark.parametrize("completion", ["PENDING", "INVALID", "UNKNOWN"])
def test_pending_reader_cannot_create_an_ordinary_baseline(store, completion):
    store.seed(1)
    authority = LockedAuthority(store)
    params = {"id": "1", "value": "v1"}
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
        assert setup.observations_match
        contract = Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": setup.config,
                "record": {
                    "name": "measurement",
                    "match": ["id"],
                    "fields": {"id": "id", "value": "value"},
                },
            }
        )
        reader = build_effect_verifier(
            contract.config.verifier,
            params,
            session=client,
            quiescence=authority,
        )
        supplied = CompleteReadProvider(contract.config.verifier, params, reader)
        authority.completion_mode = completion
        ledger = Ledger(MemoryLedger())
        with pytest.raises(BaselineUnreadable):
            begin_write(
                contract,
                params,
                actor=BOT,
                ledger=ledger,
                write_key="synthetic-item",
                read_provider=supplied,
            )
        key = ledger.key_for(contract.identity, params, "synthetic-item")
        assert ledger.latest(key) is None


@pytest.mark.parametrize("drift", ["params", "config", "identity"])
def test_provider_binding_drift_refuses(tmp_path, drift):
    db = _store(tmp_path)
    contract = _contract(db)
    supplied = provider(contract, PARAMS)
    params = dict(PARAMS)
    if drift == "params":
        params["fname"] = "different"
    elif drift == "config":
        contract.config.verifier.sql_query = "SELECT * FROM patient WHERE id=1"
    else:
        supplied._reader._openadapt_verifier_identity = "sha256:" + "0" * 64
    with pytest.raises(BaselineUnreadable):
        begin_write(
            contract, params, actor=BOT, ledger=MemoryLedger(), read_provider=supplied
        )


def test_provider_preserves_live_ledger_exclusion(tmp_path):
    db = _store(tmp_path)
    contract = _contract(db)
    supplied = provider(contract, PARAMS)
    ledger = MemoryLedger()
    pending = begin_write(
        contract,
        PARAMS,
        actor=BOT,
        ledger=ledger,
        write_key="same",
        read_provider=supplied,
    )
    try:
        with pytest.raises(WriteBlocked):
            begin_write(
                contract,
                PARAMS,
                actor=BOT,
                ledger=ledger,
                write_key="same",
                read_provider=supplied,
            )
    finally:
        pending.release()


def test_late_database_change_still_refutes(tmp_path, due_now):
    db = _db(tmp_path)
    contract = Contract.from_dict(_doc(db))
    params = _params(2)
    supplied = provider(contract, params)
    pending = begin_write(
        contract, params, actor=BOT, ledger=MemoryLedger(), read_provider=supplied
    )
    _sql(db, *_save_sql(2))
    result = verify_write(pending)
    assert result.outcome is Outcome.CONFIRMED and result.recheck_due_at
    _sql(db, "INSERT INTO calendar(pid,title) VALUES(2,'unexpected late write')")
    late = recheck(
        contract, result, params=params, actor=BOT, wait=True, read_provider=supplied
    )
    assert late.outcome == "REFUTED"


def test_reader_is_not_serialized_in_baseline(tmp_path):
    db = _store(tmp_path)
    contract = _contract(db)
    supplied = provider(contract, PARAMS)
    pending = begin_write(
        contract, PARAMS, actor=BOT, ledger=MemoryLedger(), read_provider=supplied
    )
    try:
        saved = json.loads(pending.baseline_path.read_text())
        assert "read_provider" not in saved and "_reader" not in saved
        assert supplied.resolve(contract.config.verifier, PARAMS) is supplied._reader
    finally:
        pending.release()


@pytest.mark.parametrize("mode", ["absent", "PENDING", "INVALID"])
def test_http_provider_never_overrides_completion(store, mode):
    store.seed(1)
    authority = LockedAuthority(store)
    params = {"id": "1", "value": "v1"}
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
        assert setup.observations_match
        contract = Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": setup.config,
                "record": {
                    "name": "measurement",
                    "match": ["id"],
                    "fields": {"id": "id", "value": "value"},
                },
            }
        )
        if mode == "absent":
            calls = store.calls
            with pytest.raises(BaselineUnreadable):
                begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
            assert store.calls == calls
            return
        reader = build_effect_verifier(
            contract.config.verifier,
            params,
            session=client,
            quiescence=authority,
        )
        supplied = CompleteReadProvider(contract.config.verifier, params, reader)
        pending = begin_write(
            contract, params, actor=BOT, ledger=MemoryLedger(), read_provider=supplied
        )
        store.seed(2)
        authority.completion_mode = mode
        result = verify_write(pending)
        assert result.outcome is Outcome.UNCERTAIN and not result.verified
        assert not result.retry_safe
