"""Real crypto operations retain keys in explicit handles, not global caches."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openadapt_flow import crypto
from openadapt_flow.runtime.durable import attended_state

KEY = "synthetic-operation-key"
WRONG = "synthetic-operation-wrong"
PAYLOAD = b"synthetic operation plaintext"


@pytest.fixture
def scrypt_calls(monkeypatch):
    calls = []
    original = crypto.Scrypt

    class CountedScrypt:
        def __init__(self, **kwargs):
            self.inner = original(**kwargs)

        def derive(self, material):
            calls.append(1)
            return self.inner.derive(material)

    monkeypatch.setattr(crypto, "Scrypt", CountedScrypt)
    return calls


def _no_global_credentials():
    def contains(value, needle, seen):
        if isinstance(value, (str, bytes)):
            return value == needle
        if not isinstance(value, (dict, tuple, list, set)) or id(value) in seen:
            return False
        seen.add(id(value))
        values = (*value.keys(), *value.values()) if isinstance(value, dict) else value
        return any(contains(item, needle, seen) for item in values)

    for module in (crypto, attended_state):
        for name, value in vars(module).items():
            if name == "__builtins__":
                continue
            for needle in (KEY, KEY.encode(), WRONG, WRONG.encode(), PAYLOAD):
                assert not contains(value, needle, set()), (module.__name__, name)
    for name in ("_DERIVED", "_DERIVED_FINGERPRINT_KEY", "_DERIVED_FINGERPRINT_DOMAIN"):
        assert not hasattr(crypto, name)
    assert not hasattr(attended_state, "_RUN_KEYS")
    assert not hasattr(attended_state, "_CACHE")


def _tamper(container):
    obj = json.loads(container)
    body = bytearray(base64.b64decode(obj["ciphertext"]))
    body[0] ^= 1
    obj["ciphertext"] = base64.b64encode(body).decode()
    return json.dumps(obj).encode()


def test_all_four_raw_entrypoints_derive_per_operation(scrypt_calls):
    salt, aad = b"s" * 16, crypto.BUNDLE_AAD
    container = crypto.encrypt_bytes(PAYLOAD, KEY, aad=aad, salt=salt)
    assert len(scrypt_calls) == 1
    assert crypto.decrypt_bytes(container, KEY, aad=aad) == PAYLOAD
    assert len(scrypt_calls) == 2
    expected = crypto.derive_data_key(KEY, salt)
    assert len(scrypt_calls) == 3
    handle = crypto.SetKey.derive(KEY, salt)
    assert len(scrypt_calls) == 4
    sealed = handle.seal(PAYLOAD, aad=aad)
    assert handle.open(sealed, aad=aad) == PAYLOAD
    assert len(scrypt_calls) == 4
    assert crypto.derive_data_key(KEY, salt) == expected
    assert len(scrypt_calls) == 5
    changed = crypto.encrypt_bytes(b"changed synthetic input", KEY, aad=aad, salt=salt)
    assert len(scrypt_calls) == 6
    assert crypto.decrypt_bytes(changed, KEY, aad=aad) == b"changed synthetic input"
    assert len(scrypt_calls) == 7
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(container, WRONG, aad=aad)
    assert len(scrypt_calls) == 8
    wrong = crypto.SetKey.derive(WRONG, salt)
    with pytest.raises(crypto.DecryptionError):
        wrong.open(sealed, aad=aad)
    assert len(scrypt_calls) == 9
    assert crypto.derive_data_key(KEY, b"t" * 16) != expected
    assert len(scrypt_calls) == 10
    assert crypto.derive_data_key(KEY, salt, p=2) != expected
    assert len(scrypt_calls) == 11
    _no_global_credentials()


def test_explicit_set_key_reuses_only_its_owned_aead(scrypt_calls):
    handle = crypto.SetKey.derive(KEY, b"a" * 16)
    assert len(scrypt_calls) == 1
    for _ in range(3):
        container = handle.seal(PAYLOAD, aad=crypto.BUNDLE_AAD)
        assert handle.open(container, aad=crypto.BUNDLE_AAD) == PAYLOAD
    assert len(scrypt_calls) == 1
    _no_global_credentials()


def test_legacy_attended_reads_reauthenticate_each_call(scrypt_calls):
    aad = b"synthetic legacy attended"
    container = crypto.encrypt_bytes(PAYLOAD, KEY, aad=aad)
    scrypt_calls.clear()  # Exclude only the setup encryption from read counts.
    assert attended_state.open_sealed(container, KEY, aad) == PAYLOAD
    assert attended_state.open_sealed(container, KEY, aad) == PAYLOAD
    assert len(scrypt_calls) == 2
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.open_sealed(container, WRONG, aad)
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.open_sealed(_tamper(container), KEY, aad)
    assert len(scrypt_calls) == 4
    _no_global_credentials()


def test_salted_attended_writes_and_reads_keep_no_global_credentials(
    tmp_path, scrypt_calls
):
    path = tmp_path / "attended_capability.json"

    def write(target: Path, data: bytes):
        target.write_bytes(data)

    attended_state.write_bytes(tmp_path, path, PAYLOAD, KEY, write)
    assert len(scrypt_calls) == 2  # Initial conversion phase plus payload write.
    assert attended_state.read_bytes(tmp_path, path, KEY) == PAYLOAD
    assert attended_state.read_bytes(tmp_path, path, KEY) == PAYLOAD
    assert len(scrypt_calls) == 4
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.read_bytes(tmp_path, path, WRONG)
    attended_state.write_bytes(tmp_path, path, b"changed synthetic input", KEY, write)
    assert attended_state.read_bytes(tmp_path, path, KEY) == b"changed synthetic input"
    assert len(scrypt_calls) == 7
    _no_global_credentials()


def test_journal_operations_keep_only_public_salt_metadata(scrypt_calls):
    stored = attended_state.seal_journal_snapshot(
        PAYLOAD.decode(), KEY, path_key="synthetic-operation-path", sequence=1
    )
    for _ in range(2):
        assert (
            attended_state.open_journal_snapshot(
                stored, KEY, path_key="synthetic-operation-path", sequence=1
            )
            == PAYLOAD.decode()
        )
    with pytest.raises(attended_state.AttendedStateUnreadable):
        attended_state.open_journal_snapshot(
            stored, WRONG, path_key="synthetic-operation-path", sequence=1
        )
    assert len(scrypt_calls) == 4
    _no_global_credentials()
