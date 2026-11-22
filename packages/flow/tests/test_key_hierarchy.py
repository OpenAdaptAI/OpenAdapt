"""Encrypted by default through the key hierarchy (sealed-set v3).

Every test injects device keys (hardware test doubles or the software key in
an in-memory keychain), and replaces the machine policy sources, so no test
reads the real OS keychain, the Secure Enclave or a TPM, or raises an OS
dialog.
"""

from __future__ import annotations

import base64
import json
import os
import plistlib
from pathlib import Path
from typing import Optional

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from openadapt_flow import at_rest, crypto, sealed_set
from openadapt_flow import key_hierarchy as kh

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic pixels"
TYPED = b"a synthetic typed value"


class MemoryKeychain(at_rest.KeyStore):
    """A keychain that lives in this test only."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.broken = False

    def get(self, account: str) -> Optional[str]:
        if self.broken:
            raise at_rest.KeyStoreUnavailable("no keychain backend (test)")
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
        kh.POLICY_ENV,
        kh.TRUST_USER_POLICY_ENV,
        kh.RECOVERY_CODE_ENV,
        kh.ORG_KEY_FILE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kh, "MACHINE_POLICY_SOURCES", [])
    root = tmp_path / "home"
    monkeypatch.setenv(at_rest.HOME_ENV, str(root))
    keychain = MemoryKeychain()
    at_rest.use_key_store(keychain)
    kh.use_device_keys(None)
    at_rest._reset_for_tests()
    yield root, keychain
    at_rest.use_key_store(None)
    kh.use_device_keys(None)
    at_rest._reset_for_tests()


def _seal(root: Path, files: dict[str, bytes], kind: str = "run") -> None:
    with sealed_set.writing(root, kind, sealed_set.passphrase_for(kind)) as writer:
        for rel, data in files.items():
            writer.write(rel, data)


def _header(root: Path) -> dict:
    return json.loads((root / sealed_set.HEADER).read_text())


def _assert_ciphertext_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert TYPED not in data and not data.startswith(PNG[:8]), path


def _make_archive(folder: Path) -> Path:
    """An organization key archive as an administrator sets it up."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / kh.ARCHIVE_MARKER).write_text("")
    return folder


def _p256_policy(
    tmp_path: Path, monkeypatch, **extra
) -> tuple[Path, ec.EllipticCurvePrivateKey]:
    if "key_archive" not in extra and not (tmp_path / "archive").exists():
        _make_archive(tmp_path / "archive")
    org = ec.generate_private_key(ec.SECP256R1())
    spki = org.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    policy = {
        "schema": kh.POLICY_SCHEMA,
        "org": "example-health",
        "mode": "org",
        "recipients": [
            {
                "role": "org",
                "status": "active",
                "type": kh.HPKE_P256,
                "public_key": base64.b64encode(spki).decode(),
                "fingerprint": kh.fingerprint(org.public_key()),
            }
        ],
        "key_archive": str(tmp_path / "archive"),
        **extra,
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    monkeypatch.setenv(kh.POLICY_ENV, str(path))
    monkeypatch.setenv(kh.TRUST_USER_POLICY_ENV, "1")
    key_file = tmp_path / "org-key.pem"
    key_file.write_bytes(
        org.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key_file, org


# -- defaults ---------------------------------------------------------------------


def test_a_new_run_is_v3_ciphertext_only_and_reads_back(home) -> None:
    root, keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b'{"typed": "%s"}' % TYPED, "steps/a.png": PNG})
    header = _header(run)
    assert header["version"] == sealed_set.ENVELOPE_VERSION
    assert header["payload"] == "tink-aes256-gcm-hkdf-1mb"
    assert header["stanzas"][0]["type"] == kh.STANZA_TYPE
    record = header["bucket_record"]  # solo mode: inline, travels with backups
    assert [s["role"] for s in record["stanzas"]] == [kh.ROLE_DEVICE_SOFTWARE]
    assert len(keychain.values) == 1  # the software device key, in the keychain
    _assert_ciphertext_only(run)
    assert sealed_set.read_bytes(run / "steps" / "a.png") == PNG


def test_another_computer_cannot_open_it(home, monkeypatch) -> None:
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b"{}"})
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "elsewhere"))
    at_rest.use_key_store(MemoryKeychain())
    with pytest.raises(sealed_set.SealedSetKeyError, match="no key on this computer"):
        sealed_set.SealedSet.open(run, None)


