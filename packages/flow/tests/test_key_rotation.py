"""Fresh keys and real temporary files; no deployment-key provider is used."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace

import pytest

from openadapt_flow import crypto, key_rotation, sealed_maintenance, sealed_set
from openadapt_flow.key_rotation import RotationError, key_coverage, plan_rewrap, rewrap
from openadapt_flow.sealed_maintenance import MaintenanceError, mutation_lock

OLD = "synthetic-old-wrap-1900"
NEW = "synthetic-new-wrap-1900"
THIRD = "synthetic-third-wrap-1900"


def fixture(tmp_path, kind="recording", name="owned-set"):
    root = tmp_path / name
    with sealed_set.writing(root, kind, OLD) as writer:
        writer.write(
            "events.jsonl" if kind == "recording" else "report.json",
            b"synthetic-private-payload-1900",
        )
        if kind == "recording":
            writer.write("frames/000.png", b"synthetic-private-pixels-1900")
    return root


def payloads(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != sealed_set.HEADER
    }


def test_legacy_rotation_preserves_every_payload_index_and_set_digest(tmp_path):
    root = fixture(tmp_path)
    old_header = sealed_set.read_header(root)
    old_digest = sealed_set.SealedSet.open(root, OLD).set_digest
    before = payloads(root)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="synthetic-kek-2")
    header_bytes = (root / sealed_set.HEADER).read_bytes()
    assert rewrap(plan, old_key=OLD, new_key=NEW)["dry_run"]
    assert (root / sealed_set.HEADER).read_bytes() == header_bytes
    assert payloads(root) == before
    result = rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    assert result["rewrapped"] and not result["dry_run"]
    header = sealed_set.read_header(root)
    assert header["version"] == 2
    assert header["index"] == old_header["index"]
    assert sealed_set.SealedSet.open(root, NEW).set_digest == old_digest
    assert payloads(root) == before
    with pytest.raises(crypto.CryptoError):
        sealed_set.SealedSet.open(root, OLD)
    coverage = key_coverage([root], keys={"synthetic-kek-2": NEW})
    assert coverage["complete"]
    assert coverage["key_retirement_authorized"] is False


def test_second_rewrap_and_resumed_writer_keep_payload_key(tmp_path):
    root = fixture(tmp_path)
    first = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(first, old_key=OLD, new_key=NEW, commit=True)
    before = payloads(root)
    binding = sealed_set.read_header(root)["data_key_id"]
    second = plan_rewrap(root, old_key=NEW, new_key=THIRD, key_id="k3")
    rewrap(second, old_key=NEW, new_key=THIRD, commit=True)
    assert sealed_set.read_header(root)["data_key_id"] == binding
    assert payloads(root) == before
    with sealed_set.writing(root, "recording", THIRD, resume=True) as writer:
        writer.write("frames/001.png", b"fresh-synthetic-pixels")
    assert (
        sealed_set.SealedSet.open(root, THIRD).read_bytes("frames/001.png")
        == b"fresh-synthetic-pixels"
    )
    assert key_coverage([root], keys={"k3": THIRD})["complete"]


def test_old_cached_passphrase_and_stale_writer_cannot_restore_old_header(tmp_path):
    root = fixture(tmp_path)
    cached = sealed_set.open_set(root, OLD)
    writer = sealed_set.SealedSetWriter(root, "recording", OLD, resume=True)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    assert (
        cached.read_bytes("events.jsonl") == b"synthetic-private-payload-1900"
    )  # no in-memory zeroization claim
    with pytest.raises(crypto.CryptoError):
        sealed_set.open_set(root, OLD)
    header = (root / sealed_set.HEADER).read_bytes()
    for mutation in (
        lambda: writer.write("events.jsonl", b"stale"),
        writer.finalize,
        lambda: writer.unlink("events.jsonl"),
    ):
        with pytest.raises(sealed_set.SealedSetError, match="header changed"):
            mutation()
    assert (root / sealed_set.HEADER).read_bytes() == header
    sealed_set.SealedSet.open(root, NEW).verify_all()


def test_another_process_cache_observes_whole_header_revision(tmp_path):
    root = fixture(tmp_path)
    sealed_set.open_set(root, OLD)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    raw = plan.replacement
    # Simulate publication in another process without this process's _forget.
    (root / sealed_set.HEADER).write_bytes(raw)
    with pytest.raises(crypto.CryptoError):
        sealed_set.open_set(root, OLD)
    sealed_set.open_set(root, NEW).verify_all()


@pytest.mark.parametrize(
    "field,value",
    [
        ("key_id", "different-key"),
        ("salt", "AAAAAAAAAAAAAAAAAAAAAA=="),
        ("kdf", "scrypt"),
        ("kdf_n", 32768),
        ("kdf_r", 8),
        ("kdf_p", 1),
        ("data_key_id", "0" * 32),
        ("data_key_id", "invalid"),
        ("data_key_id", None),
        ("kind", "run"),
        ("wrapped_key", "AAAA"),
    ],
)
def test_authenticated_wrapping_metadata_tampering_refuses(tmp_path, field, value):
    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    header = sealed_set.read_header(root)
    header[field] = value
    (root / sealed_set.HEADER).write_text(json.dumps(header))
    with pytest.raises((ValueError, crypto.CryptoError)):
        sealed_set.SealedSet.open(root, NEW)
    assert not key_coverage([root], keys={"k2": NEW, "different-key": NEW})["complete"]


def test_plan_change_source_change_and_active_writer_refuse(tmp_path):
    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    with pytest.raises(RotationError, match="replacement_changed"):
        rewrap(replace(plan, replacement=b"{}"), old_key=OLD, new_key=NEW, commit=True)
    writer = sealed_set.SealedSetWriter(root, "recording", OLD, resume=True)
    sealed_set.register_writer(writer)
    try:
        with pytest.raises(RotationError, match="active_writer"):
            rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    finally:
        sealed_set.unregister_writer(writer)
    with sealed_set.writing(root, "recording", OLD, resume=True) as writer:
        writer.write("events.jsonl", b"synthetic-revision")
    with pytest.raises(RotationError, match="stale_rewrap_plan"):
        rewrap(plan, old_key=OLD, new_key=NEW, commit=True)


def test_fault_before_replace_keeps_old_header_decryptable(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    before = payloads(root)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-before-replace")

    monkeypatch.setattr(sealed_maintenance.os, "replace", fail)
    with pytest.raises(RuntimeError, match="synthetic-before-replace"):
        rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    sealed_set.SealedSet.open(root, OLD).verify_all()
    assert payloads(root) == before


def test_fault_after_replace_never_reports_success_and_new_header_decrypts(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    before = payloads(root)
    original = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RuntimeError("synthetic-after-replace")
        return original(fd)

    monkeypatch.setattr(sealed_maintenance.os, "fsync", fail_directory)
    with pytest.raises(RuntimeError, match="synthetic-after-replace"):
        rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    sealed_set.SealedSet.open(root, NEW).verify_all()
    assert payloads(root) == before
    assert key_coverage([root], keys={"k2": NEW})["complete"]


def test_real_process_exit_immediately_after_atomic_replace(tmp_path):
    root = fixture(tmp_path)
    before = payloads(root)
    code = """import os,sys
