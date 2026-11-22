"""Independent reads reauthenticate; only explicit handles own derived keys."""

from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path

import pytest

from openadapt_flow import crypto, sealed_set

KEY = "synthetic-reader-key"
WRONG = "synthetic-reader-wrong"
PAYLOAD = b'{"synthetic": "reader lifecycle"}'
ENTRYPOINTS = (
    "handle_open",
    "open_set",
    "read_bytes",
    "read_text",
    "open_text",
    "list_files",
    "exists",
    "is_file",
    "is_dir",
)


@pytest.fixture(autouse=True)
def isolated_legacy_counter_state(monkeypatch):
    # Isolate old-source counter cases; corrected source has no reader cache.
    if hasattr(sealed_set, "_OPENED"):
        monkeypatch.setattr(sealed_set, "_OPENED", {})


@pytest.fixture
def sealed_bundle(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    monkeypatch.delenv(sealed_set.RECORDING_ENV_KEY, raising=False)
    with sealed_set.writing(root, "bundle", KEY) as writer:
        writer.write("workflow.json", PAYLOAD)
        writer.write("templates/synthetic.bin", b"synthetic file bytes")
    return root


@pytest.fixture
def scrypt_calls(monkeypatch):
    calls = []
    real_scrypt = crypto.Scrypt

    class CountedScrypt:
        def __init__(self, **kwargs):
            self.inner = real_scrypt(**kwargs)

        def derive(self, material):
            calls.append(1)  # Retain no secret/key material in the measurement.
            return self.inner.derive(material)

    monkeypatch.setattr(crypto, "Scrypt", CountedScrypt)
    return calls


def _read(entrypoint: str, root: Path):
    target = root / "workflow.json"
    if entrypoint == "handle_open":
        return sealed_set.SealedSet.open(root, KEY).read_bytes("workflow.json")
    if entrypoint == "open_set":
        return sealed_set.open_set(root, KEY).read_bytes("workflow.json")
    if entrypoint == "read_bytes":
        return sealed_set.read_bytes(target)
    if entrypoint == "read_text":
        return sealed_set.read_text(target)
    if entrypoint == "open_text":
        with sealed_set.open_text(target) as stream:
            return stream.read()
    if entrypoint == "list_files":
        return sealed_set.list_files(root)
    if entrypoint == "exists":
        return sealed_set.exists(target)
    if entrypoint == "is_file":
        return sealed_set.is_file(target)
    assert entrypoint == "is_dir"
    return sealed_set.is_dir(root / "templates")


def _assert_no_module_reader_or_credentials():
    def retained(value, seen):
        if isinstance(value, sealed_set.SealedSet):
            return True
        if isinstance(value, (str, bytes)):
            return value in (KEY, WRONG, KEY.encode(), WRONG.encode(), PAYLOAD)
        if not isinstance(value, (dict, tuple, list, set)) or id(value) in seen:
            return False
        seen.add(id(value))
        values = (*value.keys(), *value.values()) if isinstance(value, dict) else value
        return any(retained(item, seen) for item in values)

    for name, value in vars(sealed_set).items():
        if name != "__builtins__":
            assert not retained(value, set()), name


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_independent_reader_requests_run_scrypt_each_time(
    sealed_bundle, scrypt_calls, entrypoint
):
    first = _read(entrypoint, sealed_bundle)
    assert first and len(scrypt_calls) == 1
    assert _read(entrypoint, sealed_bundle) == first
    assert len(scrypt_calls) == 2
    _assert_no_module_reader_or_credentials()


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_each_reader_rechecks_the_layout_after_a_prior_open(sealed_bundle, entrypoint):
    # A caller-owned earlier reader cannot authorize a separate future read.
    earlier = sealed_set.open_set(sealed_bundle, KEY)
    assert earlier.read_bytes("workflow.json") == PAYLOAD
    (sealed_bundle / "unexpected.txt").write_text("synthetic unsealed addition")
    with pytest.raises(sealed_set.SealedSetError, match="not in the sealed index"):
        _read(entrypoint, sealed_bundle)


@pytest.mark.parametrize("failure", ["none", "wrong_key", "tampered_index"])
def test_reader_is_not_retained_after_success_or_failed_reopen(sealed_bundle, failure):
    reader = sealed_set.open_set(sealed_bundle, KEY)
    reference = weakref.ref(reader)
    if failure == "wrong_key":
        with pytest.raises(crypto.CryptoError):
            sealed_set.open_set(sealed_bundle, WRONG)
    elif failure == "tampered_index":
        header_path = sealed_bundle / sealed_set.HEADER
        header = json.loads(header_path.read_bytes())
        header["index"] = "invalid sealed index!"
        header_path.write_text(json.dumps(header))
        with pytest.raises(sealed_set.SealedSetError):
            sealed_set.open_set(sealed_bundle, KEY)
    del reader
    gc.collect()
    assert reference() is None
    _assert_no_module_reader_or_credentials()


def test_explicit_reader_handle_reuses_only_its_owned_key(sealed_bundle, scrypt_calls):
    reader = sealed_set.SealedSet.open(sealed_bundle, KEY)
    assert len(scrypt_calls) == 1
    for _ in range(2):
        assert reader.read_bytes("workflow.json") == PAYLOAD
        assert reader.is_file("workflow.json")
        assert reader.is_dir("templates")
    assert len(scrypt_calls) == 1
    _assert_no_module_reader_or_credentials()


@pytest.mark.parametrize("fail", [False, True])
def test_scoped_writer_reuse_and_finally_cleanup(tmp_path, scrypt_calls, fail):
    root = tmp_path / "writer"

    def operation():
        with sealed_set.writing(root, "bundle", KEY) as writer:
            assert sealed_set.active_writer(root) is writer
            writer.write("workflow.json", PAYLOAD)
            for _ in range(2):
                assert sealed_set.read_bytes(root / "workflow.json") == PAYLOAD
            assert len(scrypt_calls) == 1
            if fail:
                raise RuntimeError("synthetic interrupted operation")

    if fail:
        with pytest.raises(RuntimeError, match="synthetic interrupted operation"):
            operation()
    else:
        operation()
    assert sealed_set.active_writer(root) is None
    _assert_no_module_reader_or_credentials()
    if fail:
        with pytest.raises(sealed_set.SealedSetError, match="never finalized"):
            sealed_set.open_set(root, KEY)
    else:
        assert sealed_set.open_set(root, KEY).read_bytes("workflow.json") == PAYLOAD
