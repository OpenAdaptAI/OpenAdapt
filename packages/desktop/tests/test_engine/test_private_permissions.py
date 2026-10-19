"""Local data stays private to the account that runs Desktop (ledger B094).

Captures, run folders, reports and the index database can hold patient
details. On a shared workstation another local account must not be able to
list or read them, so every directory Desktop creates is 0700 and every file
it creates is 0600, whatever umask the process started with.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from openadapt_desktop.config import EngineConfig
from openadapt_desktop.controller import RecordingController
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices
from openadapt_desktop.storage_manager import StorageManager

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits do not apply on Windows"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def default_umask():
    """Start each test from the macOS and Linux default umask (022)."""

    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _config(data_dir: Path) -> EngineConfig:
    return EngineConfig(
        data_dir=data_dir,
        storage_mode="air-gapped",
        log_level="WARNING",
        audit_log_path=data_dir / "audit.jsonl",
    )


def test_storage_initialize_makes_data_dir_private(tmp_path: Path) -> None:
    data_dir = tmp_path / ".openadapt"
    storage = StorageManager(_config(data_dir))
    storage.initialize()
    try:
        assert _mode(data_dir) == 0o700
        for name in ("captures", "archive", "tombstones"):
            assert _mode(data_dir / name) == 0o700, name
        db_path = data_dir / "index.db"
        assert _mode(db_path) == 0o600
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                assert _mode(sidecar) == 0o600, suffix
    finally:
        storage.db.close()


def test_existing_open_data_dir_is_tightened(tmp_path: Path) -> None:
    """An install made before this fix (0755 folders, 0644 database) is fixed on start."""

    data_dir = tmp_path / ".openadapt"
    data_dir.mkdir(mode=0o755)
    os.chmod(data_dir, 0o755)
    for name in ("captures", "archive", "tombstones"):
        (data_dir / name).mkdir(mode=0o755)
        os.chmod(data_dir / name, 0o755)
    old_db = IndexDB(data_dir / "index.db")
    old_db.initialize()
    old_db.close()
    os.chmod(data_dir / "index.db", 0o644)

    storage = StorageManager(_config(data_dir))
    storage.initialize()
    try:
        assert _mode(data_dir) == 0o700
        for name in ("captures", "archive", "tombstones"):
            assert _mode(data_dir / name) == 0o700, name
        assert _mode(data_dir / "index.db") == 0o600
    finally:
        storage.db.close()


def test_index_db_alone_is_private(tmp_path: Path) -> None:
    db = IndexDB(tmp_path / "data" / "index.db")
    db.initialize()
    try:
        assert _mode(tmp_path / "data") == 0o700
        assert _mode(tmp_path / "data" / "index.db") == 0o600
    finally:
        db.close()


def test_capture_folder_and_its_files_are_private(tmp_path: Path) -> None:
    captures = tmp_path / ".openadapt" / "captures"
    controller = RecordingController(captures_dir=captures)
    controller.start()
    try:
        assert _mode(captures) == 0o700
        (capture_dir,) = list(captures.iterdir())
        assert _mode(capture_dir) == 0o700
        assert _mode(capture_dir / "screenshots") == 0o700
        assert _mode(capture_dir / "video") == 0o700
        assert _mode(capture_dir / "meta.json") == 0o600
        assert _mode(capture_dir / "state.json") == 0o600
    finally:
        controller.stop()


def test_replay_run_folder_is_private(tmp_path: Path) -> None:
    config = _config(tmp_path / ".openadapt")
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
    seen: dict[str, int] = {}

    class Bridge:
        def replay(self, _bundle, out_dir, *, config):
            from openadapt_desktop.flow_bridge import FlowResult

            seen["runs"] = _mode(out_dir.parent)
            seen["run"] = _mode(out_dir)
            seen["data_dir"] = _mode(out_dir.parent.parent)
            (out_dir / "report.json").write_text(json.dumps({"results": []}))
            return FlowResult(ok=True, returncode=0, out_dir=out_dir)

    services = EngineServices(config, db=db, storage=None, audit=None)
    services._flow_bridge = Bridge()
    disp = EngineDispatcher(config, services=services, emit=lambda *_: None)
    try:
        disp.dispatch(
            "replay_workflow",
            {"workflow_id": "bnd1", "target": {"backend": "web"}},
        )
    finally:
        db.close()

    assert seen == {"runs": 0o700, "run": 0o700, "data_dir": 0o700}


def test_cli_and_sidecar_entrypoints_restrict_the_umask(tmp_path: Path, monkeypatch) -> None:
    """Files Flow writes into a run folder inherit the engine's umask."""

    from openadapt_desktop import cli
    from openadapt_desktop import main as engine_main

    config = _config(tmp_path / ".openadapt")
    monkeypatch.setattr(cli, "EngineConfig", lambda: config)
    cli.main(["config"])
    assert os.umask(0o022) == 0o077

    monkeypatch.setattr(engine_main.sys, "argv", ["engine", engine_main.EMBEDDED_FLOW_MODE])
    monkeypatch.setattr(engine_main, "_run_embedded_flow", lambda: None)
    engine_main.main()
    assert os.umask(0o022) == 0o077