from pathlib import Path
from openadapt_flow import sealed_maintenance as m
from openadapt_flow.key_rotation import plan_rewrap,rewrap
root=Path(sys.argv[1]); plan=plan_rewrap(root,old_key="synthetic-old-wrap-1900",new_key="synthetic-new-wrap-1900",key_id="k2")
original=os.replace
def stop(*a,**kw):
 original(*a,**kw)
 os._exit(17)
m.os.replace=stop
rewrap(plan,old_key="synthetic-old-wrap-1900",new_key="synthetic-new-wrap-1900",commit=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root)], capture_output=True, timeout=30
    )
    assert result.returncode == 17, result.stderr.decode()
    sealed_set.SealedSet.open(root, NEW).verify_all()
    assert payloads(root) == before
    # Process exit is not a power-loss durability experiment.
    assert key_coverage([root], keys={"k2": NEW})["complete"]


def test_actual_process_lock_is_exclusive_and_kernel_released_on_exit(tmp_path):
    root = fixture(tmp_path)
    code = """import sys
from pathlib import Path
from openadapt_flow.sealed_maintenance import mutation_lock
with mutation_lock(Path(sys.argv[1])):
 print("held",flush=True)
 sys.stdin.read()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(MaintenanceError, match="mutation_lock_busy"):
            with mutation_lock(root, wait_s=0.05):
                pytest.fail("overlapping ownership")
    finally:
        child.terminate()
        child.communicate(timeout=10)
    with mutation_lock(root, wait_s=0.2):
        pass


def test_mixed_batch_coverage_cannot_authorize_key_retirement(tmp_path):
    first = fixture(tmp_path, name="first")
    second = fixture(tmp_path, name="second")
    plan = plan_rewrap(first, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    result = key_coverage([first, second], keys={"k2": NEW})
    assert not result["complete"]
    assert [entry["covered"] for entry in result["sets"]] == [True, False]
    assert result["sets"][1]["reason"] == "legacy_unlabelled"
    assert not key_coverage([first], keys={})["complete"]
    assert not key_coverage([first], keys={"k2": "wrong-synthetic"})["complete"]
    assert key_coverage([], keys={})["complete"] is False
    with pytest.raises(RotationError, match="duplicate_coverage_root"):
        key_coverage([first, first], keys={"k2": NEW})


@pytest.mark.parametrize(
    "old,new,key_id",
    [
        ("", NEW, "k2"),
        (OLD, "", "k2"),
        (OLD, OLD, "k2"),
        (OLD, NEW, ""),
        (OLD, NEW, "free text key"),
    ],
)
def test_explicit_valid_distinct_keys_and_opaque_id_required(
    tmp_path, old, new, key_id
):
    root = fixture(tmp_path)
    before = (root / sealed_set.HEADER).read_bytes()
    with pytest.raises(RotationError):
        plan_rewrap(root, old_key=old, new_key=new, key_id=key_id)
    assert (root / sealed_set.HEADER).read_bytes() == before


def test_crafted_plan_cannot_publish_an_unreadable_format_header(tmp_path):
    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    header = json.loads(plan.replacement)
    header["format"] = "not-a-readable-sealed-set"
    # A caller knowing both wrapping keys may construct a valid new envelope,
    # but may not change immutable format fields through the rotation API.
    data_key = sealed_set._data_key(sealed_set.read_header(root), OLD)
    header["wrapped_key"] = base64.b64encode(
        crypto.encrypt_bytes(data_key, NEW, aad=sealed_set._wrapping_aad(header))
    ).decode()
    raw = json.dumps(header, sort_keys=True).encode()
    changed = replace(
        plan, replacement=raw, replacement_header_sha256=hashlib.sha256(raw).hexdigest()
    )
    old = (root / sealed_set.HEADER).read_bytes()
    with pytest.raises(RotationError, match="immutable_header_changed"):
        rewrap(changed, old_key=OLD, new_key=NEW, commit=True)
    assert (root / sealed_set.HEADER).read_bytes() == old
    sealed_set.SealedSet.open(root, OLD).verify_all()


def test_path_alias_cannot_acquire_a_second_process_lock(tmp_path):
    root = fixture(tmp_path)
    alias = tmp_path / "owned-set-alias"
    alias.symlink_to(root, target_is_directory=True)
    code = """import sys
