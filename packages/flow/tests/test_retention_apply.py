"""Synthetic sealed runs and ephemeral signing keys on a real temporary FS."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.seal import parse_key_registry

from openadapt_flow import retention_apply, sealed_set
from openadapt_flow.retention import RetentionError
from openadapt_flow.retention_apply import apply_retention, prepare_retention
from openadapt_flow.seal_keys import SoftwareSigner
from tests.test_retention import ISSUED, files, fixture, plan

PROTECT = "synthetic-capsule-protection-1900"
PREPARED = ISSUED + timedelta(days=30)
DUE = PREPARED + timedelta(days=3)


def prepared(tmp_path):
    root, source_seal, registry = fixture(tmp_path)
    source = plan(root, source_seal, registry, now=PREPARED)
    signer = SoftwareSigner(Ed25519PrivateKey.generate(), "generated")
    capsule = prepare_retention(
        source,
        registry,
        key="synthetic-retention-1900-only",
        protection_key=PROTECT,
        signer=signer,
        now=PREPARED,
    )
    registry = [*registry, *parse_key_registry([signer.registry_entry()])]
    return root, capsule, registry, tmp_path / "audit-capsule.json"


def apply(root, capsule, registry, audit, *, now=DUE, commit=False):
    return apply_retention(
        root,
        capsule.canonical_json(),
        audit,
        registry,
        protection_key=PROTECT,
        now=now,
        commit=commit,
    )


def test_default_dry_run_and_preparation_time_not_old_report_age(tmp_path):
    root, capsule, registry, audit = prepared(tmp_path)
    before = files(tmp_path)
    assert capsule.receipt.prepared_at == PREPARED.isoformat().replace("+00:00", "Z")
    assert not apply(root, capsule, registry, audit, now=PREPARED)["eligible"]
    assert not apply(
        root, capsule, registry, audit, now=DUE - timedelta(microseconds=1)
    )["eligible"]
    preview = apply(root, capsule, registry, audit)
    assert preview["dry_run"] and preview["eligible"]
    assert len(preview["would_delete"]) == 3
    assert files(tmp_path) == before
    assert not audit.exists()
    with pytest.raises(RetentionError, match="retention_not_elapsed"):
        apply(root, capsule, registry, audit, now=PREPARED, commit=True)
    assert files(tmp_path) == before


def test_explicit_apply_preserves_exact_signed_capsule_and_original_header(tmp_path):
    root, capsule, registry, audit = prepared(tmp_path)
    header = (root / sealed_set.HEADER).read_bytes()
    result = apply(root, capsule, registry, audit, commit=True)
    assert result["completed"] and result["remaining_files"] == 0
    assert result["removed_files"] == 3
    assert result["media_sanitization_claimed"] is False
    assert audit.read_bytes() == capsule.canonical_json()
    assert (root / sealed_set.HEADER).read_bytes() == header
    assert set(files(root)) == {sealed_set.HEADER}
    assert os.stat(audit).st_mode & 0o777 == 0o600
    with pytest.raises(sealed_set.SealedSetError):
        sealed_set.SealedSet.open(root, "synthetic-retention-1900-only")
    assert apply(root, capsule, registry, audit, commit=True)["removed_files"] == 0
    projection = capsule.receipt.canonical_json()
    for forbidden in (
        b"directory-label",
        b"SYNTHETIC-RETENTION-PRIVATE-TEXT",
        b"synthetic-private-pixels",
        b"snapshots/dom",
        b"00000000-0000-4000",
    ):
        assert forbidden not in projection
        assert forbidden not in capsule.canonical_json()


@pytest.mark.parametrize("stage", ["capsule", "claim", "unlink"])
def test_real_filesystem_interruption_resumes_exact_persisted_authorization(
    tmp_path, monkeypatch, stage
):
    root, capsule, registry, audit = prepared(tmp_path)
    if stage == "capsule":
        original = retention_apply._persist

        def stop(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic-after-capsule")

        monkeypatch.setattr(retention_apply, "_persist", stop)
    elif stage == "claim":
        original = retention_apply.rename_claim

        def stop(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic-after-claim")

        monkeypatch.setattr(retention_apply, "rename_claim", stop)
    else:
        original = os.unlink

        def stop(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if str(path).startswith(".openadapt-retention-"):
                raise RuntimeError("synthetic-after-unlink")
            return result

        monkeypatch.setattr(retention_apply.os, "unlink", stop)
        # This forwarding wrapper retains dir_fd support. Keep the production
        # platform capability check active while instrumenting the real unlink.
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {stop})
    with pytest.raises(RuntimeError, match="synthetic-after"):
        apply(root, capsule, registry, audit, commit=True)
    assert audit.read_bytes() == capsule.canonical_json()
    assert (root / sealed_set.HEADER).exists()
    monkeypatch.undo()
    assert apply(root, capsule, registry, audit, commit=True)["completed"]
    assert set(files(root)) == {sealed_set.HEADER}


def test_audit_publication_failure_never_removes_or_claims_raw_files(
    tmp_path, monkeypatch
):
    root, capsule, registry, audit = prepared(tmp_path)
    before = files(root)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-capsule-publication-failed")

    monkeypatch.setattr(retention_apply.os, "link", fail)
    with pytest.raises(RuntimeError, match="publication-failed"):
        apply(root, capsule, registry, audit, commit=True)
    assert files(root) == before
    assert not audit.exists()


@pytest.mark.parametrize(
    "change", ["payload", "header", "extra", "missing", "hardlink", "symlink"]
)
def test_stale_or_unbound_source_refuses_before_audit_publication(tmp_path, change):
    root, capsule, registry, audit = prepared(tmp_path)
    target = root / "steps/000_before.png.enc"
    if change == "payload":
        target.write_bytes(b"changed-synthetic-ciphertext")
    elif change == "header":
        (root / sealed_set.HEADER).write_bytes(b"changed-header")
    elif change == "extra":
        (root / "extra.enc").write_bytes(b"unbound")
    elif change == "missing":
        target.unlink()
    elif change == "hardlink":
        os.link(target, tmp_path / "other-copy")
    else:
        target.unlink()
        target.symlink_to(tmp_path / "outside")
    before = files(root)
    with pytest.raises(RetentionError):
        apply(root, capsule, registry, audit, commit=True)
    assert files(root) == before
    assert not audit.exists()


def test_raced_replacement_is_preserved_after_claim_not_deleted(tmp_path, monkeypatch):
    root, capsule, registry, audit = prepared(tmp_path)
    original = retention_apply.rename_claim

    def race(parent_fd, source, destination):
        # An uncooperative actor replaces the original between preflight and
        # claim. The claimed bytes are rechecked before unlink and preserved.
        fd = os.open(source, os.O_WRONLY | os.O_TRUNC, dir_fd=parent_fd)
        os.write(fd, b"SYNTHETIC-UNAUTHORIZED-REPLACEMENT")
        os.close(fd)
        return original(parent_fd, source, destination)

    monkeypatch.setattr(retention_apply, "rename_claim", race)
    with pytest.raises(RetentionError, match="source_changed_after_claim"):
        apply(root, capsule, registry, audit, commit=True)
    assert any(
        b"SYNTHETIC-UNAUTHORIZED-REPLACEMENT" == value for value in files(root).values()
    )
    assert audit.read_bytes() == capsule.canonical_json()


def test_conflicting_or_in_source_audit_path_never_overwrites_evidence(tmp_path):
    root, capsule, registry, audit = prepared(tmp_path)
    before = files(root)
    audit.write_bytes(b"preexisting-unrelated-evidence")
    with pytest.raises(RetentionError, match="audit_capsule_conflict"):
        apply(root, capsule, registry, audit, commit=True)
    with pytest.raises(RetentionError, match="outside_source"):
        apply(root, capsule, registry, root / "audit.json", commit=True)
    assert audit.read_bytes() == b"preexisting-unrelated-evidence"
    assert files(root) == before


@pytest.mark.parametrize("part", ["receipt", "authorization", "signature"])
def test_capsule_tampering_and_unknown_signer_refuse(tmp_path, part):
    root, capsule, registry, audit = prepared(tmp_path)
    doc = json.loads(capsule.canonical_json())
    if part == "receipt":
        doc["receipt"]["raw_days"] = 0
    elif part == "authorization":
        doc["authorization"] = "AAAA"
    else:
        doc["seal"]["signature"] = "AAAA"
    with pytest.raises(RetentionError):
        apply_retention(
            root,
            json.dumps(doc).encode(),
            audit,
            registry,
            protection_key=PROTECT,
            now=DUE,
            commit=True,
        )
    with pytest.raises(RetentionError, match="retention_signature_invalid"):
        apply(root, capsule, registry[:1], audit, commit=True)
    assert not audit.exists()


def test_protection_key_and_source_root_replacement_refuse(tmp_path):
    root, capsule, registry, audit = prepared(tmp_path)
    with pytest.raises(RetentionError):
        apply_retention(
            root,
            capsule.canonical_json(),
            audit,
            registry,
            protection_key="wrong-synthetic",
            now=DUE,
            commit=True,
        )
    moved = tmp_path / "moved-original"
    root.rename(moved)
    root.mkdir()
    with pytest.raises(RetentionError, match="source_replaced"):
        apply(root, capsule, registry, audit, commit=True)
    assert not audit.exists()


def test_raced_claim_destination_is_never_overwritten(tmp_path, monkeypatch):
    root, capsule, registry, audit = prepared(tmp_path)
    original = retention_apply.rename_claim

    def race(parent_fd, source, destination):
        fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd
        )
        os.write(fd, b"SYNTHETIC-EXISTING-CLAIM")
        os.close(fd)
        return original(parent_fd, source, destination)

    monkeypatch.setattr(retention_apply, "rename_claim", race)
    with pytest.raises(FileExistsError):
        apply(root, capsule, registry, audit, commit=True)
    assert b"SYNTHETIC-EXISTING-CLAIM" in files(root).values()
    assert (root / "report.json.enc").exists()
    assert audit.read_bytes() == capsule.canonical_json()


def test_audit_inside_another_sealed_set_is_not_an_authorized_output(tmp_path):
    root, capsule, registry, _ = prepared(tmp_path)
    other = tmp_path / "other-owned-set"
    with sealed_set.writing(other, "run", "synthetic-other-set-key") as writer:
        writer.write("report.json", b"synthetic-other-set-payload")
    before = files(tmp_path)
    with pytest.raises(RetentionError, match="audit_inside_sealed_set"):
        apply(root, capsule, registry, other / "capsule.json", commit=True)
    assert files(tmp_path) == before
    sealed_set.SealedSet.open(other, "synthetic-other-set-key").verify_all()


def test_capsule_directory_fsync_failure_preserves_raw_and_resume_refsyncs(
    tmp_path, monkeypatch
):
    import stat

    root, capsule, registry, audit = prepared(tmp_path)
    before = files(root)
    original = os.fsync

    def stop(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RuntimeError("synthetic-capsule-directory-fsync")
        return original(fd)

    monkeypatch.setattr(retention_apply.os, "fsync", stop)
    with pytest.raises(RuntimeError, match="capsule-directory-fsync"):
        apply(root, capsule, registry, audit, commit=True)
    assert files(root) == before
    assert audit.read_bytes() == capsule.canonical_json()
    monkeypatch.undo()
    assert apply(root, capsule, registry, audit, commit=True)["completed"]


def test_root_replaced_after_capsule_publish_refuses_before_descriptor_deletion(
    tmp_path, monkeypatch
):
    root, capsule, registry, audit = prepared(tmp_path)
    before = files(root)
    original = retention_apply._persist
    moved = tmp_path / "moved-after-capsule"

    def race(*args, **kwargs):
        original(*args, **kwargs)
        root.rename(moved)
        root.mkdir()

    monkeypatch.setattr(retention_apply, "_persist", race)
    with pytest.raises(RetentionError, match="source_replaced"):
        apply(root, capsule, registry, audit, commit=True)
    assert files(moved) == before
    assert files(root) == {}
    assert audit.read_bytes() == capsule.canonical_json()
