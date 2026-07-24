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
    def test_start_and_stop(self, deps, monkeypatch) -> None:
        disp, _db, events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
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

    def test_mac_start_requests_input_monitoring_only_when_needed(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, _events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring", lambda: False
        )
        requests: list[bool] = []
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: requests.append(True) or True,
        )

        result = disp.dispatch("start_recording", {})

        assert result["recording"] is True
        assert requests == [True]

    def test_mac_start_refuses_when_input_monitoring_remains_denied(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring", lambda: False
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring", lambda: False
        )

        with pytest.raises(PermissionError, match="Input Monitoring permission"):
            disp.dispatch("start_recording", {})

        assert disp.services.controller.is_recording is False
        assert (
            "recording_error",
            {
                "error": (
                    "Input Monitoring permission is required to record keyboard "
                    "and mouse input. Grant it in System Settings, then try again."
                )
            },
        ) in events

    def test_mac_start_does_not_request_when_input_monitoring_is_granted(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, _events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring", lambda: True
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: pytest.fail("permission request must not run"),
        )

        assert disp.dispatch("start_recording", {})["recording"] is True


class TestLibraryCommands:
    def test_get_workflows_from_bundles(self, deps) -> None:
        disp, db, _e = deps
        db.insert_bundle("bnd1", "/tmp/b", capture_id="cap1")
        db.update_bundle("bnd1", workflow_name="My WF", steps=4, workflow_id="wf_9")
        out = disp.dispatch("get_workflows", {})
        # The frontend (src/lib/engine.ts / WorkflowLibrary) consumes a bare
        # Workflow[]; the handler returns the list directly, not {"workflows": ...}.
        assert isinstance(out, list)
        wf = out[0]
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

    def test_replay_reports_browser_setup_before_acting(self, deps, tmp_path: Path) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        order: list[str] = []

        class Bridge:
            def ensure_browser_runtime(self, progress) -> None:
                order.append("ensure")
                progress("checking", "Checking")
                progress("ready", "Ready")

            def replay(self, _bundle, out_dir):
                from engine.flow_bridge import FlowResult

                order.append("replay")
                return FlowResult(ok=True, returncode=0, out_dir=out_dir)

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch("replay_workflow", {"workflow_id": "bnd1"})

        assert order == ["ensure", "replay"]
        assert result["workflow_id"] == "bnd1"
        assert [data["state"] for event, data in events if event == "browser_runtime"] == [
            "checking",
            "ready",
        ]

    def test_browser_setup_failure_never_sends_a_workflow_action(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        class Bridge:
            replay_called = False

            def ensure_browser_runtime(self, progress) -> None:
                progress("error", "Retry")
                raise RuntimeError("browser setup failed")

            def replay(self, _bundle, out_dir):
                self.replay_called = True
                raise AssertionError("replay must not start")

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch("replay_workflow", {"workflow_id": "bnd1"})

        assert result == {"ok": False, "error": "browser setup failed"}
        assert bridge.replay_called is False
        assert any(
            event == "replay_progress" and data["state"] == "error"
            for event, data in events
        )


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

    def test_connect_uri_forwards_one_exact_string_and_emits_safe_state(
        self, deps, monkeypatch, tmp_path
    ) -> None:
        disp, _db, events = deps
        uri = (
            "openadapt://connect?pairing=oap_"
            + "A" * 43
            + "&host=https%3A%2F%2Fapp.openadapt.ai"
        )
        received: list[str] = []
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(tmp_path / "config.toml"))

        def _connect(exact_uri: str) -> dict:
            received.append(exact_uri)
            return {
                "authenticated": True,
                "host": "https://app.openadapt.ai",
                "paired": True,
            }

        monkeypatch.setattr("engine.auth.pairing.connect_uri", _connect)
        result = disp.dispatch("connect_uri", {"uri": uri})
        assert result["paired"] is True
        assert received == [uri]
        assert ("pairing_state", {
            "status": "connected",
            "host": "https://app.openadapt.ai",
        }) in events

    def test_connect_uri_requires_a_single_string_parameter(self, deps) -> None:
        disp, _db, _events = deps
        for params in ({}, {"uri": ["openadapt://connect"]}, {"argv": ["--uri", "x"]}):
            with pytest.raises(ValueError, match="uri is required"):
                disp.dispatch("connect_uri", params)


class TestConfigCommands:
    def test_get_config(self, deps) -> None:
        disp, _db, _e = deps
        cfg = disp.dispatch("get_config", {})
        # The Settings screen reads ``host``; the engine keeps ``hosted_host`` too
        # for consumers keyed on the engine field name.
        assert set(cfg) == {
            "host",
            "hosted_host",
            "deployment_lane",
            "phi_mode",
            "poll_interval_s",
        }
        assert cfg["host"] == cfg["hosted_host"]

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


