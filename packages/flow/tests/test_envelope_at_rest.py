"""Encrypted by default: the device key, recipients, sharing and shredding.

Every test injects an in-memory keychain (or forces the file store), so no
test reads or writes the real OS keychain or raises an OS dialog.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

import pyrage
import pytest

from openadapt_flow import at_rest, crypto, envelope, sealed_set

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic pixels"


class MemoryKeychain(at_rest.KeyStore):
    """A keychain that lives in this test only."""

    def __init__(self, *, broken: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.broken = broken
        self.reads = 0

    def get(self, account: str) -> Optional[str]:
        if self.broken:
            raise at_rest.KeyStoreUnavailable("no keychain backend (test)")
        self.reads += 1
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        if self.broken:
            raise at_rest.KeyStoreUnavailable("no keychain backend (test)")
        self.values[account] = value


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in (
        at_rest.OPT_OUT_ENV,
        at_rest.MODE_ENV,
        at_rest.ENCRYPT_ENV,
        at_rest.KEY_STORE_ENV,
        crypto.ENV_KEY,
        sealed_set.RECORDING_ENV_KEY,
        envelope.ORG_RECIPIENTS_ENV,
        envelope.IDENTITY_FILE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(envelope.POLICY_FILE_ENV, str(tmp_path / "no-policy.json"))
    root = tmp_path / "home"
    monkeypatch.setenv(at_rest.HOME_ENV, str(root))
    keychain = MemoryKeychain()
    at_rest.use_key_store(keychain)
    at_rest._reset_for_tests()
    yield root, keychain
    at_rest.use_key_store(None)
    at_rest._reset_for_tests()


def _seal(root: Path, files: dict[str, bytes], kind: str = "run") -> None:
    with sealed_set.writing(root, kind, sealed_set.passphrase_for(kind)) as writer:
        for rel, data in files.items():
            writer.write(rel, data)


def _header(root: Path) -> dict:
    return json.loads((root / sealed_set.HEADER).read_text())


def _on_disk(root: Path) -> list[bytes]:
    return [p.read_bytes() for p in root.rglob("*") if p.is_file()]


# -- the device key ------------------------------------------------------------


def test_device_key_goes_to_the_keychain_with_a_marker_only(home, capsys) -> None:
    root, keychain = home
    first = at_rest.device_secret(create=True)
    assert first and first.startswith("AGE-SECRET-KEY-1")
    assert list(keychain.values.values()) == [first]
    marker = json.loads((root / "keys" / at_rest.MARKER_NAME).read_text())
    assert marker["store"] == "keychain"
    assert first not in json.dumps(marker)  # a check value, never the key
    assert not (root / "keys" / at_rest.DEVICE_KEY_NAME).exists()
    assert not (root / "keys" / at_rest.KEY_NAME).exists()
    assert at_rest.device_secret(create=True) == first
    assert "OS keychain entry" in capsys.readouterr().err


def test_a_missing_keychain_entry_fails_closed_and_mints_nothing(home) -> None:
    root, keychain = home
    at_rest.device_secret(create=True)
    keychain.values.clear()
    with pytest.raises(at_rest.LocalKeyError, match="missing"):
        at_rest.local_key()
    assert keychain.values == {}
    assert not (root / "keys" / at_rest.DEVICE_KEY_NAME).exists()


def test_a_replaced_keychain_entry_fails_closed(home) -> None:
    _root, keychain = home
    at_rest.device_secret(create=True)
    account = next(iter(keychain.values))
    keychain.values[account] = str(pyrage.x25519.Identity.generate())
    with pytest.raises(at_rest.LocalKeyError, match="does not hold"):
        at_rest.device_secret(create=False)


def test_a_locked_keychain_fails_closed_once_the_key_lives_there(home) -> None:
    _root, keychain = home
    at_rest.device_secret(create=True)
    keychain.broken = True
    with pytest.raises(at_rest.LocalKeyError, match="Unlock the keychain"):
        at_rest.existing_key()


def test_no_keychain_falls_back_to_a_private_file_with_a_warning(home, capsys) -> None:
    root, keychain = home
    keychain.broken = True
    secret = at_rest.device_secret(create=True)
    path = root / "keys" / at_rest.DEVICE_KEY_NAME
    assert path.read_text().strip() == secret
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert "WARNING: no OS keychain is available" in capsys.readouterr().err


def test_keychain_only_refuses_without_a_keychain(home, monkeypatch) -> None:
    _root, keychain = home
    keychain.broken = True
    monkeypatch.setenv(at_rest.KEY_STORE_ENV, "keychain")
    with pytest.raises(at_rest.LocalKeyError, match="keychain"):
        at_rest.device_secret(create=True)


def test_a_key_file_from_an_earlier_version_keeps_working(home) -> None:
    root, keychain = home
    legacy = base64.urlsafe_b64encode(os.urandom(32)).decode()
    keys = root / "keys"
    keys.mkdir(parents=True, mode=0o700)
    (keys / at_rest.KEY_NAME).write_text(legacy + "\n")
    os.chmod(keys / at_rest.KEY_NAME, 0o600)
    assert at_rest.local_key() == legacy
    assert crypto.resolve_key(None) == legacy
    assert keychain.values == {}


def test_tests_never_reach_the_real_keychain(monkeypatch) -> None:
    at_rest.use_key_store(None)
    monkeypatch.delenv(at_rest.KEY_STORE_ENV, raising=False)
    assert "PYTEST_CURRENT_TEST" in os.environ
    assert at_rest._store_choice() == "file"


# -- the opt-out -----------------------------------------------------------------


def test_encrypt_zero_turns_it_off_with_one_warning_line(home, capsys) -> None:
    root, _keychain = home
    os.environ[at_rest.ENCRYPT_ENV] = "0"
    try:
        assert at_rest.seal_new("a recording", root / "rec") is False
        assert at_rest.seal_new("a recording", root / "rec") is False
        err = [line for line in capsys.readouterr().err.splitlines() if line]
        assert len(err) == 1 and err[0].startswith("WARNING:")
        ledger = (root / at_rest.LEDGER_NAME).read_text().splitlines()
        assert json.loads(ledger[0])["reason"] == "OPENADAPT_ENCRYPT_AT_REST=0"
    finally:
        del os.environ[at_rest.ENCRYPT_ENV]


def test_a_mistyped_encrypt_setting_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(at_rest.ENCRYPT_ENV, "nope")
    with pytest.raises(ValueError):
        at_rest.opt_out_reason()


def test_strict_mode_ignores_the_opt_out(home, monkeypatch) -> None:
    monkeypatch.setenv(at_rest.ENCRYPT_ENV, "0")
    monkeypatch.setenv(at_rest.MODE_ENV, "strict")
    assert at_rest.seal_new("a recording", "x") is True


def test_new_artifacts_are_sealed_by_default(home) -> None:
    assert at_rest.DEFAULT_SEALS_ARTIFACTS is True
    assert at_rest.seals_by_default() is True


# -- recipients ------------------------------------------------------------------


def test_a_new_run_is_wrapped_for_the_device_only_ciphertext_on_disk(home) -> None:
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b'{"typed": "a synthetic value"}', "steps/a.png": PNG})
    header = _header(run)
    assert header["version"] == sealed_set.ENVELOPE_VERSION
    device = at_rest.device_secret(create=False)
    assert header["recipients"] == [envelope.device_recipient(device)]
    assert "passphrase" not in header and "device_mac" in header
    for data in _on_disk(run):
        assert b"a synthetic value" not in data and not data.startswith(PNG[:8])
    assert sealed_set.read_bytes(run / "steps" / "a.png") == PNG


def test_another_computer_cannot_open_it(home, monkeypatch) -> None:
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b"{}"})
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "elsewhere"))
    with pytest.raises(sealed_set.SealedSetKeyError, match="no key on this computer"):
        sealed_set.SealedSet.open(run, None)


def test_an_org_recipient_reads_it_without_the_device(home, monkeypatch) -> None:
    root, _keychain = home
    org = pyrage.x25519.Identity.generate()
    monkeypatch.setenv(envelope.ORG_RECIPIENTS_ENV, str(org.to_public()))
    run = root.parent / "run"
    _seal(run, {"report.json": b"evidence"})
    assert str(org.to_public()) in sealed_set.recipients_of(run)
    identity = root.parent / "reviewer.txt"
    identity.write_text(f"# reviewer\n{org}\n")
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "reviewer-home"))
    monkeypatch.setenv(envelope.IDENTITY_FILE_ENV, str(identity))
    assert sealed_set.SealedSet.open(run, None).read_bytes("report.json") == b"evidence"


def test_an_org_recipient_from_the_policy_file(home, monkeypatch, tmp_path) -> None:
    org = pyrage.x25519.Identity.generate()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"recipients": [str(org.to_public())]}))
    monkeypatch.setenv(envelope.POLICY_FILE_ENV, str(policy))
    assert envelope.org_recipients() == [str(org.to_public())]


def test_a_malformed_org_recipient_refuses_the_write(home, monkeypatch) -> None:
    root, _keychain = home
    monkeypatch.setenv(envelope.ORG_RECIPIENTS_ENV, "age1notarecipient")
    with pytest.raises(sealed_set.SealedSetError, match="not an age recipient"):
        _seal(root.parent / "run", {"report.json": b"{}"})


def test_share_rewraps_without_reencrypting(home, monkeypatch) -> None:
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b"shared"})
    before = (run / "report.json.enc").read_bytes()
    friend = pyrage.x25519.Identity.generate()
    sealed_set.add_recipients(run, [str(friend.to_public())])
    assert (run / "report.json.enc").read_bytes() == before
    assert sealed_set.read_bytes(run / "report.json") == b"shared"
    identity = root.parent / "friend.txt"
    identity.write_text(str(friend))
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "friend-home"))
    monkeypatch.setenv(envelope.IDENTITY_FILE_ENV, str(identity))
    assert sealed_set.read_bytes(run / "report.json") == b"shared"


def test_shred_makes_it_unreadable_and_drops_checkpoints(home) -> None:
    root, _keychain = home
    run = root.parent / "run"
    run.mkdir()
    (run / "checkpoints").mkdir()
    checkpoint = crypto.encrypt_bytes(b"{}", "k" * 20, aad=crypto.CHECKPOINT_AAD)
    (run / "checkpoints" / "s1.enc").write_bytes(checkpoint)
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    writer.write("report.json", b"gone")
    writer.finalize(adopt_external=True)
    removed = sealed_set.shred(run)
    assert removed == ["checkpoints/s1.enc"]
    assert not (run / "checkpoints" / "s1.enc").exists()
    with pytest.raises(sealed_set.SealedSetError, match="shredded"):
        sealed_set.read_bytes(run / "report.json")
    assert "age" not in _header(run)


def test_shred_deletes_other_stores_ciphertext(home) -> None:
    root, _keychain = home
    run = root.parent / "run"
    run.mkdir()
    (run / "pending.json").write_bytes(
        crypto.encrypt_bytes(b"{}", "k" * 20, aad=crypto.CHECKPOINT_AAD)
    )
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    writer.write("report.json", b"gone")
    writer.finalize(adopt_external=True)
    assert sealed_set.shred(run) == ["pending.json"]
    assert not (run / "pending.json").exists()


def test_a_forged_header_for_this_device_is_refused(home) -> None:
    """age has no sender authentication: the device MAC is what stops a
    header someone else wrapped for this computer's public recipient."""
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b"real"})
    header = _header(run)
    forged = dict(header)
    forged["age"] = envelope.wrap(os.urandom(32), forged, forged["recipients"])
    (run / sealed_set.HEADER).write_text(json.dumps(forged))
    with pytest.raises(sealed_set.SealedSetError, match="device MAC"):
        sealed_set.read_bytes(run / "report.json")
    forged.pop("device_mac")
    (run / sealed_set.HEADER).write_text(json.dumps(forged))
    with pytest.raises(sealed_set.SealedSetError, match="device MAC"):
        sealed_set.read_bytes(run / "report.json")