def test_a_header_rewrapped_by_someone_else_is_refused(home) -> None:
    """The bucket public key is not secret: a new DEK wrapped to it, with a
    DEK MAC, still lacks the bucket-key MAC."""
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b"real"})
    header = _header(run)
    public = serialization.load_der_public_key(
        base64.b64decode(header["bucket_record"]["bucket_pub"])
    )
    dek = os.urandom(32)
    header["stanzas"][0]["ct"] = base64.b64encode(
        kh.hpke_seal(
            public, dek, sealed_set._v3_wrap_info(header, header["stanzas"][0]["fpr"])
        )
    ).decode()
    forged = sealed_set.V3Key(dek, header["set_id"])
    forged.stamp(header)
    (run / sealed_set.HEADER).write_text(json.dumps(header))
    with pytest.raises(sealed_set.SealedSetError, match="MAC"):
        sealed_set.read_bytes(run / "report.json")


def test_an_edited_header_is_refused(home) -> None:
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": b"real"})
    header = _header(run)
    header["kind"] = "bundle"
    (run / sealed_set.HEADER).write_text(json.dumps(header))
    with pytest.raises(sealed_set.SealedSetError):
        sealed_set.read_bytes(run / "report.json")


def test_a_configured_key_keeps_the_v1_header(home, monkeypatch) -> None:
    root, keychain = home
    monkeypatch.setenv(crypto.ENV_KEY, "a-deployment-key-for-tests")
    run = root.parent / "run"
    _seal(run, {"report.json": b"{}"})
    assert _header(run)["version"] == 1
    assert keychain.values == {}


def test_a_legacy_plaintext_folder_and_key_file_still_work(home, monkeypatch) -> None:
    root, _keychain = home
    legacy = base64.urlsafe_b64encode(os.urandom(32)).decode()
    keys = root / "keys"
    keys.mkdir(parents=True, mode=0o700)
    (keys / at_rest.KEY_NAME).write_text(legacy + "\n")
    os.chmod(keys / at_rest.KEY_NAME, 0o600)
    assert crypto.resolve_key(None) == legacy
    old = root.parent / "old"
    monkeypatch.setenv(crypto.ENV_KEY, legacy)  # write the v1 set main wrote
    with sealed_set.writing(old, "run", legacy) as writer:
        writer.write("report.json", b"old")
    monkeypatch.delenv(crypto.ENV_KEY)
    assert _header(old)["version"] == 1
    assert sealed_set.read_bytes(old / "report.json") == b"old"
    plain = root.parent / "plain"
    plain.mkdir()
    (plain / "report.json").write_bytes(b"{}")
    assert sealed_set.read_bytes(plain / "report.json") == b"{}"


# -- hardware providers (test doubles) -------------------------------------------


def test_runtime_stanza_only_while_open_then_the_viewer_prompts(home) -> None:
    root, _keychain = home
    runtime = kh.HardwareDouble(kh.ROLE_RUNTIME)
    viewer = kh.HardwareDouble(kh.ROLE_VIEWER, presence=True)
    kh.use_device_keys(runtime, viewer)
    run = root.parent / "run"
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    roles = [s["role"] for s in writer._header["bucket_record"]["stanzas"]]
    assert roles == [kh.ROLE_RUNTIME, kh.ROLE_VIEWER]
    writer.write("report.json", TYPED)
    writer.finalize()
    roles = [s["role"] for s in _header(run)["bucket_record"]["stanzas"]]
    assert roles == [kh.ROLE_VIEWER]
    assert sealed_set.read_bytes(run / "report.json") == TYPED
    assert viewer.prompts == 1 and runtime.unwraps == 0
    viewer.deny = True
    with pytest.raises(sealed_set.SealedSetError, match="cancelled"):
        sealed_set.read_bytes(run / "report.json")


def test_a_bundle_keeps_its_runtime_stanza_for_unattended_replay(home) -> None:
    root, _keychain = home
    runtime = kh.HardwareDouble(kh.ROLE_RUNTIME)
    viewer = kh.HardwareDouble(kh.ROLE_VIEWER, presence=True)
    kh.use_device_keys(runtime, viewer)
    bundle = root.parent / "bundle"
    _seal(bundle, {"workflow.json": b"{}"}, kind="bundle")
    assert sealed_set.read_bytes(bundle / "workflow.json") == b"{}"
    assert viewer.prompts == 0 and runtime.unwraps == 1