from pathlib import Path
from openadapt_flow.sealed_maintenance import MaintenanceError,mutation_lock
try:
 with mutation_lock(Path(sys.argv[1]),wait_s=0.05):
  sys.exit(8)
except MaintenanceError as exc:
 print(str(exc))
"""
    with mutation_lock(root):
        result = subprocess.run(
            [sys.executable, "-c", code, str(alias)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "mutation_lock_busy"


def test_system_default_temp_alias_is_canonicalized_without_weakening_explicit_paths(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    real = tmp_path / "system-temp"
    real.mkdir()
    alias = tmp_path / "system-temp-alias"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.delenv("OPENADAPT_SEALED_LOCK_DIR")
    monkeypatch.setattr(sealed_maintenance.tempfile, "gettempdir", lambda: str(alias))
    with mutation_lock(root):
        pass
    assert list(real.glob("openadapt-sealed-mutation-*/*.lock"))
    monkeypatch.setenv("OPENADAPT_SEALED_LOCK_DIR", str(alias / "explicit"))
    with pytest.raises(MaintenanceError, match="unsafe_mutation_lock_directory"):
        with mutation_lock(root):
            pytest.fail("explicit symlink lock directory accepted")


def test_different_named_lock_namespace_still_contends_on_root_inode(tmp_path):
    root = fixture(tmp_path)
    code = """import sys
from pathlib import Path
from openadapt_flow.sealed_maintenance import MaintenanceError,mutation_lock
try:
 with mutation_lock(Path(sys.argv[1]),wait_s=0.05):
  sys.exit(8)
except MaintenanceError as exc:
 print(str(exc))
"""
    child_env = dict(os.environ)
    child_env["OPENADAPT_SEALED_LOCK_DIR"] = str(tmp_path / "different-child-locks")
    with mutation_lock(root):
        result = subprocess.run(
            [sys.executable, "-c", code, str(root)],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "mutation_lock_busy"


@pytest.mark.parametrize("nested", [False, True])
def test_hard_exit_before_replace_keeps_old_set_and_sealed_ancestors_readable(
    tmp_path, nested
):
    parent = tmp_path
    if nested:
        parent = tmp_path / "outer-owned-set"
        with sealed_set.writing(parent, "run", OLD) as writer:
            writer.write("report.json", b"synthetic-outer-payload")
    root = fixture(parent)
    before = payloads(root)
    header = (root / sealed_set.HEADER).read_bytes()
    code = """import os,sys
