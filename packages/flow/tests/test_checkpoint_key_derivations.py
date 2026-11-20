"""Checkpoint crypto derives per operation without a process-global verifier.

The former cross-operation cache optimization is intentionally removed.
Wire, readback, wrong-key and tamper controls remain; explicit SetKey handles
reuse their owned AEAD within a sealed-set operation.
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


def test_a_run_derives_once_per_crypto_operation(tmp_path, derivation_requests):
    before = crypto.derivations
    store = CheckpointStore(tmp_path / "run", key=KEY)
    store.write_manifest(RunManifest(workflow_name="w", bundle_dir="b"))
    for i in range(3):
        store.write_checkpoint(_checkpoint(i))
        store.continuation_state_digest()
        assert store.last_checkpoint().step_id == f"s{i}"
    assert crypto.derivations - before == len(derivation_requests)
    assert len(derivation_requests) > 3


def test_resumed_store_reads_derive_per_operation(tmp_path, derivation_requests):
    run = tmp_path / "run"
    writer = CheckpointStore(run, key=KEY)
    for i in range(5):
        writer.write_checkpoint(_checkpoint(i))
    before_requests = len(derivation_requests)
    before = crypto.derivations
    reader = CheckpointStore(run, key=KEY)
    for _ in range(10):
        assert len(reader.checkpoints()) == 5
    reader.write_checkpoint(_checkpoint(5))
    assert crypto.derivations - before == len(derivation_requests) - before_requests
    assert crypto.derivations - before == 51


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


def test_durable_run_derives_per_operation(tmp_path, derivation_requests):
    # Keep the original synthetic durable readback workload; its independent
    # crypto operations now each derive, rather than sharing a global verifier.
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
    assert len(CheckpointStore(run_dir, key=KEY).checkpoints()) == 12
    assert crypto.derivations - before == len(derivation_requests)
    assert len(derivation_requests) > 3
