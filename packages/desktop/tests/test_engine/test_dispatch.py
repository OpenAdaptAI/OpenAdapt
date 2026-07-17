"""Tests for the shared EngineDispatcher (P0-2/P0-3 command wire)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.controller import RecordingState
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices


class FakeController:
    """Minimal recording controller stand-in."""

    def __init__(self) -> None:
        self.state = RecordingState.IDLE
        self._current_capture_id = None
        self._started_at = None
        self.compiled: dict | None = {"bundle_id": "bnd1", "bundle_path": "/tmp/b", "ok": True}

    @property
    def is_recording(self) -> bool:
        return self.state in (RecordingState.RECORDING, RecordingState.PAUSED)

    @property
    def current_capture_id(self):
        return self._current_capture_id

    def start(self, task_description: str = "") -> str:
        self.state = RecordingState.RECORDING
        self._current_capture_id = "cap1"
        return "cap1"

    def stop(self) -> dict:
        self.state = RecordingState.IDLE
        cid, self._current_capture_id = self._current_capture_id, None
        return {"id": cid, "duration": 1.0, "event_count": 3, "size_bytes": 10}

    def compile_capture(self, capture_id, capture_dir):
        return self.compiled


class FakeStorage:
    def get_captures(self, limit=50, review_status=None):
        return [{"capture_id": "cap1"}]

    def get_storage_usage(self):
        return {"used_bytes": 1, "max_bytes": 100}


class FakeAudit:
    def log(self, *a, **k):
        pass


@pytest.fixture
def deps(tmp_path: Path):
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    events: list[tuple[str, dict]] = []
    services = EngineServices(
        config, db=db, storage=FakeStorage(), audit=FakeAudit(),
        controller=FakeController(),
    )
    disp = EngineDispatcher(config, services=services, emit=lambda e, d: events.append((e, d)))
    yield disp, db, events
    db.close()


class TestRecordingCommands:
    def test_start_and_stop(self, deps) -> None:
        disp, _db, events = deps
        r = disp.dispatch("start_recording", {})
        assert r["capture_id"] == "cap1"
        assert ("recording_started", {"capture_id": "cap1"}) in events
        r2 = disp.dispatch("stop_recording", {})
        assert r2["capture_id"] == "cap1"
        assert any(e == "recording_stopped" for e, _ in events)

    def test_get_status_shape(self, deps) -> None:
        disp, _db, _e = deps
        s = disp.dispatch("get_status", {})
        assert set(s) >= {"recording", "paused", "duration_secs", "capture_id"}
        assert s["recording"] is False


class TestLibraryCommands:
    def test_get_workflows_from_bundles(self, deps) -> None:
        disp, db, _e = deps
        db.insert_bundle("bnd1", "/tmp/b", capture_id="cap1")
        db.update_bundle("bnd1", workflow_name="My WF", steps=4, workflow_id="wf_9")
        out = disp.dispatch("get_workflows", {})
        wf = out["workflows"][0]
        assert wf["id"] == "bnd1"
        assert wf["name"] == "My WF"
        assert wf["synced"] is True

    def test_compile_recording(self, deps) -> None:
        disp, db, events = deps
        db.insert_capture("cap1", "/tmp/cap", "2026-07-14T00:00:00+00:00")
        r = disp.dispatch("compile_recording", {"capture_id": "cap1"})
        assert r["ok"] is True
        assert r["workflow_id"] == "bnd1"
        assert any(e == "compile_progress" for e, _ in events)

    def test_compile_missing_capture(self, deps) -> None:
        disp, _db, _e = deps
        r = disp.dispatch("compile_recording", {"capture_id": "nope"})
        assert r["ok"] is False


class TestSyncCommands:
    def test_pause_resume_sync(self, deps) -> None:
        disp, _db, events = deps
        disp.dispatch("pause_sync", {})
        assert disp.dispatch("get_sync_state", {})["state"] == "paused"
        disp.dispatch("resume_sync", {})
        assert disp.dispatch("get_sync_state", {})["state"] == "synced"
        assert any(e == "sync_state" for e, _ in events)

    def test_needs_attention(self, deps) -> None:
        disp, db, events = deps
        db.insert_run("r1", "/tmp/r", bundle_id="bnd1")
        db.insert_halt("h1", "r1", status="open")
        out = disp.dispatch("get_needs_attention", {})
        assert out["count"] == 1
        assert out["open_halts"] == 1
        assert ("break_count", {"count": 1}) in events

    def test_push_workflow(self, deps, monkeypatch) -> None:
        disp, db, events = deps
        db.insert_bundle("bnd1", str(disp.config.data_dir), capture_id="cap1")
        monkeypatch.setattr(
            "engine.hosted.push",
            lambda *a, **k: {"success": True, "workflow_id": "wf_1",
                             "dashboard_url": "u", "error": ""},
        )
        r = disp.dispatch("push_workflow", {"workflow_id": "bnd1"})
        assert r["ok"] is True
        assert r["workflow_id"] == "wf_1"
        assert any(e == "sync_state" for e, _ in events)


class TestAuthCommands:
    def test_login_paste(self, deps, monkeypatch, fake_keyring) -> None:
        disp, _db, _e = deps
        cred = {"kind": "ingest_token", "token": "t", "refresh_token": None,
                "org_id": "org_1", "host": disp.config.hosted_host, "expires_at": None}
        monkeypatch.setattr(
            "engine.auth.paste.PasteTokenProvider.login", lambda self, token=None: cred
        )
        r = disp.dispatch("login_paste", {"token": "t"})
        assert r["authenticated"] is True
        assert r["org_id"] == "org_1"

    def test_get_auth_status_unauthed(self, deps, fake_keyring) -> None:
        disp, _db, _e = deps
        assert disp.dispatch("get_auth_status", {})["authenticated"] is False


class TestConfigCommands:
    def test_get_config(self, deps) -> None:
        disp, _db, _e = deps
        cfg = disp.dispatch("get_config", {})
        assert set(cfg) == {"hosted_host", "deployment_lane", "phi_mode", "poll_interval_s"}

    def test_set_config_persists(self, deps, monkeypatch, tmp_path) -> None:
        disp, _db, _e = deps
        toml_path = tmp_path / "config.toml"
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(toml_path))
        r = disp.dispatch("set_config", {"key": "deployment_lane", "value": "byoc"})
        assert r["ok"] is True
        assert disp.config.deployment_lane == "byoc"
        assert "byoc" in toml_path.read_text()

    def test_set_config_rejects_unknown_key(self, deps) -> None:
        disp, _db, _e = deps
        r = disp.dispatch("set_config", {"key": "s3_secret_access_key", "value": "x"})
        assert r["ok"] is False


class TestMisc:
    def test_check_permissions_shape(self, deps) -> None:
        disp, _db, _e = deps
        p = disp.dispatch("check_permissions", {})
        assert set(p) == {"screen_recording", "accessibility"}

    def test_unknown_command_raises(self, deps) -> None:
        disp, _db, _e = deps
        with pytest.raises(KeyError):
            disp.dispatch("does_not_exist", {})

    def test_open_teach_relays_event(self, deps) -> None:
        disp, _db, events = deps
        disp.dispatch("open_teach", {"workflow_id": "bnd1"})
        assert any(e == "open_window" for e, _ in events)

    def test_all_frontend_commands_registered(self, deps) -> None:
        disp, _db, _e = deps
        # The exact CMD catalog from the app's src/lib/engine.ts.
        expected = {
            "start_recording", "stop_recording", "pause_recording",
            "resume_recording", "get_status", "get_workflows", "get_captures",
            "get_storage_usage", "compile_recording", "replay_workflow",
            "run_workflow", "get_run_report", "teach_fix", "push_workflow",
            "get_sync_state", "pause_sync", "resume_sync", "get_needs_attention",
            "login_browser", "login_paste", "logout", "get_auth_status",
            "get_config", "set_config", "check_permissions", "scrub_capture",
            "approve_review", "dismiss_review", "get_pending_reviews",
        }
        assert expected.issubset(set(disp.commands))
