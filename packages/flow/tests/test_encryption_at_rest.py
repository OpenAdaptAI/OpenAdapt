"""Opt-in encryption-at-rest for compiled bundles and durable checkpoints.

Covers ``openadapt_flow.crypto`` (the AES-256-GCM AEAD substrate) and its
integration at the two serialization seams:

* ``Workflow.save(encrypt=True)`` / ``Workflow.load(key=...)`` -- the bundle
  ``workflow.json`` sealed at rest, while the schema-v2 integrity manifest still
  verifies on decrypt;
* ``CheckpointStore(key=...)`` -- the durable checkpoints / pending escalation /
  run manifest / program checkpoints sealed at rest.

Every test asserts one of: a clean round-trip, a LOUD failure on a
wrong/missing key, tamper detection (a flipped ciphertext byte fails the AEAD
tag), or that the UNENCRYPTED default path is byte-for-byte unchanged.

Import-light: builds IR objects directly (no Playwright / OCR / model deps).
"""

from __future__ import annotations

import base64
import json

import pytest

from openadapt_flow import crypto
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable.checkpoint import (
    ENC_SUFFIX,
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import ProgramCheckpoint

KEY = "correct horse battery staple"
WRONG = "Tr0ub4dor&3"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _workflow() -> Workflow:
    return Workflow(
        name="demo",
        steps=[
            Step(
                id="s1",
                intent="click Save",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/btn.png",
                    region=(100, 100, 50, 20),
                    click_point=(110, 105),
                    ocr_text="Save",
                ),
            ),
            Step(id="s2", intent="submit", action=ActionKind.KEY, key="Enter"),
        ],
    )


def _bundle_dir(tmp_path):
    b = tmp_path / "bundle"
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "btn.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-crop-bytes")
    return b