from pathlib import Path
from openadapt_flow import sealed_maintenance as m
from openadapt_flow.key_rotation import plan_rewrap,rewrap
root=Path(sys.argv[1]); plan=plan_rewrap(root,old_key="synthetic-old-wrap-1900",new_key="synthetic-new-wrap-1900",key_id="k2")
def stop(*a,**kw):
 os._exit(18)
m.os.replace=stop
rewrap(plan,old_key="synthetic-old-wrap-1900",new_key="synthetic-new-wrap-1900",commit=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root)], capture_output=True, timeout=30
    )
    assert result.returncode == 18, result.stderr.decode()
    assert (root / sealed_set.HEADER).read_bytes() == header
    sealed_set.SealedSet.open(root, OLD).verify_all()
    assert payloads(root) == before
    if nested:
        sealed_set.SealedSet.open(parent, OLD).verify_all()


def test_cross_device_publication_error_refuses_without_copy_fallback(
    tmp_path, monkeypatch
):
    import errno

    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    before = payloads(root)
    header = (root / sealed_set.HEADER).read_bytes()

    def fail(*args, **kwargs):
        raise OSError(errno.EXDEV, "synthetic-cross-device")

    monkeypatch.setattr(sealed_maintenance.os, "replace", fail)
    with pytest.raises(OSError) as caught:
        rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    assert caught.value.errno == errno.EXDEV
    assert (root / sealed_set.HEADER).read_bytes() == header
    assert payloads(root) == before
    sealed_set.SealedSet.open(root, OLD).verify_all()


