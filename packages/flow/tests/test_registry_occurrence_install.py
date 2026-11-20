"""Publisher acceptance and installation share one serialized occurrence."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from openadapt_flow import registry, registry_attest
from openadapt_flow.registry_snapshot import verify_snapshot
from tests.test_registry_snapshot import _index


@pytest.fixture(autouse=True)
def own_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))


def snapshot(tmp_path, monkeypatch):
    path, data = _index(tmp_path)
    registry.stamp_index(data, now=datetime.now(timezone.utc))
    data["serial"] = 50
    raw = json.dumps(data).encode()
    # Explicitly injected attestor: synthetic source/race tests, not publication.
    monkeypatch.setattr(
        registry_attest,
        "verify",
        lambda *_a, **_k: (
            "synthetic-publisher",
            {"index.json": hashlib.sha256(raw).hexdigest()},
        ),
    )
    return verify_snapshot(raw, b'{"synthetic":true}', index_url=str(path))


def publisher_lock_is_held():
    path = registry._state_path().with_suffix(".lock")
    descriptor = os.open(path, os.O_RDWR)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            return True
        return False
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@pytest.mark.parametrize("phase", ["mark", "publish"])
def test_newer_acceptance_cannot_overtake_a_paused_install(
    tmp_path, monkeypatch, phase
):
    from openadapt_flow import skills

    current = snapshot(tmp_path, monkeypatch)
    dest = tmp_path / "installed"
    entered, release, attempted = Event(), Event(), Event()
    product = skills.mark_pulled if phase == "mark" else registry._publish_staged_bundle

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test barrier was not released"
        return product(*args, **kwargs)

    monkeypatch.setattr(
        skills if phase == "mark" else registry,
        "mark_pulled" if phase == "mark" else "_publish_staged_bundle",
        pause,
    )

    def accept_newer():
        attempted.set()
        registry.record_serial(current.serial_key, 90)
        assert dest.is_dir(), "newer accepted before the older installation committed"
        assert json.loads((dest / "skill.lock.json").read_text())["index_serial"] == 50

    with ThreadPoolExecutor(max_workers=2) as pool:
        install = pool.submit(
            registry._pull_snapshot, current, "synthetic/observe", dest
        )
        try:
            assert entered.wait(5)
            # Actual kernel nonblocking-lock refusal, not a scheduling sleep.
            assert publisher_lock_is_held(), (
                "publisher acceptance lock ended before installation"
            )
            newer = pool.submit(accept_newer)
            assert attempted.wait(5)
        finally:
            release.set()
        assert install.result(timeout=5) == dest
        newer.result(timeout=5)
    assert registry._read_state()["serials"][current.serial_key] == 90


@pytest.mark.parametrize(
    "state",
    [
        '{"serials":{"publisher":9},"serials":{"publisher":1}}',
        '{"serials":{"publisher":9,"publisher":1}}',
        '{"serials":{"publisher":9},"metadata":{"a":1,"a":2}}',
        '{"serials":{"publisher":9},"metadata":NaN}',
        '{"serials":{"publisher":9},"metadata":Infinity}',
        '{"serials":{"publisher":9},"metadata":1e9999}',
    ],
)
@pytest.mark.parametrize("consumer", ["read", "check", "record"])
def test_ambiguous_ledger_never_resets_any_consumer(tmp_path, state, consumer):
    path = registry._state_path()
    path.parent.mkdir()
    original = state.encode()
    path.write_bytes(original)
    with pytest.raises(registry.RegistryError, match="not reset"):
        if consumer == "read":
            registry._read_state()
        elif consumer == "check":
            registry.serial_problem("publisher", 2)
        else:
            registry.record_serial("publisher", 2)
    assert path.read_bytes() == original


def test_newer_mark_during_download_refuses_before_replacing_destination(
    tmp_path, monkeypatch
):
    current = snapshot(tmp_path, monkeypatch)
    dest = tmp_path / "installed"
    dest.mkdir()
    (dest / "retained").write_bytes(b"old bundle")
    read = registry._read
    advanced = False

    def advance(*args, **kwargs):
        nonlocal advanced
        if not advanced:
            registry.record_serial(current.serial_key, 90)
            advanced = True
        return read(*args, **kwargs)

    monkeypatch.setattr(registry, "_read", advance)
    with pytest.raises(registry.RegistryError, match="older"):
        registry._pull_snapshot(current, "synthetic/observe", dest, overwrite=True)
    assert advanced and (dest / "retained").read_bytes() == b"old bundle"
    assert not (dest / "workflow.json").exists()


def test_expiry_during_staging_never_replaces_old_destination(tmp_path, monkeypatch):
    from datetime import timedelta

    from openadapt_flow import skills

    current = snapshot(tmp_path, monkeypatch)
    dest = tmp_path / "installed"
    dest.mkdir()
    (dest / "retained").write_bytes(b"old bundle")
    expired = False
    mark = skills.mark_pulled
    freshness = registry.index_freshness_problem

    def advance(*args, **kwargs):
        nonlocal expired
        mark(*args, **kwargs)
        expired = True

    def clock(data, **kwargs):
        when = datetime.now(timezone.utc) + (
            timedelta(days=31) if expired else timedelta()
        )
        return freshness(data, now=when)

    monkeypatch.setattr(skills, "mark_pulled", advance)
    monkeypatch.setattr(registry, "index_freshness_problem", clock)
    with pytest.raises(registry.RegistryError, match="expired"):
        registry._pull_snapshot(current, "synthetic/observe", dest, overwrite=True)
    assert expired and (dest / "retained").read_bytes() == b"old bundle"
    assert not (dest / "workflow.json").exists()


def test_failed_publication_restores_existing_bundle(tmp_path, monkeypatch):
    current = snapshot(tmp_path, monkeypatch)
    dest = tmp_path / "installed"
    dest.mkdir()
    (dest / "retained").write_bytes(b"old bundle")
    replace = Path.replace

    def fail_commit(path, target):
        if path.name == "bundle" and Path(target) == dest:
            raise OSError("synthetic publication failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_commit)
    with pytest.raises(OSError, match="publication failure"):
        registry._pull_snapshot(current, "synthetic/observe", dest, overwrite=True)
    assert (dest / "retained").read_bytes() == b"old bundle"
    assert not (dest / "workflow.json").exists()


def test_new_destination_is_never_clobbered_without_overwrite(tmp_path, monkeypatch):
    from openadapt_flow import skills

    current = snapshot(tmp_path, monkeypatch)
    dest = tmp_path / "installed"
    mark = skills.mark_pulled

    def another_writer(*args, **kwargs):
        mark(*args, **kwargs)
        dest.mkdir()
        (dest / "retained").write_bytes(b"another writer")

    monkeypatch.setattr(skills, "mark_pulled", another_writer)
    with pytest.raises(registry.RegistryError, match="exists"):
        registry._pull_snapshot(current, "synthetic/observe", dest)
    assert (dest / "retained").read_bytes() == b"another writer"
    assert not (dest / "workflow.json").exists()


@pytest.mark.parametrize("value", [True, -1, 1.5])
def test_invalid_marks_never_poison_the_ledger(value):
    with pytest.raises(registry.RegistryError, match="invalid"):
        registry.record_serial("publisher", value)
    assert not registry._state_path().exists()
