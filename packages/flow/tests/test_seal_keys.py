"""Customer-held Seal keys: key file, OS keychain, PKCS#11. Fail closed, no fallback.

The keychain and PKCS#11 paths run against in-memory stand-ins for the
``keyring`` and ``python-pkcs11`` modules. Two opt-in tests use the real
backends when ``OPENADAPT_TEST_KEYCHAIN=1`` or ``OPENADAPT_TEST_PKCS11_MODULE``
is set. All data is synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow import seal_keys
from openadapt_flow.offline_seal import (
    LocalSealError,
    load_registry,
    seal_after_run,
    seal_run,
    signer_for,
    verify_run,
)
from openadapt_flow.seal_keys import KeyUnavailable, Pkcs11Signer, create_key

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, name: str = "run") -> Path:
    run = tmp_path / name
    run.mkdir()
    (run / "report.json").write_text(
        json.dumps({"run_id": name, "execution_outcome": "VERIFIED"}), encoding="utf-8"
    )
    return run


def _pem(key: Ed25519PrivateKey, password: bytes | None = None) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=(
            serialization.BestAvailableEncryption(password)
            if password
            else serialization.NoEncryption()
        ),
    )


def _key_file(tmp_path: Path, password: bytes | None = None) -> Path:
    path = tmp_path / "customer-seal.pem"
    path.write_bytes(_pem(Ed25519PrivateKey.generate(), password))
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        seal_keys.ENV_KEY,
        seal_keys.ENV_KEY_FILE,
        seal_keys.ENV_KEY_PASSPHRASE,
        seal_keys.ENV_PKCS11_MODULE,
        seal_keys.ENV_PKCS11_PIN,
    ):
        monkeypatch.delenv(name, raising=False)


# --- key file -------------------------------------------------------------


def test_customer_key_file_signs_and_no_key_is_generated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    key = _key_file(tmp_path)
    monkeypatch.setenv(seal_keys.ENV_KEY_FILE, str(key))
    signer = signer_for(home)
    assert signer.source == "file"
    seal = seal_run(_run(tmp_path), home=home)
    assert seal.key_id == signer.key_id
    # The runner made no key of its own.
    assert not (home / "keys" / "ed25519.pem").exists()


def test_key_file_by_spec_and_passphrase(tmp_path, monkeypatch):
    key = _key_file(tmp_path, password=b"synthetic-pass")
    monkeypatch.setenv(seal_keys.ENV_KEY, f"file:{key}")
    with pytest.raises(LocalSealError) as exc:
        signer_for(tmp_path / "home")
    assert exc.value.code == "key_unavailable"
    monkeypatch.setenv(seal_keys.ENV_KEY_PASSPHRASE, "synthetic-pass")
    assert signer_for(tmp_path / "home").source == "file"


def test_missing_key_file_fails_closed_without_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv(seal_keys.ENV_KEY_FILE, str(tmp_path / "absent.pem"))
    run = _run(tmp_path)
    with pytest.raises(LocalSealError) as exc:
        seal_run(run, home=home)
    assert exc.value.code == "key_unavailable"
    assert not (run / "seal.json").exists()
    assert not (home / "keys").exists()
    # The run hook reports it and writes nothing.
    note = seal_after_run(
        run,
        env={
            seal_keys.ENV_KEY_FILE: str(tmp_path / "absent.pem"),
            "OPENADAPT_SEAL_HOME": str(home),
        },
    )
    assert note == "offline seal not written: key_unavailable"
    assert not (run / "seal.json").exists()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620, 0o610])
def test_key_file_open_to_group_or_others_is_refused(tmp_path, monkeypatch, mode):
    if sys.platform == "win32":
        pytest.skip("POSIX file modes")
    key = _key_file(tmp_path)
    key.chmod(mode)
    monkeypatch.setenv(seal_keys.ENV_KEY_FILE, str(key))
    with pytest.raises(LocalSealError) as exc:
        signer_for(tmp_path / "home")
    assert exc.value.code == "key_file_permissions"
    key.chmod(0o400)
    assert signer_for(tmp_path / "home").source == "file"


def test_non_ed25519_key_file_is_refused(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec

    path = tmp_path / "p256.pem"
    path.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv(seal_keys.ENV_KEY, f"file:{path}")
    with pytest.raises(LocalSealError) as exc:
        signer_for(tmp_path / "home")
    assert exc.value.code == "key_not_ed25519"


def test_conflicting_or_unknown_config_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(seal_keys.ENV_KEY, "file:/a.pem")
    monkeypatch.setenv(seal_keys.ENV_KEY_FILE, "/b.pem")
    with pytest.raises(LocalSealError) as exc:
        signer_for(tmp_path)
    assert exc.value.code == "key_config_conflict"
    monkeypatch.delenv(seal_keys.ENV_KEY_FILE)
    monkeypatch.setenv(seal_keys.ENV_KEY, "vault:seal")
    with pytest.raises(LocalSealError) as exc:
        signer_for(tmp_path)
    assert exc.value.code == "key_config_invalid"


def test_create_file_key_refuses_to_overwrite(tmp_path):
    path = tmp_path / "keys" / "new.pem"
    signer = create_key(f"file:{path}")
    assert signer.source == "file"
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(KeyUnavailable) as exc:
        create_key(f"file:{path}")
    assert exc.value.code == "key_exists"


# --- pinning and rotation --------------------------------------------------


def test_wrong_key_rotation_and_revocation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    old_key, new_key = _key_file(tmp_path), tmp_path / "new.pem"
    new_key.write_bytes(_pem(Ed25519PrivateKey.generate()))
    new_key.chmod(0o600)

    monkeypatch.setenv(seal_keys.ENV_KEY_FILE, str(old_key))
    old_entry = signer_for(home).registry_entry()
    old_run = _run(tmp_path, "old")
    seal_run(old_run, home=home)

    monkeypatch.setenv(seal_keys.ENV_KEY_FILE, str(new_key))
    new_entry = signer_for(home).registry_entry()
    new_run = _run(tmp_path, "new")
    seal_run(new_run, home=home)

    only_old = tmp_path / "only-old.json"
    only_old.write_text(json.dumps({"keys": [old_entry]}))
    assert verify_run(new_run, load_registry(only_old)).reason == "unknown_key"

    # Rotation: both keys stay pinned and active, so old and new Seals verify.
    both = tmp_path / "both.json"
    both.write_text(json.dumps({"keys": [old_entry, new_entry]}))
    assert verify_run(old_run, load_registry(both)).ok
    assert verify_run(new_run, load_registry(both)).key_id == new_entry["key_id"]
    # Revocation (a compromised key): every Seal it signed is refused, whatever
    # issued_at it claims, because its holder could write any issued_at.
    for revoked_at in ("2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z"):
        both.write_text(
            json.dumps(
                {
                    "keys": [
                        dict(old_entry, status="revoked", revoked_at=revoked_at),
                        new_entry,
                    ]
                }
            )
        )
        assert verify_run(old_run, load_registry(both)).reason == "revoked_key"
        assert verify_run(new_run, load_registry(both)).ok


def _cli(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    full = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OPENADAPT_SEAL") and not k.startswith("OPENADAPT_NO_OUT")
    }
    full.update(
        {"PYTHONPATH": os.pathsep.join([str(ROOT), full.get("PYTHONPATH", "")])}
    )
    full.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "local-seal",
            "--home",
            str(tmp_path / "home"),
            *args,
        ],
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_pin_revoke_and_verify_with_keys(tmp_path):
    key = _key_file(tmp_path)
    run = _run(tmp_path)
    registry = tmp_path / "site-keys.json"
    spec = f"file:{key}"
    assert _cli(tmp_path, "--key", spec, "run", str(run)).returncode == 0
    pinned = _cli(tmp_path, "--key", spec, "key", "pin", str(registry))
    assert pinned.returncode == 0, pinned.stderr
    ok = _cli(tmp_path, "verify", str(run), "--keys", str(registry))
    assert ok.returncode == 0 and "valid offline Seal" in ok.stdout
    assert str(run / "report.json") in ok.stdout
    # The original kept aside and an edited report.json: named, and --strict refuses.
    (run / "report.json").rename(run / "report.pre-seal.json")
    (run / "report.json").write_text('{"edited": true}')
    loose = _cli(tmp_path, "verify", str(run), "--keys", str(registry))
    assert loose.returncode == 0 and "report.pre-seal.json" in loose.stdout
    assert "report.json differs" in loose.stdout
    strict = _cli(tmp_path, "verify", str(run), "--keys", str(registry), "--strict")
    assert strict.returncode == 1 and "subject_mismatch" in strict.stderr
    (run / "report.pre-seal.json").replace(run / "report.json")
    # Without --key the verifier's own generated key is not the customer's.
    assert _cli(tmp_path, "verify", str(run)).returncode == 1
    key_id = json.loads(registry.read_text())["keys"][0]["key_id"]
    # Revoking at a future time still refuses now: issued_at is the signer's claim.
    revoked = _cli(
        tmp_path, "key", "revoke", str(registry), key_id, "--at", "2999-01-01T00:00:00Z"
    )
    assert revoked.returncode == 0, revoked.stderr
    refused = _cli(tmp_path, "verify", str(run), "--keys", str(registry))
    assert refused.returncode == 1 and "revoked_key" in refused.stderr
    missing = _cli(
        tmp_path,
        "--key",
        f"file:{tmp_path / 'gone.pem'}",
        "run",
        str(_run(tmp_path, "r2")),
    )
    assert missing.returncode == 1 and "key_unavailable" in missing.stderr


# --- OS keychain (keyring stand-in) ----------------------------------------


@pytest.fixture
def fake_keyring(monkeypatch):
    store: dict[tuple[str, str], str] = {}
    mod = types.ModuleType("keyring")
    mod.get_password = lambda service, name: store.get((service, name))  # type: ignore[attr-defined]
    mod.set_password = lambda service, name, value: store.__setitem__(  # type: ignore[attr-defined]
        (service, name), value
    )
    monkeypatch.setitem(sys.modules, "keyring", mod)
    return store


def test_keychain_key_signs_and_verifies(tmp_path, monkeypatch, fake_keyring):
    signer = create_key("keychain:site-a")
    assert (seal_keys.KEYCHAIN_SERVICE, "site-a") in fake_keyring
    with pytest.raises(KeyUnavailable):
        create_key("keychain:site-a")
    monkeypatch.setenv(seal_keys.ENV_KEY, "keychain:site-a")
    home = tmp_path / "home"
    run = _run(tmp_path)
    seal = seal_run(run, home=home)
    assert seal.key_id == signer.key_id
    registry = tmp_path / "keys.json"
    registry.write_text(json.dumps([signer.registry_entry()]))
    assert verify_run(run, load_registry(registry)).ok
    assert not (home / "keys").exists()


def test_missing_keychain_item_fails_closed(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv(seal_keys.ENV_KEY, "keychain:nobody")
    with pytest.raises(LocalSealError) as exc:
        seal_run(_run(tmp_path), home=tmp_path / "home")
    assert exc.value.code == "key_unavailable"
    assert not (tmp_path / "home" / "keys").exists()


def test_keychain_without_keyring_installed_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setenv(seal_keys.ENV_KEY, "keychain:site-a")
    with pytest.raises(LocalSealError) as exc:
        signer_for(tmp_path)
    assert exc.value.code == "key_unavailable" and "keychain" in str(exc.value)


@pytest.mark.skipif(
    os.environ.get("OPENADAPT_TEST_KEYCHAIN") != "1",
    reason="set OPENADAPT_TEST_KEYCHAIN=1 to use the real OS keychain",
)
def test_real_os_keychain_round_trip(tmp_path):  # pragma: no cover - opt-in
    import secrets

    import keyring

    name = f"openadapt-test-{secrets.token_hex(4)}"
    try:
        signer = create_key(f"keychain:{name}")
        again = signer_for(tmp_path, spec=f"keychain:{name}")
        assert again.key_id == signer.key_id
    finally:
        keyring.delete_password(seal_keys.KEYCHAIN_SERVICE, name)


# --- PKCS#11 (python-pkcs11 stand-in) --------------------------------------


class _FakeKey(dict):
    def __init__(self, private: Ed25519PrivateKey | None, point: bytes) -> None:
        super().__init__()
        self._private = private
        self.calls: list[dict] = []
        self[_Attr.EC_POINT] = point

    def sign(self, data, mechanism=None, mechanism_param=None):
        assert mechanism == _Mech.EDDSA
        assert self._private is not None
        return self._private.sign(data)


class _Attr:
    EC_POINT = "EC_POINT"


class _Mech:
    EDDSA = "EDDSA"


class _Cls:
    PRIVATE_KEY = "private"
    PUBLIC_KEY = "public"


def _fake_pkcs11(monkeypatch, *, pin: str, label: str = "seal", fail_open=False):
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    keys = {
        _Cls.PRIVATE_KEY: _FakeKey(private, b""),
        # CKA_EC_POINT as a DER OCTET STRING, the common token encoding.
        _Cls.PUBLIC_KEY: _FakeKey(None, b"\x04\x20" + raw),
    }

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_key(self, object_class=None, key_type=None, label=None, id=None):
            if label != wanted_label:
                raise LookupError("no such object")
            return keys[object_class]

    class Token:
        def open(self, user_pin=None):
            if user_pin != pin:
                raise PermissionError("CKR_PIN_INCORRECT")
            return Session()

    class Lib:
        def __init__(self, path):
            if fail_open:
                raise OSError("module not found")

        def get_token(self, token_label=None):
            if token_label != "SealToken":
                raise LookupError("no token")
            return Token()

    wanted_label = label
    mod = types.ModuleType("pkcs11")
    mod.lib = Lib  # type: ignore[attr-defined]
    mod.KeyType = types.SimpleNamespace(EC_EDWARDS="EC_EDWARDS")  # type: ignore[attr-defined]
    mod.ObjectClass = _Cls  # type: ignore[attr-defined]
    mod.Attribute = _Attr  # type: ignore[attr-defined]
    mod.Mechanism = _Mech  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pkcs11", mod)
    return raw


URI = "pkcs11:token=SealToken;object=seal?module-path=/opt/hsm/libsynthetic.so"


def test_pkcs11_key_signs_on_the_token(tmp_path, monkeypatch):
    raw = _fake_pkcs11(monkeypatch, pin="1234")
    monkeypatch.setenv(seal_keys.ENV_KEY, URI)
    monkeypatch.setenv(seal_keys.ENV_PKCS11_PIN, "1234")
    signer = signer_for(tmp_path / "home")
    assert isinstance(signer, Pkcs11Signer) and signer.public_raw() == raw
    run = _run(tmp_path)
    seal = seal_run(run, home=tmp_path / "home")
    registry = tmp_path / "keys.json"
    registry.write_text(json.dumps([signer.registry_entry()]))
    assert verify_run(run, load_registry(registry)).key_id == seal.key_id
    assert not (tmp_path / "home" / "keys").exists()


def test_pkcs11_pin_source_env(tmp_path, monkeypatch):
    _fake_pkcs11(monkeypatch, pin="9876")
    monkeypatch.setenv("SITE_HSM_PIN", "9876")
    signer = signer_for(tmp_path, spec=URI + "&pin-source=env:SITE_HSM_PIN")
    assert len(signer.sign(b"synthetic")) == 64


@pytest.mark.parametrize(
    "case",
    ["wrong_pin", "wrong_object", "module_missing", "library_missing"],
)
def test_pkcs11_unavailable_fails_closed(tmp_path, monkeypatch, case):
    if case == "library_missing":
        monkeypatch.setitem(sys.modules, "pkcs11", None)
    else:
        _fake_pkcs11(
            monkeypatch,
            pin="1234",
            label="other" if case == "wrong_object" else "seal",
            fail_open=case == "module_missing",
        )
    monkeypatch.setenv(seal_keys.ENV_KEY, URI)
    monkeypatch.setenv(
        seal_keys.ENV_PKCS11_PIN, "0000" if case == "wrong_pin" else "1234"
    )
    run = _run(tmp_path)
    with pytest.raises(LocalSealError) as exc:
        seal_run(run, home=tmp_path / "home")
    assert exc.value.code == "key_unavailable"
    assert not (run / "seal.json").exists()
    assert not (tmp_path / "home" / "keys").exists()


def test_pkcs11_binary_id_above_0x7f(tmp_path):
    signer = Pkcs11Signer.from_uri(
        "pkcs11:token=SealToken;id=%01%A2%FF?module-path=/opt/hsm/libsynthetic.so", {}
    )
    assert signer.object_id == b"\x01\xa2\xff"


def test_pkcs11_uri_refuses_pin_in_uri_and_missing_module(tmp_path):
    with pytest.raises(KeyUnavailable):
        Pkcs11Signer.from_uri(URI + "&pin-value=1234", {})
    with pytest.raises(KeyUnavailable):
        Pkcs11Signer.from_uri("pkcs11:token=SealToken;object=seal", {})


@pytest.mark.skipif(
    not os.environ.get("OPENADAPT_TEST_PKCS11_MODULE"),
    reason="set OPENADAPT_TEST_PKCS11_MODULE (for example SoftHSM2) to use a real token",
)
def test_real_pkcs11_token(tmp_path):  # pragma: no cover - opt-in
    # Expects token "openadapt-test" holding an Ed25519 key labelled "seal",
    # PIN in OPENADAPT_SEAL_PKCS11_PIN.
    uri = (
        "pkcs11:token=openadapt-test;object=seal?module-path="
        + os.environ["OPENADAPT_TEST_PKCS11_MODULE"]
    )
    signer = signer_for(tmp_path, spec=uri, env=dict(os.environ))
    assert len(signer.sign(b"synthetic")) == 64