def test_the_hardware_providers_are_not_built_and_fall_back_labelled(home) -> None:
    for provider in (
        kh.SecureEnclaveRuntimeKey(),
        kh.SecureEnclaveViewerKey(),
        kh.TpmRuntimeKey(),
        kh.WindowsHelloViewerKey(),
    ):
        assert provider.available() is False
    runtime, viewer = kh.device_keys(create=True)
    assert kh.is_software(runtime) and runtime is viewer
    assert kh.describe(kh.load_policy())["device_runtime_key"] == "software"


def test_require_hardware_refuses_the_software_key(home, tmp_path, monkeypatch) -> None:
    path = tmp_path / "solo-policy.json"
    path.write_text(
        json.dumps({"schema": kh.POLICY_SCHEMA, "require_hardware_device_key": True})
    )
    monkeypatch.setenv(kh.POLICY_ENV, str(path))
    with pytest.raises(sealed_set.SealedSetError, match="hardware device key"):
        _seal(home[0].parent / "run", {"report.json": b"{}"})


# -- the software device key in the keychain ------------------------------------------


def test_a_missing_keychain_entry_fails_closed_and_mints_nothing(home) -> None:
    root, keychain = home
    _seal(root.parent / "run", {"report.json": b"{}"})
    keychain.values.clear()
    with pytest.raises(sealed_set.SealedSetError, match="missing"):
        sealed_set.read_bytes(root.parent / "run" / "report.json")
    assert keychain.values == {}


def test_a_locked_keychain_fails_closed(home) -> None:
    root, keychain = home
    _seal(root.parent / "run", {"report.json": b"{}"})
    keychain.broken = True
    with pytest.raises(at_rest.LocalKeyError, match="Unlock the keychain"):
        at_rest.device_secret(create=False)


def test_no_keychain_falls_back_to_a_private_file_with_a_warning(home, capsys) -> None:
    root, keychain = home
    keychain.broken = True
    secret = at_rest.device_secret(create=True)
    path = root / "keys" / at_rest.DEVICE_KEY_NAME
    assert path.read_text().strip() == secret
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert "WARNING: no OS keychain is available" in capsys.readouterr().err


def test_tests_never_reach_the_real_keychain(monkeypatch) -> None:
    at_rest.use_key_store(None)
    monkeypatch.delenv(at_rest.KEY_STORE_ENV, raising=False)
    assert "PYTEST_CURRENT_TEST" in os.environ
    assert at_rest._store_choice() == "file"


# -- organization mode --------------------------------------------------------------


def test_org_mode_archives_and_the_org_opens_it_elsewhere(
    home, tmp_path, monkeypatch
) -> None:
    root, _keychain = home
    key_file, org = _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    header = _header(run)
    assert "bucket_record" not in header  # never travels with the evidence
    archived = json.loads(
        (tmp_path / "archive" / f"{header['key_ref']['bucket_id']}.json").read_text()
    )
    assert kh.ROLE_RUNTIME not in [s["role"] for s in archived["stanzas"]]
    assert kh.fingerprint(org.public_key()) in [s["fpr"] for s in archived["stanzas"]]
    _assert_ciphertext_only(run)
    # A reviewer on another computer: the archive copy and the org key.
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "reviewer"))
    at_rest.use_key_store(MemoryKeychain())
    buckets = root.parent / "reviewer" / "keys" / "buckets"
    buckets.mkdir(parents=True)
    (buckets / f"{header['key_ref']['bucket_id']}.json").write_text(
        json.dumps(archived)
    )
    monkeypatch.setenv(kh.ORG_KEY_FILE_ENV, str(key_file))
    assert sealed_set.read_bytes(run / "report.json") == TYPED


def test_org_mode_with_an_rsa_oaep_recipient(home, tmp_path, monkeypatch) -> None:
    root, _keychain = home
    org = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    spki = org.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema": kh.POLICY_SCHEMA,
                "mode": "org",
                "recipients": [
                    {
                        "role": "org",
                        "type": kh.RSA_OAEP_3072,
                        "public_key": base64.b64encode(spki).decode(),
                    }
                ],
                "key_archive": str(_make_archive(tmp_path / "archive")),
            }
        )
    )
    monkeypatch.setenv(kh.POLICY_ENV, str(policy))
    monkeypatch.setenv(kh.TRUST_USER_POLICY_ENV, "1")
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    bucket_id = _header(run)["key_ref"]["bucket_id"]
    archived = (tmp_path / "archive" / f"{bucket_id}.json").read_text()
    key_file = tmp_path / "rsa.pem"
    key_file.write_bytes(
        org.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "auditor"))
    at_rest.use_key_store(MemoryKeychain())
    buckets = root.parent / "auditor" / "keys" / "buckets"
    buckets.mkdir(parents=True)
    (buckets / f"{bucket_id}.json").write_text(archived)
    monkeypatch.setenv(kh.ORG_KEY_FILE_ENV, str(key_file))
    assert sealed_set.read_bytes(run / "report.json") == TYPED


