"""Checkpoint crypto keeps no process-global key cache; a store owns its keys.

#2341 removed the process-wide derived-key cache. A checkpoint store is an
explicit caller-owned handle: it keeps the keys it derived for its own
lifetime (crypto.ContainerKeys), so one run stretches its passphrase once
instead of once per read (131 scrypt runs, about 20 s, on one MockMed run).
Another store, or a changed key, derives again. Wire format, wrong-key and
tamper refusals are unchanged, and every read still authenticates.
"""

from __future__ import annotations

import base64
import json

import pytest

from openadapt_flow import crypto
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    RunCheckpoint,
    RunManifest,
)

KEY = "correct horse battery staple"
WRONG = "incorrect horse battery staple"


@pytest.fixture
def derivation_requests(monkeypatch):
    requests = []
    original = crypto._derive

    def counting(*args, **kwargs):
        requests.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(crypto, "_derive", counting)
    return requests


def _checkpoint(i: int) -> RunCheckpoint:
    return RunCheckpoint(
        workflow_name="w",
        step_index=i,
        step_id=f"s{i}",
        intent="click",
        next_step_index=i + 1,
        params={"patient": "MRN-42"},
    )


def test_a_store_derives_its_key_once_for_a_whole_run(tmp_path, derivation_requests):
    before = crypto.derivations
    store = CheckpointStore(tmp_path / "run", key=KEY)
    store.write_manifest(RunManifest(workflow_name="w", bundle_dir="b"))
    for i in range(3):
        store.write_checkpoint(_checkpoint(i))
        store.continuation_state_digest()
        assert store.last_checkpoint().step_id == f"s{i}"
    assert crypto.derivations - before == len(derivation_requests) == 1


def test_another_store_on_the_same_run_derives_again(tmp_path, derivation_requests):
    run = tmp_path / "run"
    writer = CheckpointStore(run, key=KEY)
    for i in range(5):
        writer.write_checkpoint(_checkpoint(i))
    assert len(derivation_requests) == 1
    reader = CheckpointStore(run, key=KEY)
    for _ in range(10):
        assert len(reader.checkpoints()) == 5
    reader.write_checkpoint(_checkpoint(5))
    # The reader stretched the writer's salt once for its reads, and its own
    # new salt once for its write; nothing was shared with the writer.
    assert len(derivation_requests) == 3


def test_a_changed_store_key_drops_the_derived_keys(tmp_path, derivation_requests):
    run = tmp_path / "run"
    store = CheckpointStore(run, key=KEY)
    store.write_checkpoint(_checkpoint(0))
    assert store.last_checkpoint() is not None
    assert len(derivation_requests) == 1
    store.key = WRONG
    with pytest.raises(crypto.DecryptionError):
        store.last_checkpoint()
    assert len(derivation_requests) == 2


def test_a_store_without_an_explicit_key_keeps_no_keys(
    tmp_path, monkeypatch, derivation_requests
):
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", KEY)
    writer = CheckpointStore(tmp_path / "run", key=KEY)
    writer.write_checkpoint(_checkpoint(0))
    reader = CheckpointStore(tmp_path / "run")
    before = len(derivation_requests)
    for _ in range(3):
        assert reader.last_checkpoint() is not None
    assert len(derivation_requests) - before == 3
    assert reader._container_keys is None


def test_containers_keep_the_format_with_one_salt_and_fresh_nonces(tmp_path):
    store = CheckpointStore(tmp_path / "run", key=KEY)
    paths = [store.write_checkpoint(_checkpoint(i)) for i in range(3)]
    containers = [json.loads(p.read_bytes()) for p in paths]
    for c in containers:
        assert c["format"] == "openadapt-flow-enc" and c["version"] == 1
        assert c["kdf"] == "scrypt" and len(base64.b64decode(c["salt"])) == 16
        assert b"MRN-42" not in json.dumps(c).encode()
    assert len({c["salt"] for c in containers}) == 1
    assert len({c["nonce"] for c in containers}) == 3
    # Any reader decrypts them from what each container records.
    for p, i in zip(paths, range(3)):
        plain = crypto.decrypt_bytes(p.read_bytes(), KEY, aad=crypto.CHECKPOINT_AAD)
        assert json.loads(plain)["step_id"] == f"s{i}"


def test_a_cached_key_never_opens_for_another_passphrase(tmp_path):
    run = tmp_path / "run"
    store = CheckpointStore(run, key=KEY)
    store.write_checkpoint(_checkpoint(0))
    assert store.last_checkpoint() is not None  # prior independent successful read
    with pytest.raises(crypto.DecryptionError):
        CheckpointStore(run, key=WRONG).last_checkpoint()


def test_tampering_is_still_detected_with_a_cached_key(tmp_path):
    store = CheckpointStore(tmp_path / "run", key=KEY)
    path = store.write_checkpoint(_checkpoint(0))
    assert store.last_checkpoint() is not None
    container = json.loads(path.read_bytes())
    raw = bytearray(base64.b64decode(container["ciphertext"]))
    raw[0] ^= 1
    container["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    path.write_bytes(json.dumps(container).encode())
    with pytest.raises(crypto.DecryptionError):
        store.last_checkpoint()


def test_an_explicit_salt_must_be_sixteen_bytes():
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_bytes(b"x", KEY, aad=crypto.CHECKPOINT_AAD, salt=b"short")


def test_repeated_payload_operations_do_not_use_a_global_cache():
    before = crypto.derivations
    salt = b"s" * 16
    containers = [
        crypto.encrypt_bytes(b"x", KEY, aad=crypto.CHECKPOINT_AAD, salt=salt)
        for _ in range(2)
    ]
    for container in containers:
        assert crypto.decrypt_bytes(container, KEY, aad=crypto.CHECKPOINT_AAD) == b"x"
    assert crypto.derivations - before == 4
    assert not hasattr(crypto, "_DERIVED")


def test_a_durable_run_derives_once_per_store(tmp_path, derivation_requests):
    # The original synthetic durable readback workload: the run's store owns
    # its derived key, and a fresh store reading the run back derives again.
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_durable_runtime import _key_step, _vision_ok
    from tests.test_replayer import FakeBackend

    workflow = Workflow(
        name="durable-keyed",
        steps=[_key_step(f"s{i}", "A", risk="reversible") for i in range(12)],
    )
    bundle, run_dir = tmp_path / "bundle", tmp_path / "run"
    workflow.save(bundle)
    before = crypto.derivations
    report = Replayer(
        FakeBackend(),
        vision=_vision_ok(),
        durable=True,
        checkpoint_key=KEY,
        poll_interval_s=0.01,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    assert report.success is True
    during_run = len(derivation_requests)
    assert 1 <= during_run <= 3
    assert len(CheckpointStore(run_dir, key=KEY).checkpoints()) == 12
    assert len(derivation_requests) == during_run + 1
    assert crypto.derivations - before == len(derivation_requests)
