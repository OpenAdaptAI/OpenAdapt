"""Encrypted checkpoints stretch the passphrase a bounded number of times.

Before, every sealed checkpoint write and read ran scrypt (about 130 times on
one MockMed run, 10.9 s instead of 3.3 s). A store now seals its containers
under one salt, and a process keeps derived keys in memory, so a run derives
once for writes and reuses that key for reads. The container format, its
per-container salt field and random nonce, wrong-key refusal and tamper
detection are unchanged.
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


def _checkpoint(i: int) -> RunCheckpoint:
    return RunCheckpoint(
        workflow_name="w",
        step_index=i,
        step_id=f"s{i}",
        intent="click",
        next_step_index=i + 1,
        params={"patient": "MRN-42"},
    )


def test_a_run_derives_its_key_a_bounded_number_of_times(tmp_path):
    before = crypto.derivations
    store = CheckpointStore(tmp_path / "run", key=KEY)
    store.write_manifest(RunManifest(workflow_name="w", bundle_dir="b"))
    for i in range(40):
        store.write_checkpoint(_checkpoint(i))
        store.continuation_state_digest()
        assert store.last_checkpoint().step_id == f"s{i}"
    # One for this store's salt. (The process cache may already hold it.)
    assert crypto.derivations - before <= 1


def test_a_resumed_store_reads_with_one_more_derivation(tmp_path):
    run = tmp_path / "run"
    writer = CheckpointStore(run, key=KEY)
    for i in range(5):
        writer.write_checkpoint(_checkpoint(i))
    crypto._DERIVED.clear()
    before = crypto.derivations
    reader = CheckpointStore(run, key=KEY)
    for _ in range(10):
        assert len(reader.checkpoints()) == 5
    reader.write_checkpoint(_checkpoint(5))
    # One to read the writer's salt, one for the reader's own salt.
    assert crypto.derivations - before <= 2


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
    assert store.last_checkpoint() is not None  # key now cached for this salt
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


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(crypto, "_DERIVED_LIMIT", 3)
    for _ in range(6):
        crypto.encrypt_bytes(b"x", KEY, aad=crypto.CHECKPOINT_AAD)
    assert len(crypto._DERIVED) <= 3