def test_a_configured_key_keeps_the_header_flow_always_wrote(home, monkeypatch) -> None:
    root, keychain = home
    monkeypatch.setenv(crypto.ENV_KEY, "a-deployment-key-for-tests")
    run = root.parent / "run"
    _seal(run, {"report.json": b"{}"})
    assert _header(run)["version"] == 1
    assert keychain.values == {}  # no device key was needed


def test_a_configured_key_and_an_org_recipient_both_open_it(home, monkeypatch) -> None:
    root, _keychain = home
    org = pyrage.x25519.Identity.generate()
    monkeypatch.setenv(crypto.ENV_KEY, "a-deployment-key-for-tests")
    monkeypatch.setenv(envelope.ORG_RECIPIENTS_ENV, str(org.to_public()))
    run = root.parent / "run"
    _seal(run, {"report.json": b"both"})
    header = _header(run)
    assert header["version"] == 3 and "passphrase" in header
    assert header["recipients"] == [str(org.to_public())]
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "other"))
    assert sealed_set.read_bytes(run / "report.json") == b"both"
    monkeypatch.setenv(crypto.ENV_KEY, "the-wrong-key-entirely")
    with pytest.raises((sealed_set.SealedSetError, crypto.CryptoError)):
        sealed_set.SealedSet.open(run, None)


def test_a_legacy_plaintext_folder_still_reads(home) -> None:
    root, _keychain = home
    plain = root.parent / "old-run"
    plain.mkdir()
    (plain / "report.json").write_bytes(b"{}")
    assert sealed_set.read_bytes(plain / "report.json") == b"{}"
