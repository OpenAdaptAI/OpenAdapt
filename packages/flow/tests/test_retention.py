"""Fresh encrypted fixtures; retention must earn eligibility without writes."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.seal import parse_key_registry

from openadapt_flow import at_rest, crypto, sealed_set
from openadapt_flow.offline_seal import sign_subject
from openadapt_flow.retention import RetentionError, RetentionPolicy, plan_retention
from openadapt_flow.seal_keys import SoftwareSigner
from tests.test_run_receipt import _report

RUNTIME_ID = "00000000-0000-4000-8000-000000001900"
KEY = "synthetic-retention-1900-only"
T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
ISSUED = T0 + timedelta(minutes=1)
POLICY = RetentionPolicy(raw_days=3, audit_days=365)


def fixture(
    tmp_path,
    *,
    report_over=None,
    raw_over=None,
    seal_id=RUNTIME_ID,
    outcome="VERIFIED",
    issued=ISSUED,
):
    values = {
        "workflow_name": "synthetic-retention-1900",
        "started_at": T0.isoformat(),
        "params": {"note": "SYNTHETIC-RETENTION-PRIVATE-TEXT"},
        "run_id_sha256": hashlib.sha256(RUNTIME_ID.encode()).hexdigest(),
    }
    values.update(report_over or {})
    report = _report(**values)
    doc = report.model_dump(mode="json")
    doc.update(raw_over or {})
    raw = json.dumps(doc, sort_keys=True).encode()
    root = tmp_path / "directory-label-is-not-runtime-identity"
    with sealed_set.writing(root, "run", KEY) as writer:
        writer.write("report.json", raw)
        writer.write("steps/000_before.png", b"synthetic-private-pixels-1900")
        writer.write("snapshots/dom.json", b'{"text":"synthetic-private-dom-1900"}')
    signer = SoftwareSigner(Ed25519PrivateKey.generate(), "generated")
    seal = sign_subject(
        "run-report",
        hashlib.sha256(raw).hexdigest(),
        seal_id,
        outcome=outcome,
        now=issued,
        signer=signer,
    )
    registry = parse_key_registry([signer.registry_entry()])
    return root, json.dumps(seal.wire(), sort_keys=True).encode(), registry


def plan(root, seal, registry, *, now=None):
    return plan_retention(
        root, POLICY, seal, registry, key=KEY, now=now or ISSUED + timedelta(days=3)
    )


def files(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_exact_declared_runtime_hash_binds_without_raw_id_in_report(tmp_path):
    root, seal, registry = fixture(tmp_path)
    result = plan(root, seal, registry)
    assert result.eligible
    assert result.reason == "eligible"
    assert result.receipt.outcome == "VERIFIED"
    assert len(result.targets) == 4
    assert result.targets[-1].relative_path == sealed_set.HEADER
    assert result.preview()["dry_run"] is True
    assert (
        "SYNTHETIC-RETENTION-PRIVATE-TEXT"
        not in result.receipt.canonical_json().decode()
    )


def test_planning_never_signs_creates_keys_or_opens_a_file_for_writing(
    tmp_path, monkeypatch
):
    root, seal, registry = fixture(tmp_path)
    before = files(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only planning attempted a side effect")

    monkeypatch.setattr(at_rest, "default_key", forbidden)
    monkeypatch.setattr(at_rest, "existing_key", forbidden)
    monkeypatch.setattr(SoftwareSigner, "sign", forbidden)
    original = os.open

    def read_only(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            forbidden()
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", read_only)
    assert plan(root, seal, registry).eligible
    assert files(tmp_path) == before


def test_policy_uses_signed_time_and_exact_elapsed_boundary(tmp_path):
    root, seal, registry = fixture(tmp_path)
    due = ISSUED + timedelta(days=3)
    assert not plan(root, seal, registry, now=due - timedelta(microseconds=1)).eligible
    assert plan(root, seal, registry, now=due).eligible
    assert plan(root, seal, registry, now=due).audit_until == "2027-11-01T12:01:00Z"


def test_directory_basename_is_not_assumed_to_be_runtime_identity(tmp_path):
    root, seal, registry = fixture(
        tmp_path, seal_id="directory-label-is-not-runtime-identity"
    )
    with pytest.raises(RetentionError, match="source_runtime_identity_mismatch"):
        plan(root, seal, registry)


@pytest.mark.parametrize("hash_value", [None, "f" * 64, "invalid"])
def test_missing_invalid_or_mismatched_declared_identity_refuses(tmp_path, hash_value):
    root, seal, registry = fixture(tmp_path, raw_over={"run_id_sha256": hash_value})
    with pytest.raises(RetentionError):
        plan(root, seal, registry)


def test_contradictory_raw_id_cannot_override_the_declared_hash(tmp_path):
    root, seal, registry = fixture(tmp_path, raw_over={"run_id": "a-different-runtime"})
    with pytest.raises(RetentionError, match="source_run_id_mismatch"):
        plan(root, seal, registry)


def test_unknown_or_revoked_signing_key_refuses(tmp_path):
    root, seal, registry = fixture(tmp_path)
    with pytest.raises(RetentionError, match="source_seal_unknown_key"):
        plan(root, seal, [])
    with pytest.raises(RetentionError, match="source_seal_revoked_key"):
        plan(
            root,
            seal,
            [replace(registry[0], status="revoked", revoked_at="2026-11-02T00:00:00Z")],
        )


def test_signed_nonverified_outcome_refuses(tmp_path):
    root, seal, registry = fixture(tmp_path, outcome="HALTED")
    with pytest.raises(RetentionError, match="signed_verified_run_required"):
        plan(root, seal, registry)


def test_verified_label_does_not_replace_the_existing_complete_report_contract(
    tmp_path,
):
    root, seal, registry = fixture(tmp_path, raw_over={"production_eligible": False})
    with pytest.raises(RetentionError, match="complete_verified_report_required"):
        plan(root, seal, registry)


def test_signed_future_time_and_naive_clock_refuse(tmp_path):
    root, seal, registry = fixture(tmp_path)
    with pytest.raises(RetentionError, match="signed_time_outside_run"):
        plan(root, seal, registry, now=T0)
    with pytest.raises(RetentionError, match="naive_current_time"):
        plan(root, seal, registry, now=datetime(2026, 11, 5))


def test_explicit_decryption_key_is_required_without_local_fallback(tmp_path):
    root, seal, registry = fixture(tmp_path)
    with pytest.raises(RetentionError, match="explicit_decryption_key_required"):
        plan_retention(
            root, POLICY, seal, registry, key="", now=ISSUED + timedelta(days=3)
        )


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "nested"])
def test_unbound_filesystem_ownership_is_refused(tmp_path, kind):
    root, seal, registry = fixture(tmp_path)
    if kind == "symlink":
        (root / "outside-link").symlink_to(tmp_path / "outside")
    elif kind == "hardlink":
        os.link(root / "steps/000_before.png.enc", tmp_path / "outside-copy")
    else:
        with sealed_set.writing(root / "nested", "run", KEY) as writer:
            writer.write("report.json", b"{}")
    before = files(tmp_path)
    with pytest.raises(RetentionError):
        plan(root, seal, registry)
    assert files(tmp_path) == before


def test_runtime_reconciliation_is_not_a_frozen_retention_inventory(tmp_path):
    root, seal, registry = fixture(tmp_path)
    (root / "later-checkpoint.enc").write_bytes(
        crypto.encrypt_bytes(b"synthetic", KEY, aad=crypto.CHECKPOINT_AAD)
    )
    with pytest.raises(RetentionError, match="run_inventory_not_finalized"):
        plan(root, seal, registry)


@pytest.mark.parametrize("raw,audit", [(True, 10), (-1, 10), (1, False), (2, 1)])
def test_policy_durations_do_not_coerce_or_shorten_audit_retention(raw, audit):
    with pytest.raises(RetentionError):
        RetentionPolicy(raw_days=raw, audit_days=audit)