def _flip_container_ciphertext(container: bytes) -> bytes:
    """Return ``container`` with a single ciphertext byte flipped (still valid
    base64 / JSON, but the AEAD tag will no longer authenticate)."""
    obj = json.loads(container)
    ct = bytearray(base64.b64decode(obj["ciphertext"]))
    ct[0] ^= 0x01
    obj["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
    return json.dumps(obj).encode("utf-8")


# ---------------------------------------------------------------------------
# crypto primitive
# ---------------------------------------------------------------------------


def test_crypto_round_trip():
    payload = b'{"hello": "world", "n": 42}'
    sealed = crypto.encrypt_bytes(payload, KEY, aad=crypto.BUNDLE_AAD)
    assert sealed != payload
    assert crypto.is_encrypted(sealed)
    assert crypto.decrypt_bytes(sealed, KEY, aad=crypto.BUNDLE_AAD) == payload


def test_crypto_wrong_key_fails_loudly():
    sealed = crypto.encrypt_bytes(b"secret", KEY, aad=crypto.BUNDLE_AAD)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(sealed, WRONG, aad=crypto.BUNDLE_AAD)


def test_crypto_missing_key_fails_loudly(monkeypatch):
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    with pytest.raises(crypto.MissingKeyError):
        crypto.encrypt_bytes(b"secret", None, aad=crypto.BUNDLE_AAD)
    sealed = crypto.encrypt_bytes(b"secret", KEY, aad=crypto.BUNDLE_AAD)
    with pytest.raises(crypto.MissingKeyError):
        crypto.decrypt_bytes(sealed, None, aad=crypto.BUNDLE_AAD)


def test_crypto_tamper_detected():
    sealed = crypto.encrypt_bytes(b"important payload", KEY, aad=crypto.BUNDLE_AAD)
    tampered = _flip_container_ciphertext(sealed)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(tampered, KEY, aad=crypto.BUNDLE_AAD)


def test_crypto_domain_separation():
    # A container sealed for the bundle domain must not decrypt under the
    # checkpoint domain even with the right key (associated-data mismatch).
    sealed = crypto.encrypt_bytes(b"payload", KEY, aad=crypto.BUNDLE_AAD)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(sealed, KEY, aad=crypto.CHECKPOINT_AAD)


def test_crypto_env_fallback(monkeypatch):
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    sealed = crypto.encrypt_bytes(b"x", None, aad=crypto.BUNDLE_AAD)
    assert crypto.decrypt_bytes(sealed, None, aad=crypto.BUNDLE_AAD) == b"x"


def test_is_encrypted_rejects_plaintext():
    assert not crypto.is_encrypted(b'{"schema_version": 2, "name": "demo"}')
    assert not crypto.is_encrypted(b"not json at all")


# ---------------------------------------------------------------------------
# bundle encryption
# ---------------------------------------------------------------------------


def test_bundle_unencrypted_default_unchanged(tmp_path):
    b = _bundle_dir(tmp_path)
    wf = _workflow()
    path = wf.save(b)
    assert path.name == "workflow.json"
    assert (b / "workflow.json").is_file()
    assert not (b / "workflow.json.enc").exists()
    assert wf.encrypted is False
    # Plaintext JSON on disk, human-readable, no key needed to load.
    assert json.loads((b / "workflow.json").read_text())["name"] == "demo"
    loaded = Workflow.load(b)
    assert loaded.name == "demo"
    assert loaded.encrypted is False


def test_bundle_encrypted_round_trip(tmp_path):
    b = _bundle_dir(tmp_path)
    wf = _workflow()
    path = wf.save(b, encrypt=True, key=KEY)

    # Ciphertext on disk; no plaintext workflow.json lingering.
    assert path.name == "workflow.json.enc"
    assert (b / "workflow.json.enc").is_file()
    assert not (b / "workflow.json").exists()
    assert crypto.is_encrypted((b / "workflow.json.enc").read_bytes())
    assert wf.encrypted is True

    # The manifest sidecar stays plaintext and advertises the encrypted state
    # for an opaque compliance inventory (no key needed).
    sidecar = json.loads((b / "manifest.json").read_text())
    assert sidecar["encrypted"] is True

    loaded = Workflow.load(b, key=KEY)
    assert loaded.model_dump() == wf.model_dump()
    assert loaded.encrypted is True


def test_bundle_load_without_key_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    with pytest.raises(crypto.MissingKeyError):
        Workflow.load(b)


def test_bundle_load_with_wrong_key_fails_loudly(tmp_path):
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    with pytest.raises(crypto.DecryptionError):
        Workflow.load(b, key=WRONG)


def test_bundle_tampered_ciphertext_detected(tmp_path):
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    enc = b / "workflow.json.enc"
    enc.write_bytes(_flip_container_ciphertext(enc.read_bytes()))
    with pytest.raises(crypto.DecryptionError):
        Workflow.load(b, key=KEY)


def test_bundle_encrypted_preserves_integrity_manifest(tmp_path):
    # The schema-v2 content digest is sealed over the PLAINTEXT content before
    # encryption, so a decrypted load still verifies integrity end-to-end.
    b = _bundle_dir(tmp_path)
    wf = _workflow()
    wf.save(b, encrypt=True, key=KEY)
    loaded = Workflow.load(b, key=KEY, verify_integrity=True)
    assert loaded.manifest is not None
    assert loaded.manifest.content_digest
    assert loaded.manifest.encrypted is True
    # btn.png is still hashed into the manifest even though the bundle is sealed.
    assert any(p.endswith("btn.png") for p in loaded.manifest.file_hashes)


def test_bundle_key_param_implies_encryption(tmp_path):
    # Passing a key (without encrypt=True) is intent enough to seal the bundle.
    b = _bundle_dir(tmp_path)
    path = _workflow().save(b, key=KEY)
    assert path.name == "workflow.json.enc"


def test_bundle_encrypt_uses_env_key(tmp_path, monkeypatch):
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True)  # key from env
    loaded = Workflow.load(b)  # key from env
    assert loaded.name == "demo"


