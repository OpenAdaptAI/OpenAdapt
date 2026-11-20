"""Synthetic maintenance must not depend on a process-global reader cache."""

from __future__ import annotations

import sys

import pytest

from openadapt_flow import crypto, key_rotation, retention_apply, sealed_set
from openadapt_flow.retention import RetentionError
from tests.test_key_rotation import NEW, OLD
from tests.test_key_rotation import fixture as rotation_fixture
from tests.test_retention import KEY, files
from tests.test_retention_apply import DUE, PROTECT, prepared


@pytest.mark.parametrize("operation", ["rewrap", "retention"])
def test_owned_reader_interoperates_with_maintenance(tmp_path, operation):
    if operation == "rewrap":
        root = rotation_fixture(tmp_path)
        key = OLD
        logical = "events.jsonl"
        plan = key_rotation.plan_rewrap(
            root, old_key=OLD, new_key=NEW, key_id="synthetic-followup-key"
        )

        def apply(*, commit=False, wrong_key=False):
            return key_rotation.rewrap(
                plan,
                old_key=OLD,
                new_key="synthetic-wrong-followup-key" if wrong_key else NEW,
                commit=commit,
            )

        refusal = crypto.CryptoError
    else:
        root, capsule, registry, audit = prepared(tmp_path)
        key = KEY
        logical = "report.json"

        def apply(*, commit=False, wrong_key=False):
            return retention_apply.apply_retention(
                root,
                capsule.canonical_json(),
                audit,
                registry,
                protection_key=(
                    "synthetic-wrong-followup-key" if wrong_key else PROTECT
                ),
                now=DUE,
                commit=commit,
            )

        refusal = RetentionError

    # One explicit caller owns the earlier handle. Separate operations must
    # authenticate current disk state without module-global invalidation.
    reader = sealed_set.open_set(root, key)
    plaintext = reader.read_bytes(logical)
    before = files(root)
    assert apply()["dry_run"]
    assert files(root) == before
    with pytest.raises(refusal):
        apply(commit=True, wrong_key=True)
    assert files(root) == before
    assert sealed_set.open_set(root, key).read_bytes(logical) == plaintext

    if operation == "retention" and sys.platform != "linux":
        # Preserve the real backend refusal; do not emulate Linux publication.
        with pytest.raises(RetentionError, match="descriptor_retention_unsupported"):
            apply(commit=True)
        assert files(root) == before
        assert not audit.exists()
        return

    result = apply(commit=True)
    assert not result["dry_run"]
    if operation == "rewrap":
        assert result["rewrapped"]
        assert reader.read_bytes(logical) == plaintext
        fresh = sealed_set.open_set(root, NEW)
        assert fresh is not reader
        assert fresh.read_bytes(logical) == plaintext
        fresh.verify_all()
        assert {k: v for k, v in files(root).items() if k != sealed_set.HEADER} == {
            k: v for k, v in before.items() if k != sealed_set.HEADER
        }
        with pytest.raises(crypto.CryptoError):
            sealed_set.open_set(root, OLD)
    else:
        assert result["completed"] and result["remaining_files"] == 0
        assert files(root) == {sealed_set.HEADER: before[sealed_set.HEADER]}
        assert audit.read_bytes() == capsule.canonical_json()
        with pytest.raises(sealed_set.SealedSetError):
            sealed_set.open_set(root, key)
        assert apply(commit=True)["removed_files"] == 0