class TestRunReportMapping:
    """_run_report must map openadapt-flow's real report.json onto RunReport."""

    def _write_report(self, run_dir: Path, report: dict) -> None:
        import json

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(json.dumps(report))

    def test_maps_flow_results_schema(self, deps, tmp_path: Path) -> None:
        disp, _db, _e = deps
        run_dir = tmp_path / "run"
        # openadapt-flow 1.x report.json shape: results[] of StepResult, plus
        # total_ms and est_model_cost_usd at the top level.
        self._write_report(
            run_dir,
            {
                "results": [
                    {
                        "step_id": "step_000",
                        "intent": "click 'Patients'",
                        "ok": True,
                        "resolution": {"rung": "structural", "confidence": 0.99},
                        "effect_verified": True,
                        "elapsed_ms": 812.4,
                    },
                    {
                        "step_id": "step_001",
                        "intent": "type 'Jane Doe'",
                        "ok": False,
                        "skipped": False,
                        "resolution": {"rung": "template", "confidence": 0.4},
                        "effect_verified": False,
                        "elapsed_ms": 1500.0,
                    },
                ],
                "total_ms": 2312.4,
                "est_model_cost_usd": 0.0134,
            },
        )
        rep = disp._run_report(run_dir, "wf_1", "run_1")
        assert rep["workflow_id"] == "wf_1"
        assert rep["total_steps"] == 2
        s0, s1 = rep["steps"]
        assert s0 == {
            "index": 0,
            "action": "click",
            "target": "Patients",
            "state": "verified",
            "latency_ms": 812,
            "effect": "verified",
        }
        assert s1["state"] == "failed"
        assert s1["effect"] == "not_verified"
        # total_ms -> duration_s, est_model_cost_usd -> cost_usd.
        assert rep["metrics"] == {"duration_s": 2.3, "cost_usd": 0.0134}
        assert rep["halt"] is None

    def test_maps_flow_halt(self, deps, tmp_path: Path) -> None:
        disp, _db, _e = deps
        run_dir = tmp_path / "run"
        self._write_report(
            run_dir,
            {
                "results": [
                    {
                        "step_id": "step_003",
                        "intent": "click 'Submit'",
                        "ok": False,
                        "resolution": {"rung": "ocr", "confidence": 0.2},
                        "effect_verified": False,
                        "elapsed_ms": 640.0,
                    }
                ],
                "halt": {
                    "state_id": "step_003",
                    "intent": "click 'Submit'",
                    "reason": "ambiguous target",
                    "outcome": "halt",
                },
                "total_ms": 640.0,
                "est_model_cost_usd": 0.002,
            },
        )
        rep = disp._run_report(run_dir, "wf_1", "run_1")
        assert rep["halt"] is not None
        assert rep["halt"]["step_index"] == 3
        assert rep["halt"]["step_intent"] == "click 'Submit'"
        assert rep["halt"]["reason"] == "ambiguous target"
        # resolver_rung falls back to the halted step's resolution rung.
        assert rep["halt"]["resolver_rung"] == "ocr"
        assert rep["steps"][0]["state"] == "halted"


class TestMisc:
    def test_non_mac_check_permissions_shape(self, deps, monkeypatch) -> None:
        disp, _db, _e = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        p = disp.dispatch("check_permissions", {})
        assert p == {
            "screen_recording": True,
            "accessibility": True,
            "input_monitoring": True,
        }

    def test_mac_permission_check_is_prompt_free(self, deps, monkeypatch) -> None:
        disp, _db, _e = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr("engine.dispatch._mac_preflight_screen", lambda: True)
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_accessibility", lambda: False
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring", lambda: False
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: pytest.fail("passive check must never request permission"),
        )

        assert disp.dispatch("check_permissions", {}) == {
            "screen_recording": True,
            "accessibility": False,
            "input_monitoring": False,
        }

    def test_non_mac_input_monitoring_request_returns_granted_shape(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, _e = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: pytest.fail("non-mac platforms must not request macOS access"),
        )

        assert disp.dispatch("request_input_monitoring", {}) == {
            "screen_recording": True,
            "accessibility": True,
            "input_monitoring": True,
        }

    def test_mac_input_monitoring_request_rechecks_authoritative_state(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, _e = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr("engine.dispatch._mac_preflight_screen", lambda: True)
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_accessibility", lambda: True
        )
        checks = iter((False, True))
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring",
            lambda: next(checks),
        )
        requests: list[bool] = []
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: requests.append(True) or True,
        )

        assert disp.dispatch("request_input_monitoring", {}) == {
            "screen_recording": True,
            "accessibility": True,
            "input_monitoring": True,
        }
        assert requests == [True]

    def test_mac_input_monitoring_request_does_not_assume_success(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, _e = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr("engine.dispatch._mac_preflight_screen", lambda: True)
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_accessibility", lambda: True
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring", lambda: False
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring", lambda: True
        )

        assert disp.dispatch("request_input_monitoring", {}) == {
            "screen_recording": True,
            "accessibility": True,
            "input_monitoring": False,
        }

    def test_mac_input_monitoring_request_skips_prompt_when_already_granted(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, _e = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr("engine.dispatch._mac_preflight_screen", lambda: True)
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_accessibility", lambda: True
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring", lambda: True
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: pytest.fail("granted access must not prompt again"),
        )

        assert disp.dispatch("request_input_monitoring", {}) == {
            "screen_recording": True,
            "accessibility": True,
            "input_monitoring": True,
        }

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
            "get_config", "set_config", "check_permissions",
            "request_input_monitoring", "scrub_capture",
            "approve_review", "dismiss_review", "get_pending_reviews",
        }
        assert expected.issubset(set(disp.commands))
