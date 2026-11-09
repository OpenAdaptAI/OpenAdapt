"""The external durable authority database is created owner-only.

Two durable runs that start at the same moment (two workers, or two
processes) both open the authority database. SQLite creates a missing file
with the process umask; if the first run's later chmod hadn't happened yet,
the second run saw a world-readable file and refused. The file now exists,
owner-only, before SQLite opens it.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading

import pytest

from openadapt_flow.runtime.durable import authority as authority_module
from tests.test_durable_authority_v13 import _fresh_run


def _store(path):
    """Start one fresh durable run (its own run id and namespace)."""

    import uuid

    return _fresh_run(path, run_id=str(uuid.uuid4()), namespace_id=uuid.uuid4().hex)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_fresh_database_is_owner_only_before_sqlite_opens_it(tmp_path, monkeypatch):
    db_path = tmp_path / "authority" / "authority.sqlite3"
    monkeypatch.setenv(authority_module.AUTHORITY_DB_ENV, str(db_path))
    seen: list[int] = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        if not seen:
            seen.append(stat.S_IMODE(os.lstat(db_path).st_mode))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(authority_module.sqlite3, "connect", connect)
    os.umask(0o022)
    _store(tmp_path / "a")
    assert seen and seen[0] & 0o077 == 0, oct(seen[0])


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_two_durable_runs_starting_together_both_start(tmp_path, monkeypatch):
    db_path = tmp_path / "authority" / "authority.sqlite3"
    monkeypatch.setenv(authority_module.AUTHORITY_DB_ENV, str(db_path))
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def start(name: str) -> None:
        try:
            barrier.wait()
            _store(tmp_path / name)
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(f"run-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert stat.S_IMODE(os.lstat(db_path).st_mode) & 0o077 == 0
