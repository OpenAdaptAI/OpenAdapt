"""Fresh own-root modes and real SQLite WAL reads; no Docker or model."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat

import pytest
from openadapt_flow.integrations.agents import demo_fixture
from openadapt_flow.runtime.effects.db_snapshot import SnapshotError, take_snapshot


def _spec():
    spec, _cases = demo_fixture.load_pack(demo_fixture.packaged_pack("kanboard"))
    return spec


@pytest.mark.parametrize("enabled,mode", [(False, 0o755), (True, 0o1777)])
def test_sidecar_policy_only_changes_fresh_child_under_private_root(
    tmp_path, enabled, mode
):
    spec = _spec()
    spec["host_sidecar_access"] = enabled
    root = tmp_path / "owned"
    files = demo_fixture.prepare(
        spec, root, project="oawrapdemo-coordination", port=19318
    )
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert root.stat().st_uid == os.geteuid()
    assert stat.S_IMODE((root / "data").stat().st_mode) == mode
    assert not files.database.exists()
    assert stat.S_IMODE(tmp_path.stat().st_mode) != 0o1777


@pytest.mark.parametrize("value", [1, "true", None])
def test_nonboolean_policy_refuses_before_files(tmp_path, value):
    spec = _spec()
    spec["host_sidecar_access"] = value
    root = tmp_path / "unused"
    with pytest.raises(ValueError, match="INVALID_DEMO_COORDINATION_POLICY"):
        demo_fixture.prepare(spec, root, project="oawrapdemo-coordination", port=19318)
    assert not root.exists()


@pytest.mark.parametrize("shape", ["existing", "root-alias", "ancestor-alias"])
def test_existing_or_aliased_state_refuses_without_changing_target(tmp_path, shape):
    spec = _spec()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (target / "sentinel").write_text("preserve")
    if shape == "existing":
        root = target
    elif shape == "root-alias":
        root = tmp_path / "alias"
        root.symlink_to(target, target_is_directory=True)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        root = alias / "new-state"
    with pytest.raises(ValueError, match="DEMO_STATE_MUST_BE_FRESH"):
        demo_fixture.prepare(spec, root, project="oawrapdemo-coordination", port=19318)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert (target / "sentinel").read_text() == "preserve"
    assert not (target / "new-state").exists()


@pytest.mark.parametrize("failure", ["mode", "owner"])
def test_descriptor_policy_refuses_unsafe_root_before_child_chmod(
    tmp_path, monkeypatch, failure
):
    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    child = root / "data"
    child.mkdir(mode=0o755)
    if failure == "mode":
        root.chmod(0o755)
    else:
        actual = os.geteuid()
        monkeypatch.setattr(demo_fixture.os, "geteuid", lambda: actual + 1)
    with pytest.raises(ValueError, match="DEMO_COORDINATION_ROOT_UNSAFE"):
        demo_fixture._native_coordination_access(root)
    assert stat.S_IMODE(child.stat().st_mode) == 0o755


@pytest.mark.parametrize("coordination_available", [False, True])
def test_real_wal_read_keeps_native_readonly_transaction_and_database_bytes(
    tmp_path, coordination_available
):
    spec = _spec()
    spec["host_sidecar_access"] = coordination_available
    files = demo_fixture.prepare(
        spec, tmp_path / "owned", project="oawrapdemo-wal", port=19318
    )
    with sqlite3.connect(files.database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE native(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO native VALUES(1, 'synthetic')")
    connection.close()
    assert not files.database.with_name(files.database.name + "-wal").exists()
    assert not files.database.with_name(files.database.name + "-shm").exists()
    files.database.chmod(0o444)
    before = hashlib.sha256(files.database.read_bytes()).hexdigest()
    # Controlled negative models a container-owned directory the reader cannot
    # create sidecars in; it is not an actual container-owner qualification.
    if not coordination_available:
        files.database.parent.chmod(0o555)

    def connect():
        connection = sqlite3.connect(
            files.database.as_uri() + "?mode=ro", uri=True, timeout=2
        )
        connection.execute("PRAGMA query_only=ON")
        return connection

    try:
        if coordination_available:
            snapshot = take_snapshot(
                connect, "sqlite", key=b"unit-only-synthetic-frame-key"
            )
            assert "native" in snapshot.tables
        else:
            with pytest.raises(SnapshotError) as failure:
                take_snapshot(connect, "sqlite", key=b"unit-only-synthetic-frame-key")
            assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
            assert failure.value.__cause__.sqlite_errorcode == 1544
        assert hashlib.sha256(files.database.read_bytes()).hexdigest() == before
        assert not os.access(files.database, os.W_OK)
    finally:
        # Test-temp cleanup only; the real retained app DB is never touched.
        files.database.parent.chmod(0o755)