def test_bundle_resave_plaintext_removes_ciphertext(tmp_path):
    # Switching an encrypted bundle back to plaintext must not leave a stale
    # ciphertext copy alongside the readable one.
    b = _bundle_dir(tmp_path)
    wf = _workflow()
    wf.save(b, encrypt=True, key=KEY)
    assert (b / "workflow.json.enc").exists()
    wf.save(b)  # plaintext re-save
    assert (b / "workflow.json").is_file()
    assert not (b / "workflow.json.enc").exists()


# ---------------------------------------------------------------------------
# checkpoint encryption
# ---------------------------------------------------------------------------


def _checkpoint():
    return RunCheckpoint(
        workflow_name="w",
        step_index=0,
        step_id="s0",
        intent="click",
        next_step_index=1,
        params={"patient": "MRN-42"},
    )


def test_checkpoint_unencrypted_default_unchanged(tmp_path):
    store = CheckpointStore(tmp_path / "run")
    store.write_manifest(RunManifest(workflow_name="w", bundle_dir="b"))
    path = store.write_checkpoint(_checkpoint())
    assert path.name.endswith(".json")
    assert not path.name.endswith(ENC_SUFFIX)
    # Readable JSON, no key needed.
    assert json.loads(path.read_text())["step_id"] == "s0"
    assert store.last_checkpoint().step_id == "s0"


def test_checkpoint_encrypted_round_trip(tmp_path):
    run = tmp_path / "run"
    store = CheckpointStore(run, key=KEY)
    mpath = store.write_manifest(RunManifest(workflow_name="w", bundle_dir="b"))
    cpath = store.write_checkpoint(_checkpoint())
    ppath = store.write_pending(
        PendingEscalation(
            workflow_name="w",
            step_index=1,
            step_id="s1",
            category="identity",
            reason="boom",
            params={"patient": "MRN-42"},
        )
    )

    # Everything is sealed on disk (.enc), no plaintext siblings, and the
    # patient identifier is not grep-visible in the ciphertext.
    for p in (mpath, cpath, ppath):
        assert p.name.endswith(ENC_SUFFIX)
        assert crypto.is_encrypted(p.read_bytes())
        assert b"MRN-42" not in p.read_bytes()
    assert not (run / "checkpoints" / "step_0000_s0.json").exists()

    # A same-key store reads them back identically.
    reader = CheckpointStore(run, key=KEY)
    assert reader.read_manifest().workflow_name == "w"
    got = reader.last_checkpoint()
    assert got is not None and got.params == {"patient": "MRN-42"}
    assert reader.read_pending().reason == "boom"


def test_checkpoint_wrong_key_fails_loudly(tmp_path):
    run = tmp_path / "run"
    CheckpointStore(run, key=KEY).write_checkpoint(_checkpoint())
    with pytest.raises(crypto.DecryptionError):
        CheckpointStore(run, key=WRONG).last_checkpoint()


def test_checkpoint_missing_key_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    run = tmp_path / "run"
    CheckpointStore(run, key=KEY).write_checkpoint(_checkpoint())
    with pytest.raises(crypto.MissingKeyError):
        CheckpointStore(run).last_checkpoint()


def test_checkpoint_tamper_detected(tmp_path):
    run = tmp_path / "run"
    path = CheckpointStore(run, key=KEY).write_checkpoint(_checkpoint())
    path.write_bytes(_flip_container_ciphertext(path.read_bytes()))
    with pytest.raises(crypto.DecryptionError):
        CheckpointStore(run, key=KEY).last_checkpoint()


def test_program_checkpoint_encrypted_round_trip(tmp_path):
    run = tmp_path / "run"
    store = CheckpointStore(run, key=KEY)
    path = store.write_program_checkpoint(
        ProgramCheckpoint(
            workflow_name="w",
            seq=0,
            verified_state_id="state-a",
            bound_params={"patient": "MRN-99"},
        )
    )
    assert path.name.endswith(ENC_SUFFIX)
    assert b"MRN-99" not in path.read_bytes()
    got = CheckpointStore(run, key=KEY).last_program_checkpoint()
    assert got is not None and got.bound_params == {"patient": "MRN-99"}