def test_org_mode_fails_closed_without_its_recipient_or_archive(
    home, tmp_path, monkeypatch
) -> None:
    path = tmp_path / "policy.json"
    monkeypatch.setenv(kh.POLICY_ENV, str(path))
    monkeypatch.setenv(kh.TRUST_USER_POLICY_ENV, "1")
    path.write_text(
        json.dumps({"schema": kh.POLICY_SCHEMA, "mode": "org", "key_archive": "x"})
    )
    with pytest.raises(sealed_set.SealedSetError, match="no active organization"):
        _seal(home[0].parent / "a", {"report.json": b"{}"})
    _p256_policy(tmp_path, monkeypatch, key_archive=None)
    with pytest.raises(sealed_set.SealedSetError, match="no key_archive"):
        _seal(home[0].parent / "b", {"report.json": b"{}"})


def test_a_user_owned_org_policy_is_not_trusted(home, tmp_path, monkeypatch) -> None:
    if os.getuid() == 0:
        pytest.skip("running as root")
    _p256_policy(tmp_path, monkeypatch)
    monkeypatch.delenv(kh.TRUST_USER_POLICY_ENV)
    with pytest.raises(kh.KeyHierarchyError, match="not owned by root"):
        kh.load_policy()


def test_a_machine_policy_wins_over_the_user_one(home, tmp_path, monkeypatch) -> None:
    _p256_policy(tmp_path, monkeypatch)
    machine = tmp_path / "ai.openadapt.policy.plist"
    machine.write_bytes(
        plistlib.dumps(
            {"Policy": json.dumps({"schema": kh.POLICY_SCHEMA, "mode": "solo"})}
        )
    )
    monkeypatch.setattr(kh, "MACHINE_POLICY_SOURCES", [str(machine)])
    if os.name == "posix" and os.getuid() != 0:
        # A machine policy a user owns is refused, plist or not.
        with pytest.raises(kh.KeyHierarchyError, match="owned by root"):
            kh.load_policy()
    monkeypatch.setattr(kh, "_root_only", lambda info: True)  # as installed by MDM
    policy = kh.load_policy()
    assert policy.mode == "solo" and policy.source.startswith("machine:")


def test_an_unreachable_archive_counts_and_then_stops_recording(
    home, tmp_path, monkeypatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    _p256_policy(tmp_path, monkeypatch, key_archive=str(blocker), max_unarchived=1)
    _seal(home[0].parent / "a", {"report.json": b"{}"})
    assert kh.unarchived_count() == 1
    with pytest.raises(sealed_set.SealedSetError, match="not reached the organization"):
        _seal(home[0].parent / "b", {"report.json": b"{}"})


def test_shred_in_org_mode_destroys_every_tracked_copy(
    home, tmp_path, monkeypatch
) -> None:
    root, _keychain = home
    key_file, _org = _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    run.mkdir()
    (run / "checkpoints").mkdir()
    (run / "checkpoints" / "s1.enc").write_bytes(
        crypto.encrypt_bytes(b"{}", "k" * 20, aad=crypto.CHECKPOINT_AAD)
    )
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    writer.write("report.json", TYPED)
    writer.finalize(adopt_external=True)
    backup = root.parent / "backup"
    import shutil

    shutil.copytree(run, backup)
    record = sealed_set.shred(run)
    assert sorted(record["copies_destroyed"]) == ["archive", "local"]
    assert record["store_files_deleted"] == ["checkpoints/s1.enc"]
    monkeypatch.setenv(kh.ORG_KEY_FILE_ENV, str(key_file))
    with pytest.raises(sealed_set.SealedSetError, match="shredded"):
        sealed_set.read_bytes(run / "report.json")
    # The backup copy holds only ciphertext and a header no record opens.
    with pytest.raises(sealed_set.SealedSetError):
        sealed_set.read_bytes(backup / "report.json")


# -- solo recovery --------------------------------------------------------------------


def test_a_recovery_code_opens_a_backup_on_a_new_computer(home, monkeypatch) -> None:
    root, _keychain = home
    code, recipient = kh.new_recovery_code()
    assert len(code.replace("-", "")) == 52
    kh.save_recovery_recipient(recipient)
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "new-laptop"))
    at_rest.use_key_store(MemoryKeychain())
    with pytest.raises(sealed_set.SealedSetKeyError):
        sealed_set.read_bytes(run / "report.json")
    monkeypatch.setenv(kh.RECOVERY_CODE_ENV, code)
    assert sealed_set.read_bytes(run / "report.json") == TYPED


