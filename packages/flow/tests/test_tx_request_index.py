"""Request identity survives encryption/signing rotation and holds corrupt state."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.tx import TransactionalCommitEngine, TxTask
from openadapt_flow.tx.receipt import (
    read_private_record,
    task_key_for,
    write_private_bytes,
    write_private_record,
)
from openadapt_flow.tx.request_index import RequestIndexHeld
from openadapt_flow.write_ledger import FileLedger, Ledger
from tests.test_tx_atomic_authority import _db, _lens


def _task(entity_id="r1"):
    return TxTask(
        task_id="same-request",
        operation="update",
        entity_id=entity_id,
        intended_delta={"note": "requested"},
    )


def _engine(tmp_path, authority, **kwargs):
    return TransactionalCommitEngine(
        _lens(), atomic_authority=authority, workdir=tmp_path / "state", **kwargs
    )


@pytest.mark.parametrize("rotation", ["encryption", "signing", "both"])
def test_key_rotation_preserves_request_hold_target_scope_and_old_ciphertext(
    tmp_path, monkeypatch, rotation
):
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-old-passphrase")
    authority = _db(tmp_path)
    with sqlite3.connect(authority.database) as c:
        c.execute("INSERT INTO records VALUES('r2','other','preserve',1)")
    first = _engine(tmp_path, authority, signing_key=Ed25519PrivateKey.generate())
    task = _task()
    assert first.execute(task).outcome == "CONFIRMED"
    key = task_key_for(task.task_id, workdir=first.workdir)
    original = first.workdir / key / "undo_r0.json"
    original_bytes = original.read_bytes()
    if rotation in ("encryption", "both"):
        monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-new-passphrase")
    signer = (
        Ed25519PrivateKey.generate()
        if rotation in ("signing", "both")
        else first._signing_key
    )
    reopened = _engine(tmp_path, authority, signing_key=signer)
    assert task_key_for(task.task_id, workdir=reopened.workdir) == key
    assert reopened._ledger_key(task) == first._ledger_key(task)
    assert reopened.execute(task).outcome == "BLOCKED_PRECOMMIT"
    assert reopened.execute(_task("r2")).outcome == "BLOCKED_PRECOMMIT"
    assert authority.read("r1")["version"] == 2 and authority.read("r2")["version"] == 1
    assert original.read_bytes() == original_bytes
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-old-passphrase")
    assert json.loads(read_private_record(original))["note"] == "old"
    assert reopened.compensate(task.task_id) is True
    assert authority.read("r1")["note"] == "old"


def test_concurrent_initialization_and_same_request_have_one_identity_and_write(
    tmp_path,
):
    authority = _db(tmp_path)
    barrier = threading.Barrier(6)

    def create(_):
        barrier.wait(timeout=10)
        return _engine(tmp_path, authority)

    with ThreadPoolExecutor(max_workers=6) as pool:
        engines = list(pool.map(create, range(6)))
    assert (
        len({engine._request_index.task_key("same-request") for engine in engines}) == 1
    )
    barrier = threading.Barrier(6)

    def execute(engine):
        barrier.wait(timeout=10)
        return engine.execute(_task())

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(execute, engines))
    assert sum(r.outcome == "CONFIRMED" for r in results) == 1
    assert sum(r.outcome == "BLOCKED_PRECOMMIT" for r in results) == 5
    assert authority.read("r1")["version"] == 2


def test_interrupted_empty_initialization_reuses_the_same_existing_ledger_key(
    tmp_path, monkeypatch
):
    from openadapt_flow.tx import receipt

    authority = _db(tmp_path)
    original = receipt.write_private_bytes

    def crash(path, data, **kwargs):
        if path.name == "tx-request-index.json":
            raise OSError("synthetic initialization interruption")
        return original(path, data, **kwargs)

    monkeypatch.setattr(receipt, "write_private_bytes", crash)
    with pytest.raises(RequestIndexHeld, match="ledger_unavailable"):
        _engine(tmp_path, authority)
    key_path = tmp_path / "state" / "ledger" / "ledger.key"
    key_before = key_path.read_bytes()
    monkeypatch.setattr(receipt, "write_private_bytes", original)
    reopened = _engine(tmp_path, authority)
    assert key_path.read_bytes() == key_before
    assert reopened.execute(_task()).outcome == "CONFIRMED"


def test_interrupted_begun_request_stays_held_after_reopen_and_key_rotation(
    tmp_path, monkeypatch
):
    authority = _db(tmp_path)
    engine = _engine(tmp_path, authority)
    task = _task()
    engine.ledger.begun(
        engine._ledger_key(task), "synthetic-crashed-attempt", write_key_bound=True
    )
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-rotated-passphrase")
    result = _engine(tmp_path, authority).execute(task)
    assert (
        result.outcome == "BLOCKED_PRECOMMIT" and "ledger_hold_active" in result.reasons
    )
    assert authority.read("r1")["version"] == 1


@pytest.mark.parametrize(
    "component,operation",
    [
        ("ledger.key", "lost"),
        ("ledger.key", "replace"),
        ("tx-request-index.json", "lost"),
        ("tx-request-index.json", "replace"),
        ("tx-request-index.json", "version"),
    ],
)
def test_lost_replaced_or_unversioned_index_holds_live_and_reopened_entrypoints(
    tmp_path, component, operation
):
    authority = _db(tmp_path)
    engine = _engine(tmp_path, authority)
    task = _task()
    assert engine.execute(task).outcome == "CONFIRMED"
    ciphertext = (
        engine.workdir / engine._request_index.task_key(task.task_id) / "undo_r0.json"
    )
    original = ciphertext.read_bytes()
    target = engine.workdir / "ledger" / component
    if operation == "lost":
        target.unlink()
    elif component == "ledger.key":
        write_private_bytes(target, b"ab" * 32)
    else:
        value = json.loads(target.read_text())
        value["schema" if operation == "version" else "ledger_key_sha256"] = (
            "unsupported" if operation == "version" else "0" * 64
        )
        write_private_bytes(target, json.dumps(value).encode())
    with pytest.raises(RequestIndexHeld):
        engine.execute(task)
    with pytest.raises(RequestIndexHeld):
        engine.wrap_action(task, lambda: pytest.fail("held index invoked action"))
    with pytest.raises(RequestIndexHeld):
        _engine(tmp_path, authority)
    assert engine.compensate(task.task_id) is False
    assert engine.compensation_proposal(task.task_id) is None
    assert authority.read("r1")["version"] == 2
    assert ciphertext.read_bytes() == original
    if operation == "lost":
        assert not target.exists()


def test_populated_legacy_workdir_never_silently_reindexes_old_requests(tmp_path):
    authority = _db(tmp_path)
    root = tmp_path / "state"
    ledger = Ledger(FileLedger(root / "ledger"))
    ledger.begun(
        ledger.key_for("legacy", {}, write_key="raw-historical-request"),
        "historical-attempt",
        write_key_bound=True,
    )
    old_record = root / "old-task" / "undo_r0.json"
    write_private_record(old_record, b'{"note":"old encrypted evidence"}')
    original = old_record.read_bytes()
    with pytest.raises(RequestIndexHeld, match="missing_for_existing_data"):
        _engine(tmp_path, authority)
    assert old_record.read_bytes() == original
    assert not (root / "ledger" / "tx-request-index.json").exists()
    assert authority.read("r1")["version"] == 1


@pytest.mark.parametrize("use_default", [False, True])
@pytest.mark.parametrize("changed_field", ["entity_name", "base_url"])
def test_candidate_metadata_cannot_rescope_same_request_or_authorize_another_target(
    tmp_path, use_default, changed_field
):
    authority = _db(tmp_path)
    with sqlite3.connect(authority.database) as c:
        c.execute("INSERT INTO records VALUES('r2','other','preserve',1)")
    state = {} if use_default else {"workdir": tmp_path / "state"}
    first = TransactionalCommitEngine(_lens(), atomic_authority=authority, **state)
    assert first.execute(_task()).outcome == "CONFIRMED"
    candidate = _lens().model_copy(
        update={
            changed_field: "renamed-observation"
            if changed_field == "entity_name"
            else "http://127.0.0.1:9"
        }
    )
    reopened = TransactionalCommitEngine(candidate, atomic_authority=authority, **state)
    assert reopened.workdir == first.workdir
    assert reopened._ledger_key(_task("r2")) == first._ledger_key(_task())
    assert reopened.execute(_task("r2")).outcome == "BLOCKED_PRECOMMIT"
    assert authority.read("r1")["version"] == 2 and authority.read("r2")["version"] == 1


def test_default_namespace_never_silently_abandons_legacy_lens_scopes(tmp_path):
    from openadapt_flow.at_rest import local_key_path

    authority = _db(tmp_path)
    history = local_key_path().parent.parent / "tx"
    legacy = history / "legacy-lens-scope"
    ledger = Ledger(FileLedger(legacy / "ledger"))
    key = ledger.key_for("legacy", {}, write_key="old-request")
    ledger.begun(key, "historical-attempt", write_key_bound=True)
    journal = legacy / "ledger" / "entries" / (key + ".jsonl")
    before = journal.read_bytes()
    with pytest.raises(RequestIndexHeld, match="legacy_default_request_state"):
        TransactionalCommitEngine(_lens(), atomic_authority=authority)
    assert journal.read_bytes() == before and not (history / "requests-v1").exists()
    assert authority.read("r1")["version"] == 1