def test_root_inode_replacement_before_publish_never_writes_replacement(
    tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    original = key_rotation.durable_replace
    moved = tmp_path / "moved-original"

    def race(path, data, **kwargs):
        root.rename(moved)
        root.mkdir()
        return original(path, data, **kwargs)

    monkeypatch.setattr(key_rotation, "durable_replace", race)
    with pytest.raises(MaintenanceError, match="source_directory_replaced"):
        rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    assert not (root / sealed_set.HEADER).exists()
    sealed_set.SealedSet.open(moved, OLD).verify_all()


def test_ancestor_symlink_change_before_publish_refuses_without_mutating_original(
    tmp_path, monkeypatch
):
    parent = tmp_path / "source-parent"
    parent.mkdir()
    root = fixture(parent)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    original = key_rotation.durable_replace
    moved = tmp_path / "moved-parent"

    def race(path, data, **kwargs):
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        return original(path, data, **kwargs)

    monkeypatch.setattr(key_rotation, "durable_replace", race)
    with pytest.raises(MaintenanceError, match="source_directory_ancestry_changed"):
        rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    sealed_set.SealedSet.open(moved / root.name, OLD).verify_all()


def test_current_header_cannot_derive_payload_key_with_previous_passphrase(tmp_path):
    root = fixture(tmp_path)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    current = sealed_set.read_header(root)
    recovered = False
    try:
        legacy_key = crypto.derive_data_key(
            OLD,
            base64.b64decode(current["salt"], validate=True),
            n=current["kdf_n"],
            r=current["kdf_r"],
            p=current["kdf_p"],
        )
        plaintext_index = crypto.SetKey(legacy_key).open(
            base64.b64decode(current["index"], validate=True),
            aad=sealed_set._index_aad(current["kind"]),
        )
        recovered = json.loads(plaintext_index)["kind"] == "recording"
    except (KeyError, ValueError, crypto.CryptoError):
        pass
    print(f"old_passphrase_current_header_manual_kdf_recovered={recovered}")
    assert not recovered, "current header still allows old-passphrase DEK derivation"
    assert not {"salt", "kdf", "kdf_n", "kdf_r", "kdf_p"}.intersection(current)
    sealed_set.SealedSet.open(root, NEW).verify_all()
    with pytest.raises(crypto.CryptoError):
        sealed_set.SealedSet.open(root, OLD)


def test_retained_legacy_header_still_allows_historical_key_access(tmp_path):
    root = fixture(tmp_path)
    historical = sealed_set.read_header(root)
    assert historical["version"] == 1
    legacy_key = sealed_set._data_key(historical, OLD)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    current = sealed_set.read_header(root)
    assert current["index"] == historical["index"]
    # Rewrapping cannot revoke a saved v1 header, recovered DEK or ciphertext.
    index = crypto.SetKey(legacy_key).open(
        base64.b64decode(current["index"], validate=True),
        aad=sealed_set._index_aad(current["kind"]),
    )
    assert json.loads(index)["kind"] == "recording"
    sealed_set.SealedSet.open(root, NEW).verify_all()


def test_envelopes_are_bound_to_distinct_sets_and_cannot_be_exchanged(tmp_path):
    first = fixture(tmp_path, name="first")
    second = fixture(tmp_path, name="second")
    for root in (first, second):
        plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
        rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    left = sealed_set.read_header(first)
    right = sealed_set.read_header(second)
    assert left["data_key_id"] != right["data_key_id"]
    left["wrapped_key"] = right["wrapped_key"]
    (first / sealed_set.HEADER).write_text(json.dumps(left))
    with pytest.raises(crypto.CryptoError):
        sealed_set.SealedSet.open(first, NEW)
    sealed_set.SealedSet.open(second, NEW).verify_all()


def test_crafted_subsequent_plan_cannot_change_authenticated_set_identity(tmp_path):
    root = fixture(tmp_path)
    first = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(first, old_key=OLD, new_key=NEW, commit=True)
    plan = plan_rewrap(root, old_key=NEW, new_key=THIRD, key_id="k3")
    header = json.loads(plan.replacement)
    header["data_key_id"] = "0" * 32
    data_key = sealed_set._data_key(sealed_set.read_header(root), NEW)
    header["wrapped_key"] = base64.b64encode(
        crypto.encrypt_bytes(data_key, THIRD, aad=sealed_set._wrapping_aad(header))
    ).decode()
    raw = json.dumps(header, sort_keys=True).encode()
    changed = replace(
        plan, replacement=raw, replacement_header_sha256=hashlib.sha256(raw).hexdigest()
    )
    previous = (root / sealed_set.HEADER).read_bytes()
    with pytest.raises(RotationError, match="immutable_header_changed"):
        rewrap(changed, old_key=NEW, new_key=THIRD, commit=True)
    assert (root / sealed_set.HEADER).read_bytes() == previous
    sealed_set.SealedSet.open(root, NEW).verify_all()


def test_mixed_legacy_wrapping_metadata_refuses_migration_but_v1_remains_readable(
    tmp_path,
):
    root = fixture(tmp_path)
    header = sealed_set.read_header(root)
    header["data_key_id"] = "0" * 32
    (root / sealed_set.HEADER).write_text(json.dumps(header))
    sealed_set.SealedSet.open(root, OLD).verify_all()
    with pytest.raises(RotationError, match="ambiguous_legacy_header"):
        plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")


def test_actual_external_checkpoint_store_cannot_claim_new_key_coverage(tmp_path):
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore, RunCheckpoint

    root = tmp_path / "owned-run-with-external-checkpoints"
    checkpoint = RunCheckpoint(
        run_id="synthetic-checkpoint-run-1900",
        workflow_name="synthetic-checkpoint-workflow-1900",
        step_index=0,
        step_id="synthetic-step-1900",
        next_step_index=1,
        params={"synthetic": "private-checkpoint-value-1900"},
    )
    with sealed_set.writing(root, "run", OLD) as writer:
        writer.write("report.json", b"synthetic-run-report-1900")
        store = CheckpointStore(root, key=OLD)
        path = store.write_checkpoint(checkpoint)
        writer.finalize(adopt_external=True)
    rel = path.relative_to(root).as_posix()
    opened = sealed_set.SealedSet.open(root, OLD)
    assert opened.codec(rel) == "external"
    assert store.checkpoints() == [checkpoint]
    original = payloads(root)
    plan = plan_rewrap(root, old_key=OLD, new_key=NEW, key_id="k2")
    rewrap(plan, old_key=OLD, new_key=NEW, commit=True)
    sealed_set.SealedSet.open(root, NEW).verify_all()
    assert payloads(root) == original
    with pytest.raises(crypto.CryptoError):
        CheckpointStore(root, key=NEW).checkpoints()
    assert CheckpointStore(root, key=OLD).checkpoints() == [checkpoint]
    coverage = key_coverage([root], keys={"k2": NEW})
    assert coverage["complete"] is False
    assert coverage["sets"][0]["covered"] is False
    assert coverage["sets"][0]["reason"] == "external_key_store_unverified"
    assert coverage["key_retirement_authorized"] is False