def test_solo_shred_reaches_only_this_copy(home) -> None:
    root, _keychain = home
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    record = sealed_set.shred(run)
    assert record["records"] == "inline"
    with pytest.raises(sealed_set.SealedSetError, match="shredded"):
        sealed_set.read_bytes(run / "report.json")


# -- the opt-out ------------------------------------------------------------------------


def test_encrypt_zero_turns_it_off_with_one_warning_line(
    home, capsys, monkeypatch
) -> None:
    root, _keychain = home
    monkeypatch.setenv(at_rest.ENCRYPT_ENV, "0")
    assert at_rest.seal_new("a recording", root / "rec") is False
    assert at_rest.seal_new("a recording", root / "rec") is False
    err = [line for line in capsys.readouterr().err.splitlines() if line]
    assert len(err) == 1 and err[0].startswith("WARNING:")
    ledger = (root / at_rest.LEDGER_NAME).read_text().splitlines()
    assert json.loads(ledger[0])["reason"] == "OPENADAPT_ENCRYPT_AT_REST=0"


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


def test_recovery_key_derivation_matches_rfc_9180() -> None:
    """DeriveKeyPair for DHKEM(P-256, HKDF-SHA256): RFC 9180 A.3.1 (ikmE, skEm)."""
    ikm = bytes.fromhex(
        "4270e54ffd08d79d5928020af4686d8f6b7d35dbe470265f1f5aa22816ce860e"
    )
    private = kh._hpke_derive_p256(ikm)
    assert private.private_numbers().private_value.to_bytes(32, "big").hex() == (
        "4995788ef4b9d6132b249ce59a77281493eb39af373d236a1fe415cb0c2d7beb"
    )


# -- review fixes: shred completeness, archive recovery, policy loading -----------


def test_a_shred_with_an_unreachable_archive_is_not_complete_until_retried(
    home, tmp_path, monkeypatch
) -> None:
    root, _keychain = home
    key_file, _org = _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    bucket_id = _header(run)["key_ref"]["bucket_id"]
    archived = tmp_path / "archive" / f"{bucket_id}.json"
    assert archived.is_file()
    os.chmod(tmp_path / "archive", 0o500)  # the archive goes read-only
    try:
        record = sealed_set.shred(run)
        assert record["complete"] is False and archived.is_file()
        assert kh.pending_shreds() == [bucket_id]
        assert sealed_set.shred(run)["complete"] is False  # still read-only
    finally:
        os.chmod(tmp_path / "archive", 0o700)
    assert sealed_set.shred(run)["complete"] is True
    assert not archived.exists() and kh.pending_shreds() == []


def test_the_cli_reports_an_incomplete_shred(
    home, tmp_path, monkeypatch, capsys
) -> None:
    from openadapt_flow.__main__ import main

    root, _keychain = home
    _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    os.chmod(tmp_path / "archive", 0o500)
    try:
        assert main(["at-rest", "shred", str(run), "--yes"]) == 1
        assert "NOT COMPLETE" in capsys.readouterr().err
    finally:
        os.chmod(tmp_path / "archive", 0o700)
    assert main(["at-rest", "shred", str(run), "--yes"]) == 0


def test_after_an_outage_the_pending_records_archive_and_recording_resumes(
    home, tmp_path, monkeypatch
) -> None:
    blocker = tmp_path / "archive"
    blocker.write_text("")  # unreachable: a file where the folder should be
    _p256_policy(tmp_path, monkeypatch, max_unarchived=1)
    _seal(home[0].parent / "a", {"report.json": b"{}"})
    assert kh.unarchived_count() == 1
    with pytest.raises(sealed_set.SealedSetError, match="not reached the organization"):
        _seal(home[0].parent / "b", {"report.json": b"{}"})
    blocker.unlink()  # the archive is back
    _make_archive(blocker)
    assert kh.archive_pending()["archived"] == 1
    assert kh.unarchived_count() == 0
    _seal(home[0].parent / "c", {"report.json": b"{}"})
    assert len(list((tmp_path / "archive").glob("*.json"))) == 2


def test_the_next_set_archives_pending_records_itself(
    home, tmp_path, monkeypatch
) -> None:
    blocker = tmp_path / "archive"
    blocker.write_text("")
    _p256_policy(tmp_path, monkeypatch, max_unarchived=1)
    _seal(home[0].parent / "a", {"report.json": b"{}"})
    blocker.unlink()
    _make_archive(blocker)
    _seal(home[0].parent / "b", {"report.json": b"{}"})  # no refusal
    assert kh.unarchived_count() == 0


def test_a_named_policy_file_that_is_missing_refuses(
    home, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(kh.POLICY_ENV, str(tmp_path / "gone.json"))
    with pytest.raises(sealed_set.SealedSetError, match="never falls back"):
        _seal(home[0].parent / "run", {"report.json": b"{}"})


# -- review round 3: archive evidence, shred authentication, races -------------


def test_an_unmounted_archive_is_never_the_archive(home, tmp_path, monkeypatch) -> None:
    """A mount point with nothing mounted is an empty local folder: records
    are not delivered there, and a record missing from it is not deleted."""
    root, _keychain = home
    mount = tmp_path / "mnt"
    mount.mkdir()  # no marker: the share is not mounted
    _p256_policy(tmp_path, monkeypatch, key_archive=str(mount))
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    assert kh.unarchived_count() == 1 and list(mount.iterdir()) == []
    record = sealed_set.shred(run)
    assert record["complete"] is False and "archive" not in record["copies_destroyed"]


def test_shred_uses_the_archive_the_set_was_written_to(
    home, tmp_path, monkeypatch
) -> None:
    root, _keychain = home
    _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    bucket_id = _header(run)["key_ref"]["bucket_id"]
    original = tmp_path / "archive" / f"{bucket_id}.json"
    (tmp_path / "archive" / kh.ARCHIVE_MARKER).unlink()  # the share drops
    assert sealed_set.shred(run)["complete"] is False
    # The organization moves to a new archive, then to solo mode: neither
    # may stand in for the archive that still holds the key.
    _make_archive(tmp_path / "new-archive")
    _p256_policy(tmp_path, monkeypatch, key_archive=str(tmp_path / "new-archive"))
    assert sealed_set.shred(run)["complete"] is False
    monkeypatch.delenv(kh.POLICY_ENV)
    assert kh.archive_pending()["shreds_pending"] == 1
    assert sealed_set.shred(run)["complete"] is False and original.is_file()
    _make_archive(tmp_path / "archive")  # the original share is back
    assert sealed_set.shred(run)["complete"] is True
    assert not original.exists() and kh.pending_shreds() == []


def test_shred_refuses_a_header_pointed_at_another_sets_key(
    home, tmp_path, monkeypatch
) -> None:
    root, _keychain = home
    _p256_policy(tmp_path, monkeypatch)
    a, b = root.parent / "a", root.parent / "b"
    _seal(a, {"report.json": TYPED})
    _seal(b, {"report.json": TYPED})
    header = _header(a)
    header["key_ref"] = _header(b)["key_ref"]
    (a / sealed_set.HEADER).write_text(json.dumps(header))
    with pytest.raises(sealed_set.SealedSetError):
        sealed_set.shred(a)
    assert sealed_set.read_bytes(b / "report.json") == TYPED


def test_an_interrupted_shred_finishes_its_cleanup_on_retry(
    home, tmp_path, monkeypatch
) -> None:
    root, _keychain = home
    _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    run.mkdir()
    (run / "checkpoints").mkdir()
    (run / "checkpoints" / "s1.enc").write_bytes(
        crypto.encrypt_bytes(b"{}", "k" * 20, aad=crypto.CHECKPOINT_AAD)
    )
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    writer.write("report.json", TYPED)
    writer.finalize(adopt_external=True)
    real = sealed_set._delete_store_files

    def crash(base: Path) -> list[str]:
        raise KeyboardInterrupt  # the process dies before the stores go

    monkeypatch.setattr(sealed_set, "_delete_store_files", crash)
    with pytest.raises(KeyboardInterrupt):
        sealed_set.shred(run)
    assert _header(run)["shred_record"]["complete"] is False
    assert (run / "checkpoints" / "s1.enc").exists()
    monkeypatch.setattr(sealed_set, "_delete_store_files", real)
    record = sealed_set.shred(run)
    assert record["complete"] is True
    assert record["store_files_deleted"] == ["checkpoints/s1.enc"]


def test_a_shredded_record_is_never_written_back(home, tmp_path, monkeypatch) -> None:
    """A writer still finalizing, or an archive worker holding the record in
    memory, must not resurrect a key that a shred destroyed."""
    root, _keychain = home
    _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    writer.write("report.json", TYPED)
    bucket = writer._key.bucket
    kh.shred(bucket.bucket_id, records="archive", key_archive=str(tmp_path / "archive"))
    assert kh.archive(bucket) is False
    with pytest.raises(sealed_set.SealedSetError, match="shredded"):
        writer.finalize()
    assert not (kh.buckets_dir() / f"{bucket.bucket_id}.json").exists()
    assert not (tmp_path / "archive" / f"{bucket.bucket_id}.json").exists()


def test_a_whole_replacement_set_does_not_open_with_the_org_key(
    home, tmp_path, monkeypatch
) -> None:
    """Anyone who knows the organization's public key can wrap a bucket to it.
    A record that arrives in a header is never opened with that key."""
    root, _keychain = home
    key_file, org = _p256_policy(tmp_path, monkeypatch)
    forged_policy = kh.Policy(
        mode="solo",
        records="inline",
        recipients=[kh.Recipient(kh.ROLE_ORG, kh.HPKE_P256, org.public_key())],
    )
    monkeypatch.setattr(kh, "load_policy", lambda: forged_policy)
    forged = root.parent / "forged"
    _seal(forged, {"report.json": b"forged evidence"})
    org_fprs = [s["fpr"] for s in _header(forged)["bucket_record"]["stanzas"]]
    assert kh.fingerprint(org.public_key()) in org_fprs
    # The reviewer, on another computer, with the organization's key.
    monkeypatch.setenv(at_rest.HOME_ENV, str(root.parent / "reviewer"))
    at_rest.use_key_store(MemoryKeychain())
    monkeypatch.setenv(kh.ORG_KEY_FILE_ENV, str(key_file))
    with pytest.raises(sealed_set.SealedSetKeyError):
        sealed_set.read_bytes(forged / "report.json")


def test_organization_mode_requires_the_archive(home, tmp_path, monkeypatch) -> None:
    _p256_policy(tmp_path, monkeypatch, records="inline")
    with pytest.raises(kh.KeyHierarchyError, match="solo mode only"):
        kh.load_policy()


def test_a_zero_backlog_needs_the_archive_for_every_set(
    home, tmp_path, monkeypatch
) -> None:
    _p256_policy(tmp_path, monkeypatch, max_unarchived=0)
    _seal(home[0].parent / "a", {"report.json": b"{}"})  # healthy archive
    assert kh.unarchived_count() == 0
    (tmp_path / "archive" / kh.ARCHIVE_MARKER).unlink()
    with pytest.raises(sealed_set.SealedSetError, match="allows 0 waiting"):
        _seal(home[0].parent / "b", {"report.json": b"{}"})
    assert kh.unarchived_count() == 0  # the refused set left no record


def test_concurrent_writers_cannot_share_the_last_backlog_slot(
    home, tmp_path, monkeypatch
) -> None:
    import threading

    mount = tmp_path / "mnt"
    mount.mkdir()
    _p256_policy(tmp_path, monkeypatch, key_archive=str(mount), max_unarchived=1)
    kh.load_policy()  # warm imports outside the race
    results: list[str] = []
    barrier = threading.Barrier(4)

    def write(index: int) -> None:
        barrier.wait()
        try:
            kh.new_bucket("run")
            results.append("ok")
        except kh.KeyHierarchyError:
            results.append("refused")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok", "refused", "refused", "refused"]
    assert kh.unarchived_count() == 1


def test_a_lost_keychain_entry_does_not_hide_the_recovery_code(
    home, monkeypatch
) -> None:
    root, keychain = home
    code, recipient = kh.new_recovery_code()
    kh.save_recovery_recipient(recipient)
    run = root.parent / "run"
    _seal(run, {"report.json": TYPED})
    keychain.values.clear()  # a restored home: the marker, not the entry
    with pytest.raises(sealed_set.SealedSetError, match="missing"):
        sealed_set.read_bytes(run / "report.json")
    monkeypatch.setenv(kh.RECOVERY_CODE_ENV, code)
    assert sealed_set.read_bytes(run / "report.json") == TYPED
    # Writing still needs the device key, and still fails closed without it.
    with pytest.raises((sealed_set.SealedSetError, at_rest.LocalKeyError)):
        _seal(root.parent / "new", {"report.json": b"{}"})


def test_each_platform_reads_only_its_own_policy_sources() -> None:
    assert kh._platform_policy_sources("win32") == ["HKLM\\SOFTWARE\\Policies\\OpenAdapt"]
    assert all(
        not s.startswith("HKLM") for s in kh._platform_policy_sources("darwin")
    )
    assert kh._platform_policy_sources("linux") == ["/etc/openadapt/policy.json"]


def test_a_root_owned_policy_others_can_write_is_not_trusted() -> None:
    def info(mode: int, uid: int = 0) -> os.stat_result:
        return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    assert kh._root_only(info(0o100644)) is True
    assert kh._root_only(info(0o100664)) is False  # group-writable
    assert kh._root_only(info(0o100646)) is False  # world-writable
    assert kh._root_only(info(0o100644, uid=501)) is False


def test_a_registry_policy_the_account_cannot_read_is_an_error(monkeypatch) -> None:
    """Access denied under HKLM never reads as "no policy" (solo fallback)."""
    import sys
    import types

    class Handle:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(hive, subkey):
        if subkey.endswith("Missing"):
            raise FileNotFoundError(subkey)
        if subkey.endswith("Denied"):
            raise PermissionError("access denied")
        return Handle()

    fake = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=open_key,
        QueryValueEx=lambda handle, name: (
            json.dumps({"schema": kh.POLICY_SCHEMA, "mode": "solo"}),
            1,
        ),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(kh.sys, "platform", "win32")
    assert kh._read_machine_source("HKLM\\SOFTWARE\\Missing") is None
    assert kh._read_machine_source("HKLM\\SOFTWARE\\Present")["mode"] == "solo"
    monkeypatch.setattr(kh, "MACHINE_POLICY_SOURCES", ["HKLM\\SOFTWARE\\Denied"])
    with pytest.raises(kh.KeyHierarchyError, match="can't be read"):
        kh.load_policy()


def test_a_v3_runs_checkpoints_are_keyed_to_the_run_and_die_with_it(
    home, tmp_path, monkeypatch
) -> None:
    import shutil

    from pydantic import BaseModel

    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

    class Bindings(BaseModel):
        typed: str

    root, _keychain = home
    _p256_policy(tmp_path, monkeypatch)
    run = root.parent / "run"
    writer = sealed_set.SealedSetWriter(run, "run", sealed_set.passphrase_for("run"))
    local = at_rest.local_key()
    store = CheckpointStore(run, key=local)  # whatever key the caller resolved
    assert store.key == sealed_set.store_passphrase_of(writer) != local
    (run / "checkpoints").mkdir()
    written = store._write_model(
        run / "checkpoints" / "s1.json", Bindings(typed=TYPED.decode())
    )
    sealed = written.read_bytes()
    assert TYPED not in sealed
    with pytest.raises(crypto.CryptoError):  # the long-lived key never opens it
        crypto.decrypt_bytes(sealed, local, aad=crypto.CHECKPOINT_AAD)
    writer.write("report.json", TYPED)
    writer.finalize(adopt_external=True)
    backup = root.parent / "backup"
    shutil.copytree(run, backup)
    sealed_set.shred(run)
    # Organization mode: the backup's header names a bucket whose every
    # tracked key copy is gone, so its checkpoints are gone too.
    with pytest.raises(sealed_set.SealedSetError):
        CheckpointStore(backup, key=local)


def test_a_checkpoint_store_without_a_key_encrypts_by_default(home) -> None:
    from pydantic import BaseModel

    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

    class Bindings(BaseModel):
        typed: str

    root, _keychain = home
    plain = root.parent / "plain-run"
    plain.mkdir()
    written = CheckpointStore(plain)._write_model(
        plain / "s1.json", Bindings(typed=TYPED.decode())
    )
    assert written.name.endswith(".enc") and TYPED not in written.read_bytes()
