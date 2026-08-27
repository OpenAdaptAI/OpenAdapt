"""Tests for the shared EngineDispatcher (P0-2/P0-3 command wire)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from engine.config import EngineConfig
from engine.controller import RecordingState
from engine.db import IndexDB
from engine.dispatch import EngineDispatcher, EngineServices
from engine.flow_bridge import FlowBridge


def _precise_report(
    outcome: str,
    *,
    production_eligible: bool,
    execution_completed: bool,
) -> dict:
    required = {"authorization": 1, "identity": 0, "postcondition": 0, "effect": 0}
    passed = dict(required)
    if outcome == "COMPLETED_UNVERIFIED":
        required["effect"] = 1
    compensation_actions = 1 if outcome == "ROLLED_BACK" else 0
    evidence = ["authorization"]
    if compensation_actions:
        evidence.append("compensation")
    return {
        "success": outcome == "VERIFIED",
        "model_calls": 0,
        "execution_outcome": outcome,
        "execution_profile": "standard",
        "production_eligible": production_eligible,
        "execution_completed": execution_completed,
        "outcome_envelope": {
            "version": "openadapt.execution-outcome/v1",
            "outcome": outcome,
            "profile": "standard",
            "production_eligible": production_eligible,
            "execution_completed": execution_completed,
            "required_contracts": required,
            "passed_contracts": passed,
            "evidence_classes": evidence,
            "model_calls": 0,
            "external_network_calls": "none",
            "compensation_actions": compensation_actions,
        },
    }


def _first_supervised_bundle(path: Path, *, state_changing: bool) -> dict:
    flow_ir = pytest.importorskip("openadapt_flow.ir")

    from engine.qualification import initialize_qualification

    action = flow_ir.ActionKind.CLICK if state_changing else flow_ir.ActionKind.WAIT
    flow_ir.Workflow(
        name="first-supervised-test",
        steps=[
            flow_ir.Step(
                id="step-1",
                intent="Inspect the test page",
                action=action,
            )
        ],
    ).save(path)
    return initialize_qualification(
        path,
        workflow_id="bnd1",
        target_kind="web",
        application="Reference app",
        application_version="1",
        environment_digest=_target_digest({"backend": "web"}),
        required_capabilities=[],
    )


def _target_digest(target: dict) -> str:
    from engine.targets import ExecutionTarget

    return EngineDispatcher._canonical_target_digest(
        ExecutionTarget.model_validate(target)
    )


class FakeController:
    """Minimal recording controller stand-in."""

    def __init__(self, db: IndexDB, capture_dir: Path) -> None:
        self.state = RecordingState.IDLE
        self._current_capture_id = None
        self._started_at = None
        self.db = db
        self.capture_dir = capture_dir
        self.compiled: dict | None = {"bundle_id": "bnd1", "bundle_path": "/tmp/b", "ok": True}
        self.compile_calls = 0

    @property
    def is_recording(self) -> bool:
        return self.state in (RecordingState.RECORDING, RecordingState.PAUSED)

    @property
    def current_capture_id(self):
        return self._current_capture_id

    def start(
        self,
        task_description: str = "",
        *,
        metadata: dict[str, object] | None = None,
    ) -> str:
        self.state = RecordingState.RECORDING
        self._current_capture_id = "cap1"
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        if self.db.get_capture("cap1") is None:
            self.db.insert_capture(
                "cap1",
                str(self.capture_dir),
                "2026-07-28T00:00:00+00:00",
                task_description=task_description,
            )
        return "cap1"

    def stop(self) -> dict:
        self.state = RecordingState.IDLE
        cid, self._current_capture_id = self._current_capture_id, None
        return {"id": cid, "duration": 1.0, "event_count": 3, "size_bytes": 10}

    def compile_capture(self, capture_id, capture_dir):
        self.compile_calls += 1
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
        config,
        db=db,
        storage=FakeStorage(),
        audit=FakeAudit(),
        controller=FakeController(db, config.data_dir / "captures" / "cap1"),
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
        assert r2["compile"]["workflow_id"] == "bnd1"
        assert r2["compile"]["recording_retained"] is True
        assert disp.services.controller.compile_calls == 1
        assert any(e == "recording_stopped" for e, _ in events)
        assert [data["state"] for event, data in events if event == "compile_progress"] == [
            "compiling",
            "compiled",
        ]

    def test_first_workflow_stage_survives_dispatcher_restart(
        self, deps, monkeypatch
    ) -> None:
        disp, db, _events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        monkeypatch.setattr(
            "engine.capabilities.ensure_backend_capability",
            lambda *_args, **_kwargs: None,
        )

        class Session:
            def __init__(self, out_dir: Path) -> None:
                self.out_dir = out_dir

            def stop(self):
                from engine.flow_bridge import FlowResult

                self.out_dir.mkdir(parents=True, exist_ok=True)
                (self.out_dir / "meta.json").write_text(
                    json.dumps({"started_at": "2026-08-26T00:00:00+00:00"})
                )
                return FlowResult(ok=True, returncode=0, out_dir=self.out_dir)

        class Bridge:
            def ensure_browser_runtime(self, _progress) -> None:
                pass

            def start_record(self, out_dir, **_kwargs):
                return Session(out_dir)

        disp.services._flow_bridge = Bridge()
        monkeypatch.setattr(
            disp,
            "_initialize_first_workflow_review",
            lambda **_kwargs: {"ok": True},
        )
        disp.dispatch("set_first_workflow_stage", {"stage": "record"})

        started = disp.dispatch(
            "start_recording",
            {
                "first_workflow": True,
                "purpose": "Read one test record",
                "target": {
                    "backend": "web",
                    "url": "https://example.test/records",
                },
            },
        )
        stopped = disp.dispatch("stop_recording", {})

        assert started["capture_id"]
        assert stopped["compile"]["workflow_id"] == "bnd1"
        restarted = EngineDispatcher(disp.config, services=disp.services)
        assert restarted.dispatch("get_first_workflow_state", {})["state"] == {
            "stage": "review",
            "capture_id": started["capture_id"],
            "workflow_id": "bnd1",
            "target": {
                "backend": "web",
                "url": "https://example.test/records",
                "linux_allow_physical_input": False,
            },
            "task": "Read one test record",
            "updated_at": db.get_first_workflow_state()["updated_at"],
        }

    @pytest.mark.parametrize(
        "params",
        [
            {"first_workflow": True, "purpose": "Read one test record"},
            {
                "first_workflow": True,
                "purpose": "Read one test record",
                "target": {"backend": "web"},
            },
            {
                "first_workflow": True,
                "target": {
                    "backend": "web",
                    "url": "https://example.test/records",
                },
            },
        ],
    )
    def test_first_workflow_requires_a_real_target_and_named_task(
        self, deps, monkeypatch, params: dict
    ) -> None:
        disp, _db, events = deps
        monkeypatch.setattr(
            "engine.capabilities.ensure_backend_capability",
            lambda *_args, **_kwargs: None,
        )

        class Bridge:
            def ensure_browser_runtime(self, _progress) -> None:
                pass

            def demo_record(self, *_args, **_kwargs):
                raise AssertionError("first workflow must not use the bundled demo")

            def start_record(self, *_args, **_kwargs):
                raise AssertionError("an incomplete first target must not record")

        disp.services._flow_bridge = Bridge()

        with pytest.raises(ValueError):
            disp.dispatch("start_recording", params)

        assert disp.services.controller.is_recording is False
        assert any(event == "recording_error" for event, _data in events)

    def test_recompile_does_not_move_a_later_first_workflow_stage_back_to_review(
        self, deps
    ) -> None:
        disp, db, _events = deps
        capture_dir = disp.services.controller.capture_dir
        capture_dir.mkdir(parents=True, exist_ok=True)
        db.insert_capture(
            "cap1",
            str(capture_dir),
            "2026-08-26T00:00:00+00:00",
        )
        db.set_first_workflow_state(
            stage="qualification",
            capture_id="cap1",
            workflow_id="bnd1",
            task="Read one test record",
        )
        disp.services.controller.compiled = {
            "bundle_id": "bnd2",
            "bundle_path": "/tmp/b2",
            "ok": True,
        }

        result = disp.dispatch("compile_recording", {"capture_id": "cap1"})

        assert result["ok"] is True
        assert db.get_first_workflow_state()["stage"] == "qualification"
        assert db.get_first_workflow_state()["workflow_id"] == "bnd1"

    def test_compile_failure_retains_recording_and_retry_succeeds(self, deps, monkeypatch) -> None:
        disp, _db, events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        disp.services.controller.compiled = None

        disp.dispatch("start_recording", {})
        result = disp.dispatch("stop_recording", {})

        assert result["compile"]["ok"] is False
        assert result["compile"]["recording_retained"] is True
        assert disp.services.controller.capture_dir.is_dir()
        failure = [
            data
            for event, data in events
            if event == "compile_progress" and data["state"] == "failed"
        ][-1]
        assert failure["automatic"] is True
        assert failure["recording_retained"] is True

        disp.services.controller.compiled = {
            "bundle_id": "bnd-retry",
            "bundle_path": "/tmp/retry",
            "ok": True,
        }
        retry = disp.dispatch("compile_recording", {"capture_id": "cap1"})

        assert retry["ok"] is True
        assert retry["workflow_id"] == "bnd-retry"
        assert disp.services.controller.capture_dir.is_dir()
        compiled = [
            data
            for event, data in events
            if event == "compile_progress" and data["state"] == "compiled"
        ][-1]
        assert compiled["automatic"] is False

    def test_get_status_shape(self, deps) -> None:
        disp, _db, _e = deps
        s = disp.dispatch("get_status", {})
        assert set(s) >= {
            "recording",
            "paused",
            "duration_secs",
            "capture_id",
            "controls",
        }
        assert s["recording"] is False
        assert s["controls"] == {"pause": False, "resume": False, "stop": False}

    def test_mac_start_requests_input_monitoring_only_when_needed(self, deps, monkeypatch) -> None:
        disp, _db, _events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr("engine.dispatch._mac_preflight_input_monitoring", lambda: False)
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
        monkeypatch.setattr("engine.dispatch._mac_preflight_input_monitoring", lambda: False)
        monkeypatch.setattr("engine.dispatch._mac_request_input_monitoring", lambda: False)

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
        monkeypatch.setattr("engine.dispatch._mac_preflight_input_monitoring", lambda: True)
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: pytest.fail("permission request must not run"),
        )

        assert disp.dispatch("start_recording", {})["recording"] is True

    def test_mac_web_authoring_skips_input_monitoring_and_prepares_browser(
        self, deps, monkeypatch
    ) -> None:
        disp, _db, events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr(
            "engine.dispatch._mac_preflight_input_monitoring",
            lambda: pytest.fail("web authoring must not inspect Input Monitoring"),
        )
        monkeypatch.setattr(
            "engine.dispatch._mac_request_input_monitoring",
            lambda: pytest.fail("web authoring must not request Input Monitoring"),
        )
        calls: list[str] = []

        class Session:
            pass

        class Bridge:
            def ensure_browser_runtime(self, progress) -> None:
                calls.append("ensure")
                progress("checking", "Checking")
                progress("ready", "Ready")

            def start_record(self, *_args, **_kwargs):
                calls.append("start")
                return Session()

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "start_recording",
            {
                "target": {
                    "backend": "web",
                    "url": "https://app.example",
                }
            },
        )

        assert result["recording"] is True
        assert calls == ["ensure", "start"]
        states = [data["state"] for event, data in events if event == "browser_runtime"]
        assert states == ["checking", "ready"]

    def test_blank_web_target_generates_and_registers_bundled_demo(self, deps, monkeypatch) -> None:
        disp, db, events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        calls: list[str] = []

        class Bridge:
            def ensure_browser_runtime(self, progress) -> None:
                calls.append("ensure")
                progress("ready", "Ready")

            def demo_record(self, out_dir):
                from engine.flow_bridge import FlowResult

                calls.append("demo")
                out_dir.mkdir(parents=True)
                (out_dir / "meta.json").write_text(
                    json.dumps(
                        {
                            "started_at": "2026-07-24T00:00:00+00:00",
                            "task_description": "Bundled demo",
                        }
                    )
                )
                return FlowResult(
                    ok=True,
                    returncode=0,
                    stdout="Recording written",
                    out_dir=out_dir,
                )

            def start_record(self, *_args, **_kwargs):
                raise AssertionError("blank web target uses demo-record directly")

        disp.services._flow_bridge = Bridge()

        result = disp.dispatch(
            "start_recording",
            {
                "target": {"backend": "web"},
                "purpose": "My first workflow",
            },
        )

        assert calls == ["ensure", "demo"]
        assert result["recording"] is False
        assert result["compile"]["workflow_id"] == "bnd1"
        assert db.get_capture(result["capture_id"])["task_description"] == ("My first workflow")
        assert [event for event, _data in events if event.startswith("recording_")] == [
            "recording_started",
            "recording_stopped",
        ]

    def test_browser_setup_failure_refuses_before_record_process(self, deps, monkeypatch) -> None:
        disp, _db, events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")

        class Bridge:
            start_called = False

            def ensure_browser_runtime(self, _progress) -> None:
                raise RuntimeError("download failed")

            def start_record(self, *_args, **_kwargs):
                self.start_called = True
                raise AssertionError("record process must not start")

        bridge = Bridge()
        disp.services._flow_bridge = bridge

        with pytest.raises(
            RuntimeError,
            match="Browser setup failed before recording began",
        ):
            disp.dispatch(
                "start_recording",
                {"target": {"backend": "web"}},
            )

        assert bridge.start_called is False
        assert (
            "recording_error",
            {"error": "Browser setup failed before recording began"},
        ) in events

    @pytest.mark.parametrize(
        "target",
        [
            {"backend": "windows", "agent_url": "http://waa.internal:5001"},
            {
                "backend": "macos",
                "macos_app": "Clinical Notes",
                "macos_window_title": "Patient Jane Doe",
            },
            {
                "backend": "linux",
                "linux_app": "clinical-app",
                "linux_window_title": "Patient Jane Doe",
            },
            {
                "backend": "rdp",
                "rdp_window": "Microsoft Remote Desktop",
                "rdp_window_title": "Patient Jane Doe",
                "rdp_readiness_text": "MRN 12345",
            },
            {
                "backend": "citrix",
                "rdp_window": "Citrix Viewer",
                "rdp_window_title": "Patient Jane Doe",
                "rdp_readiness_text": "MRN 12345",
            },
        ],
    )
    def test_target_aware_authoring_stages_exact_private_flow_record_request(
        self, deps, monkeypatch, target: dict
    ) -> None:
        disp, db, events = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        private_requests: list[dict] = []
        process_calls: list[tuple] = []

        class Session:
            def __init__(self, out_dir: Path) -> None:
                self.out_dir = out_dir

            def stop(self):
                from engine.flow_bridge import FlowResult

                self.out_dir.mkdir(parents=True, exist_ok=True)
                (self.out_dir / "meta.json").write_text(
                    json.dumps(
                        {
                            "started_at": "2026-07-24T00:00:00+00:00",
                            "task_description": "Review patient workflow",
                        }
                    )
                )
                sensitive = " ".join(
                    str(value) for value in target.values() if isinstance(value, str)
                )
                return FlowResult(
                    ok=True,
                    returncode=0,
                    stdout=f"record target {sensitive} Review patient workflow",
                    out_dir=self.out_dir,
                )

        class Bridge:
            def start_record(self, out_dir, *, request, stop_path, ready_path):
                private_requests.append(yaml.safe_load(request.read_text()))
                process_calls.append((out_dir, request, stop_path, ready_path))
                return Session(out_dir)

        disp.services._flow_bridge = Bridge()
        started = disp.dispatch(
            "start_recording",
            {
                "target": target,
                "purpose": "Review patient workflow",
            },
        )
        stopped = disp.dispatch("stop_recording", {})

        assert started["recording"] is True
        assert stopped["capture_id"] == started["capture_id"]
        assert stopped["compile"]["workflow_id"] == "bnd1"
        assert private_requests[0]["target"] == target
        assert private_requests[0]["task"] == "Review patient workflow"
        assert db.get_capture(started["capture_id"]) is not None
        # The actual process boundary receives only non-sensitive private paths.
        assert "Jane Doe" not in repr(process_calls)
        assert "MRN 12345" not in repr(process_calls)
        assert "Review patient workflow" not in repr(process_calls)
        emitted = [data["line"] for event, data in events if event == "log_line"]
        assert len(emitted) == 1
        assert "Jane Doe" not in emitted[0]
        assert "MRN 12345" not in emitted[0]
        assert "Review patient workflow" not in emitted[0]
        assert [data["state"] for event, data in events if event == "compile_progress"] == [
            "compiling",
            "compiled",
        ]


class TestLibraryCommands:
    def test_presentation_export_is_capture_scoped(self, deps, monkeypatch, tmp_path) -> None:
        disp, db, _events = deps
        capture = disp.config.data_dir / "captures" / "capture"
        capture.mkdir(parents=True)
        db.insert_capture("cap-present", str(capture), "2026-07-25T00:00:00Z")
        inspected: list[Path] = []
        monkeypatch.setattr(
            "engine.presentation_export.presentation_export_status",
            lambda path: inspected.append(path) or {"ready": True},
        )

        assert disp.dispatch(
            "get_presentation_export_status",
            {"capture_id": "cap-present"},
        ) == {"ready": True}
        assert inspected == [capture]

        outside = tmp_path / "outside"
        outside.mkdir()
        db.insert_capture("cap-outside", str(outside), "2026-07-25T00:00:00Z")
        with pytest.raises(ValueError, match="outside the local capture boundary"):
            disp.dispatch(
                "get_presentation_export_status",
                {"capture_id": "cap-outside"},
            )

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

    def test_first_supervised_replay_refuses_state_change_before_flow(
        self, deps
    ) -> None:
        disp, db, events = deps
        bundle = disp.config.data_dir / "bundles" / "bnd1"
        review = _first_supervised_bundle(bundle, state_changing=True)
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.set_first_workflow_state(
            stage="review",
            capture_id="cap1",
            workflow_id="bnd1",
            task="Inspect the test page",
        )

        class Bridge:
            replay_called = False

            def replay(self, *_args, **_kwargs):
                self.replay_called = True
                raise AssertionError("unsafe first replay must not reach Flow")

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch(
            "replay_first_workflow",
            {
                "workflow_id": "bnd1",
                "target": {"backend": "web"},
                "admission": {
                    "workflow_id": "bnd1",
                    "project_revision": review["project"]["revision"],
                    "bundle_content_digest": review["graph"]["bundle"]["provenance"][
                        "content_digest"
                    ],
                },
            },
        )

        assert result == {
            "ok": False,
            "outcome": "refused",
            "pre_action_refusal": True,
            "error": (
                "Qualification is required before the first run can change "
                "application state."
            ),
        }
        assert bridge.replay_called is False
        assert not any(event == "replay_progress" for event, _data in events)

    def test_first_supervised_replay_refuses_a_stale_exact_bundle_binding(
        self, deps
    ) -> None:
        disp, db, _events = deps
        bundle = disp.config.data_dir / "bundles" / "bnd1"
        review = _first_supervised_bundle(bundle, state_changing=False)
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.set_first_workflow_state(
            stage="review",
            capture_id="cap1",
            workflow_id="bnd1",
            task="Inspect the test page",
        )

        class Bridge:
            replay_called = False

            def replay(self, *_args, **_kwargs):
                self.replay_called = True
                raise AssertionError("a stale admission must not reach Flow")

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch(
            "replay_first_workflow",
            {
                "workflow_id": "bnd1",
                "target": {"backend": "web"},
                "admission": {
                    "workflow_id": "bnd1",
                    "project_revision": review["project"]["revision"],
                    "bundle_content_digest": "0" * 64,
                },
            },
        )

        assert result["pre_action_refusal"] is True
        assert result["error"].startswith("The workflow changed after review")
        assert bridge.replay_called is False

    @pytest.mark.parametrize(
        "extra_params",
        [
            {"target": {"backend": "web", "url": "https://other.test"}},
            {"target": {"backend": "web"}, "deployment_config": "config.yaml"},
        ],
    )
    def test_first_supervised_replay_refuses_a_changed_execution_target(
        self, deps, tmp_path: Path, extra_params: dict, monkeypatch
    ) -> None:
        disp, db, _events = deps
        bundle = disp.config.data_dir / "bundles" / "bnd1"
        review = _first_supervised_bundle(bundle, state_changing=False)
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.set_first_workflow_state(
            stage="review",
            capture_id="cap1",
            workflow_id="bnd1",
            target={"backend": "web"},
            task="Inspect the test page",
        )
        if "deployment_config" in extra_params:
            config = tmp_path / "config.yaml"
            config.write_text("backend:\n  kind: web\n")
            extra_params = {**extra_params, "deployment_config": str(config)}
        monkeypatch.setattr(
            "engine.capabilities.ensure_backend_capability",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("changed target must refuse before capability setup")
            ),
        )

        class Bridge:
            replay_called = False

            def replay(self, *_args, **_kwargs):
                self.replay_called = True
                raise AssertionError("changed target must not reach Flow")

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch(
            "replay_first_workflow",
            {
                "workflow_id": "bnd1",
                **extra_params,
                "admission": {
                    "workflow_id": "bnd1",
                    "project_revision": review["project"]["revision"],
                    "bundle_content_digest": review["graph"]["bundle"][
                        "provenance"
                    ]["content_digest"],
                },
            },
        )

        assert result["pre_action_refusal"] is True
        assert bridge.replay_called is False

    def test_uncertain_first_replay_requires_reconciliation_before_retry(
        self, deps, monkeypatch
    ) -> None:
        disp, db, _events = deps
        bundle = disp.config.data_dir / "bundles" / "bnd1"
        review = _first_supervised_bundle(bundle, state_changing=False)
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.set_first_workflow_state(
            stage="review",
            capture_id="cap1",
            workflow_id="bnd1",
            target={"backend": "web"},
            task="Inspect the test page",
        )
        monkeypatch.setattr(
            "engine.capabilities.ensure_backend_capability",
            lambda *_args, **_kwargs: None,
        )

        class Bridge:
            replay_calls = 0

            def ensure_browser_runtime(self, _progress) -> None:
                return None

            def replay(self, *_args, **_kwargs):
                self.replay_calls += 1
                raise RuntimeError("connection ended after dispatch")

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        params = {
            "workflow_id": "bnd1",
            "target": {"backend": "web"},
            "admission": {
                "workflow_id": "bnd1",
                "project_revision": review["project"]["revision"],
                "bundle_content_digest": review["graph"]["bundle"]["provenance"][
                    "content_digest"
                ],
            },
        }

        first = disp.dispatch("replay_first_workflow", params)
        second = disp.dispatch("replay_first_workflow", params)

        assert first["outcome"] == "unknown"
        assert first["persistence"]["state"] == "persisted"
        assert db.get_first_workflow_state()["stage"] == "reconciliation"
        assert second["pre_action_refusal"] is True
        assert bridge.replay_calls == 1

        reconciled = disp.dispatch(
            "set_first_workflow_stage",
            {"stage": "review", "workflow_id": "bnd1"},
        )
        assert reconciled["state"]["stage"] == "review"

    def test_first_supervised_replay_runs_an_exact_reviewed_read_only_bundle(
        self, deps, monkeypatch
    ) -> None:
        disp, db, _events = deps
        bundle = disp.config.data_dir / "bundles" / "bnd1"
        review = _first_supervised_bundle(bundle, state_changing=False)
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.set_first_workflow_state(
            stage="review",
            capture_id="cap1",
            workflow_id="bnd1",
            target={"backend": "web"},
            task="Inspect the test page",
        )
        monkeypatch.setattr(
            "engine.capabilities.ensure_backend_capability",
            lambda *_args, **_kwargs: None,
        )

        class Bridge:
            replay_called = False

            def ensure_browser_runtime(self, _progress) -> None:
                return None

            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                self.replay_called = True
                (out_dir / "report.json").write_text('{"success": true}')
                return FlowResult(ok=True, returncode=0, out_dir=out_dir)

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch(
            "replay_first_workflow",
            {
                "workflow_id": "bnd1",
                "target": {"backend": "web"},
                "admission": {
                    "workflow_id": "bnd1",
                    "project_revision": review["project"]["revision"],
                    "bundle_content_digest": review["graph"]["bundle"]["provenance"][
                        "content_digest"
                    ],
                },
            },
        )

        assert bridge.replay_called is True
        assert result["outcome"] == "success"
        assert db.get_first_workflow_state()["stage"] == "result"

    def test_fresh_first_compile_creates_review_and_reaches_supervised_replay(
        self, deps, monkeypatch
    ) -> None:
        flow_ir = pytest.importorskip("openadapt_flow.ir")

        disp, db, _events = deps
        capture_dir = disp.services.controller.capture_dir
        capture_dir.mkdir(parents=True, exist_ok=True)
        db.insert_capture(
            "cap1",
            str(capture_dir),
            "2026-08-27T00:00:00+00:00",
        )
        db.set_first_workflow_state(
            stage="record",
            capture_id="cap1",
            target={"backend": "web", "url": "https://example.test/records"},
            task="Inspect the test record",
        )
        bundle = disp.config.data_dir / "bundles" / "fresh-bnd1"

        def compile_fresh(_capture_id, _capture_dir):
            flow_ir.Workflow(
                name="fresh-first-workflow",
                steps=[
                    flow_ir.Step(
                        id="step-1",
                        intent="Wait for the test record",
                        action=flow_ir.ActionKind.WAIT,
                    )
                ],
            ).save(bundle)
            db.insert_bundle("fresh-bnd1", str(bundle), capture_id="cap1")
            return {
                "bundle_id": "fresh-bnd1",
                "bundle_path": str(bundle),
                "ok": True,
            }

        monkeypatch.setattr(
            disp.services.controller,
            "compile_capture",
            compile_fresh,
        )
        monkeypatch.setattr(
            "engine.capabilities.ensure_backend_capability",
            lambda *_args, **_kwargs: None,
        )

        compiled = disp.dispatch("compile_recording", {"capture_id": "cap1"})
        review = disp.dispatch(
            "get_qualification",
            {"workflow_id": "fresh-bnd1"},
        )

        assert compiled["ok"] is True
        assert review["draft_environment"] is True
        assert review["project"]["revision"] >= 1
        assert db.get_first_workflow_state()["stage"] == "review"
        retrieved = disp._initialize_first_workflow_review(
            workflow_id="fresh-bnd1",
            bundle_path=bundle,
            target={"backend": "web", "url": "https://example.test/records"},
        )
        assert retrieved["project"]["revision"] == review["project"]["revision"]
        assert (
            retrieved["graph"]["bundle"]["provenance"]["content_digest"]
            == review["graph"]["bundle"]["provenance"]["content_digest"]
        )
        certification = disp.dispatch(
            "certify_qualification",
            {"workflow_id": "fresh-bnd1"},
        )
        assert certification["ok"] is False
        assert "exact qualification environment" in certification["error"]
        early_qualification = disp.dispatch(
            "initialize_qualification",
            {
                "workflow_id": "fresh-bnd1",
                "target_kind": "web",
                "application": "Reference app",
                "application_version": "1",
                "environment_label": "reference-test-environment",
            },
        )
        assert early_qualification["ok"] is False
        assert "Save the first supervised run" in early_qualification["error"]

        class Bridge:
            replay_called = False

            def ensure_browser_runtime(self, _progress) -> None:
                return None

            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                self.replay_called = True
                (out_dir / "report.json").write_text('{"success": true}')
                return FlowResult(ok=True, returncode=0, out_dir=out_dir)

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch(
            "replay_first_workflow",
            {
                "workflow_id": "fresh-bnd1",
                "target": {
                    "backend": "web",
                    "url": "https://example.test/records",
                },
                "admission": {
                    "workflow_id": "fresh-bnd1",
                    "project_revision": review["project"]["revision"],
                    "bundle_content_digest": review["graph"]["bundle"][
                        "provenance"
                    ]["content_digest"],
                },
            },
        )

        assert bridge.replay_called is True
        assert result["outcome"] == "success"
        assert result["persistence"]["state"] == "persisted"
        assert db.get_first_workflow_state()["stage"] == "result"
        qualification = disp.dispatch(
            "initialize_qualification",
            {
                "workflow_id": "fresh-bnd1",
                "target_kind": "web",
                "application": "Reference app",
                "application_version": "1",
                "environment_label": "reference-test-environment",
            },
        )
        assert qualification["ok"] is True
        assert qualification["draft_environment"] is False
        assert qualification["project"]["environment"]["application"] == "Reference app"

    @pytest.mark.parametrize("command", ["replay_workflow", "run_workflow"])
    def test_normal_execution_cannot_bypass_the_active_first_review(
        self, deps, command: str
    ) -> None:
        disp, db, _events = deps
        bundle = disp.config.data_dir / "bundles" / "bnd1"
        bundle.mkdir(parents=True)
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.set_first_workflow_state(
            stage="review",
            capture_id="cap1",
            workflow_id="bnd1",
            target={"backend": "web", "url": "https://example.test"},
            task="Inspect the test page",
        )

        class Bridge:
            replay_called = False
            run_called = False

            def replay(self, *_args, **_kwargs):
                self.replay_called = True
                raise AssertionError("normal replay must not bypass the first review")

            def run(self, *_args, **_kwargs):
                self.run_called = True
                raise AssertionError("normal run must not bypass the first review")

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch(command, {"workflow_id": "bnd1"})

        assert result["pre_action_refusal"] is True
        assert "first supervised run" in result["error"]
        assert bridge.replay_called is False
        assert bridge.run_called is False

    def test_completed_first_workflow_allows_exact_environment_on_draft_descendant(
        self, deps
    ) -> None:
        flow_ir = pytest.importorskip("openadapt_flow.ir")

        disp, db, _events = deps
        descendant = disp.config.data_dir / "bundles" / "bnd2"
        flow_ir.Workflow(
            name="draft-descendant",
            steps=[
                flow_ir.Step(
                    id="step-1",
                    intent="Wait for the remote application",
                    action=flow_ir.ActionKind.WAIT,
                )
            ],
        ).save(descendant)
        db.insert_bundle("bnd2", str(descendant), capture_id="cap1")
        db.set_first_workflow_state(
            stage="complete",
            capture_id="cap1",
            workflow_id="bnd1",
            target={"backend": "citrix"},
            task="Inspect the remote test record",
        )
        draft = disp._initialize_first_workflow_review(
            workflow_id="bnd2",
            bundle_path=descendant,
            target={"backend": "citrix"},
        )

        replacement = disp.dispatch(
            "initialize_qualification",
            {
                "workflow_id": "bnd2",
                "target_kind": "citrix",
                "application": "Citrix test application",
                "application_version": "2026.1",
                "environment_label": "citrix-test-vda",
            },
        )

        assert draft["project"]["environment"]["target_kind"] == "citrix"
        assert replacement["ok"] is True
        assert replacement["draft_environment"] is False
        assert replacement["project"]["environment"]["target_kind"] == "citrix"

    def test_saved_first_result_cannot_return_to_pre_run_review(
        self, deps
    ) -> None:
        disp, db, _events = deps
        db.set_first_workflow_state(
            stage="result",
            capture_id="cap1",
            workflow_id="bnd1",
            task="Inspect the test page",
        )

        with pytest.raises(ValueError, match="Invalid first workflow stage transition"):
            disp.dispatch(
                "set_first_workflow_stage",
                {"stage": "review", "workflow_id": "bnd1"},
            )

        assert db.get_first_workflow_state()["stage"] == "result"

    def test_post_result_qualification_stage_survives_restart(
        self, deps
    ) -> None:
        disp, db, _events = deps
        db.set_first_workflow_state(
            stage="result",
            capture_id="cap1",
            workflow_id="bnd1",
            target={"backend": "web", "url": "https://example.test"},
            task="Inspect the test page",
        )

        moved = disp.dispatch(
            "set_first_workflow_stage",
            {"stage": "qualification_after_result", "workflow_id": "bnd1"},
        )
        restarted = EngineDispatcher(disp.config, services=disp.services)

        assert moved["state"]["stage"] == "qualification_after_result"
        assert restarted.dispatch("get_first_workflow_state", {})["state"]["stage"] == (
            "qualification_after_result"
        )

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

            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                assert config is None
                order.append("replay")
                (out_dir / "report.json").write_text('{"success": true}')
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

            def replay(self, _bundle, out_dir, **_kwargs):
                self.replay_called = True
                raise AssertionError("replay must not start")

        bridge = Bridge()
        disp.services._flow_bridge = bridge
        result = disp.dispatch("replay_workflow", {"workflow_id": "bnd1"})

        assert result == {
            "ok": False,
            "outcome": "refused",
            "pre_action_refusal": True,
            "error": "Browser setup failed before Flow was invoked",
        }
        assert bridge.replay_called is False
        assert not any(event == "replay_progress" for event, _data in events)

    def test_rdp_replay_skips_browser_and_dispatches_exact_target(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        calls: list[tuple] = []
        staged_configs: list[dict] = []

        class Bridge:
            def ensure_browser_runtime(self, _progress) -> None:
                raise AssertionError("RDP must not provision Chromium")

            def replay(self, target_bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                assert config is not None
                calls.append((target_bundle, out_dir, config))
                staged_configs.append(yaml.safe_load(config.read_text()))
                (out_dir / "report.json").write_text('{"success": true}')
                return FlowResult(
                    ok=True,
                    returncode=0,
                    stdout="target 10.0.0.5 ready at Patient Search",
                    out_dir=out_dir,
                )

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "rdp",
                    "rdp_host": "10.0.0.5",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["workflow_id"] == "bnd1"
        assert len(calls) == 1
        assert staged_configs == [
            {
                "backend": {
                    "kind": "rdp",
                    "rdp_host": "10.0.0.5",
                    "rdp_readiness_text": "Patient Search",
                }
            }
        ]
        assert "10.0.0.5" not in repr(calls)
        assert "Patient Search" not in repr(calls)
        assert not calls[0][2].exists()
        log_lines = [data["line"] for event, data in events if event == "log_line"]
        assert log_lines == ["target [REDACTED] ready at [REDACTED]"]
        assert not any(event == "browser_runtime" for event, _data in events)

    def test_citrix_governed_run_passes_target_and_selected_config(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, _events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        deployment = tmp_path / "deployment.yaml"
        deployment.write_text("backend:\n  kind: citrix\n")
        calls: list[tuple] = []
        staged_configs: list[dict] = []

        class Bridge:
            def run(self, target_bundle, config, out_dir):
                from engine.flow_bridge import FlowResult

                calls.append((target_bundle, config, out_dir))
                staged_configs.append(yaml.safe_load(config.read_text()))
                (out_dir / "report.json").write_text('{"success": true}')
                return FlowResult(ok=True, returncode=0, out_dir=out_dir)

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "run_workflow",
            {
                "workflow_id": "bnd1",
                "deployment_config": str(deployment),
                "target": {
                    "backend": "citrix",
                    "rdp_window_title": "Production EMR",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["workflow_id"] == "bnd1"
        assert len(calls) == 1
        assert calls[0][1] != deployment
        assert staged_configs == [
            {
                "backend": {
                    "kind": "citrix",
                    "rdp_window_title": "Production EMR",
                    "rdp_readiness_text": "Patient Search",
                }
            }
        ]
        assert "Production EMR" not in repr(calls)
        assert "Patient Search" not in repr(calls)
        assert not calls[0][1].exists()

    def test_config_only_rdp_preserves_backend_when_target_is_omitted(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        deployment = tmp_path / "deployment.yaml"
        deployment.write_text(
            "backend:\n  kind: rdp\n  rdp_host: 10.0.0.5\n  rdp_username: operator\n"
        )
        calls: list[tuple] = []
        staged_configs: list[dict] = []

        class Bridge:
            def ensure_browser_runtime(self, _progress) -> None:
                raise AssertionError("config-only RDP must not provision Chromium")

            def replay(self, target_bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                calls.append((target_bundle, out_dir, config))
                staged_configs.append(yaml.safe_load(config.read_text()))
                (out_dir / "report.json").write_text('{"success": true}')
                return FlowResult(ok=True, returncode=0, out_dir=out_dir)

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "deployment_config": str(deployment),
            },
        )

        assert result["workflow_id"] == "bnd1"
        assert staged_configs[0]["backend"]["kind"] == "rdp"
        assert staged_configs[0]["backend"]["rdp_host"] == "10.0.0.5"
        assert "--backend" not in repr(calls)
        assert not any(event == "browser_runtime" for event, _data in events)

    def test_cross_backend_fields_are_refused_before_any_action(self, deps, tmp_path: Path) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        class Bridge:
            def replay(self, *_args, **_kwargs):
                raise AssertionError("invalid target must not execute")

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "citrix",
                    "rdp_host": "not-a-citrix-setting",
                },
            },
        )

        assert result == {
            "ok": False,
            "outcome": "refused",
            "pre_action_refusal": True,
            "error": "Invalid execution target (target)",
        }
        assert not any(event == "replay_progress" for event, _data in events)

    def test_invalid_web_url_is_refused_before_recording_starts(self, deps) -> None:
        disp, _db, events = deps

        with pytest.raises(ValueError, match="Invalid execution target"):
            disp.dispatch(
                "start_recording",
                {
                    "target": {
                        "backend": "web",
                        "url": "javascript:alert(1)",
                    }
                },
            )

        assert disp.services.controller.is_recording is False
        assert any(event == "recording_error" for event, _data in events)

    def test_secret_target_fields_are_rejected_without_echoing_values(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, _events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "rdp",
                    "rdp_host": "10.0.0.5",
                    "rdp_password": "super-secret",
                },
            },
        )

        assert result["ok"] is False
        assert "rdp_password" in result["error"]
        assert "super-secret" not in result["error"]

    def test_nonzero_native_flow_without_report_never_becomes_success(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        class Bridge:
            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                return FlowResult(
                    ok=False,
                    returncode=2,
                    stderr="credential=super-secret",
                    out_dir=out_dir,
                )

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "citrix",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["ok"] is False
        assert "exit 2" in result["error"]
        assert "super-secret" not in result["error"]
        assert any(
            event == "replay_progress" and data["state"] == "unknown" for event, data in events
        )

    @pytest.mark.parametrize(
        ("returncode", "report"),
        [
            (0, {"results": []}),
            (0, {"success": False}),
            (1, {"success": True}),
            (
                0,
                {
                    "success": True,
                    "halt": {"outcome": "halt", "reason": "contradictory"},
                },
            ),
        ],
    )
    def test_ambiguous_reports_never_emit_done(
        self, deps, tmp_path: Path, returncode: int, report: dict
    ) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        class Bridge:
            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                (out_dir / "report.json").write_text(json.dumps(report))
                return FlowResult(
                    ok=returncode == 0,
                    returncode=returncode,
                    out_dir=out_dir,
                )

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "citrix",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["ok"] is False
        assert result["outcome"] == "unknown"
        states = [data["state"] for event, data in events if event == "replay_progress"]
        assert states == ["running", "unknown"]
        assert "done" not in states

    @pytest.mark.parametrize(
        (
            "outcome",
            "production_eligible",
            "execution_completed",
            "progress_state",
        ),
        [
            ("VERIFIED", True, True, "done"),
            ("COMPLETED_UNVERIFIED", False, True, "completed_unverified"),
            ("HALTED", False, False, "halted"),
            ("FAILED", False, False, "failed"),
            ("ROLLED_BACK", False, True, "rolled_back"),
        ],
    )
    def test_precise_outcomes_survive_dispatch(
        self,
        deps,
        tmp_path: Path,
        outcome: str,
        production_eligible: bool,
        execution_completed: bool,
        progress_state: str,
    ) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        class Bridge:
            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                (out_dir / "report.json").write_text(
                    json.dumps(
                        _precise_report(
                            outcome,
                            production_eligible=production_eligible,
                            execution_completed=execution_completed,
                        )
                    )
                )
                returncode = 0 if outcome == "VERIFIED" else 1
                return FlowResult(
                    ok=returncode == 0,
                    returncode=returncode,
                    out_dir=out_dir,
                )

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "citrix",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["outcome"] == outcome
        assert result["ok"] is (outcome == "VERIFIED")
        details = result["outcome_details"]
        assert details["profile"] == "standard"
        assert details["production_eligible"] is production_eligible
        assert details["model_calls"] == 0
        assert details["external_network_calls"] == "none"
        assert details["compensation_actions"] == (
            1 if outcome == "ROLLED_BACK" else 0
        )
        states = [data["state"] for event, data in events if event == "replay_progress"]
        assert states == ["running", progress_state]
        terminal = [data for event, data in events if event == "replay_progress"][-1]
        assert terminal["outcome"] == outcome
        assert terminal["evidence_classes"] == details["evidence_classes"]
        assert terminal["model_calls"] == 0
        assert terminal["external_network_calls"] == "none"
        assert db.list_runs(limit=1)[0]["status"] == outcome
        if outcome == "HALTED":
            assert result["halt"]["reason"]

    def test_run_history_failure_is_explicit_and_retryable(
        self, deps, monkeypatch, tmp_path: Path
    ) -> None:
        disp, db, _events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.set_first_workflow_state(
            stage="review",
            capture_id="cap1",
            workflow_id="first-bnd",
            task="Read one test record",
        )

        class Bridge:
            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                (out_dir / "report.json").write_text(
                    json.dumps(
                        _precise_report(
                            "VERIFIED",
                            production_eligible=True,
                            execution_completed=True,
                        )
                    )
                )
                return FlowResult(ok=True, returncode=0, out_dir=out_dir)

        disp.services._flow_bridge = Bridge()
        insert_run = db.insert_run

        def leave_incomplete_history(
            run_id: str,
            run_path: str,
            *,
            bundle_id: str | None = None,
            status: str = "pending",
        ) -> None:
            insert_run(run_id, run_path, bundle_id=bundle_id, status="pending")
            raise OSError("disk busy")

        monkeypatch.setattr(db, "insert_run", leave_incomplete_history)

        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "citrix",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["outcome"] == "VERIFIED"
        assert result["persistence"]["state"] == "degraded"
        assert result["persistence"]["retryable"] is True
        assert db.list_runs()[0]["status"] == "pending"
        retained = disp.dispatch("get_run_report", {"workflow_id": "bnd1"})
        assert retained["run_id"] == result["run_id"]
        assert retained["persistence"]["state"] == "degraded"
        teach = disp.dispatch(
            "teach_fix",
            {"workflow_id": "bnd1", "run_id": result["run_id"]},
        )
        assert "not saved in local history" in teach["message"]

        monkeypatch.setattr(db, "insert_run", insert_run)
        retry = disp.dispatch(
            "retry_run_persistence",
            {"workflow_id": "bnd1", "run_id": result["run_id"]},
        )

        assert retry["ok"] is True
        assert retry["report"]["persistence"]["state"] == "persisted"
        assert db.list_runs(limit=1)[0]["status"] == "VERIFIED"
        assert db.get_first_workflow_state()["stage"] == "review"
        assert not list((disp.config.data_dir / "runs").glob("*/.desktop-run-persistence.json"))

    def test_first_result_retries_when_the_recovery_marker_cannot_be_written(
        self, deps, monkeypatch
    ) -> None:
        disp, db, _events = deps
        run_dir = disp.config.data_dir / "runs" / "replay-run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text(
            json.dumps(
                _precise_report(
                    "VERIFIED",
                    production_eligible=True,
                    execution_completed=True,
                )
            )
        )
        db.set_first_workflow_state(
            stage="executing",
            capture_id="cap1",
            workflow_id="bnd1",
            task="Read one test record",
        )
        original_write_text = Path.write_text

        def fail_recovery_marker(path: Path, *args, **kwargs):
            if path.name.startswith(".desktop-run-persistence.json"):
                raise OSError("disk busy")
            return original_write_text(path, *args, **kwargs)

        monkeypatch.setattr("engine.dispatch.Path.write_text", fail_recovery_marker)
        persistence = disp._persist_local_run(
            run_id="run-1",
            run_dir=run_dir,
            workflow_id="bnd1",
            outcome="VERIFIED",
            first_supervised=True,
        )

        assert persistence["state"] == "failed"
        assert persistence["retryable"] is True
        monkeypatch.setattr("engine.dispatch.Path.write_text", original_write_text)
        retry = disp.dispatch(
            "retry_run_persistence",
            {"workflow_id": "bnd1", "run_id": "run-1"},
        )
        assert retry["ok"] is True
        assert retry["report"]["persistence"]["state"] == "persisted"
        assert db.get_run("run-1")["status"] == "VERIFIED"
        assert db.get_first_workflow_state()["stage"] == "result"

    def test_legacy_flow_success_remains_ok(self, deps, tmp_path: Path) -> None:
        disp, _db, _events = deps
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "report.json").write_text(json.dumps({"success": True}))

        result = disp._run_report(run_dir, "bnd1", "run-1", outcome="success")

        assert result["outcome"] == "success"
        assert result["ok"] is True

    def test_verified_outcome_rejects_completed_compensation(self) -> None:
        report = _precise_report(
            "VERIFIED",
            production_eligible=True,
            execution_completed=True,
        )
        report["outcome_envelope"]["compensation_actions"] = 1
        report["outcome_envelope"]["evidence_classes"].append("compensation")

        assert FlowBridge.classify_outcome(0, report) == "unknown"

    def test_stored_verified_outcome_is_revalidated_from_retained_report(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, _events = deps
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        report = _precise_report(
            "VERIFIED",
            production_eligible=True,
            execution_completed=True,
        )
        (run_dir / "report.json").write_text(json.dumps(report))
        db.insert_run("run-1", str(run_dir), bundle_id=None)
        db.update_run("run-1", status="VERIFIED")

        report["outcome_envelope"]["compensation_actions"] = 1
        report["outcome_envelope"]["evidence_classes"].append("compensation")
        (run_dir / "report.json").write_text(json.dumps(report))

        result = disp._run_report(
            run_dir,
            workflow_id=None,
            run_id="run-1",
            outcome=db.list_runs(limit=1)[0]["status"],
        )

        assert result["outcome"] == "unknown"
        assert result["ok"] is False
        assert "no longer proves" in result["error"]

    def test_invocation_exception_reports_unknown_effect_state(self, deps, tmp_path: Path) -> None:
        disp, db, events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        class Bridge:
            def replay(self, _bundle, out_dir, *, config):
                raise RuntimeError("Patient Jane Doe secret endpoint")

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "citrix",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["ok"] is False
        assert result["outcome"] == "unknown"
        assert result["pre_action_refusal"] is False
        assert "may have delivered an action" in result["error"]
        assert "Jane Doe" not in result["error"]
        assert any(
            event == "replay_progress" and data["state"] == "unknown" for event, data in events
        )

    def test_nonzero_governed_halt_report_remains_teachable(self, deps, tmp_path: Path) -> None:
        disp, db, _events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")

        class Bridge:
            def replay(self, _bundle, out_dir, *, config):
                from engine.flow_bridge import FlowResult

                (out_dir / "report.json").write_text(
                    json.dumps(
                        {
                            "results": [
                                {
                                    "step_id": "step_000",
                                    "intent": "click 'Submit'",
                                    "ok": False,
                                    "resolution": {"rung": "ocr"},
                                }
                            ],
                            "halt": {
                                "state_id": "step_000",
                                "intent": "click 'Submit'",
                                "reason": "ambiguous target",
                            },
                        }
                    )
                )
                return FlowResult(ok=False, returncode=1, out_dir=out_dir)

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "replay_workflow",
            {
                "workflow_id": "bnd1",
                "target": {
                    "backend": "citrix",
                    "rdp_readiness_text": "Patient Search",
                },
            },
        )

        assert result["ok"] is False
        assert result["outcome"] == "halt"
        assert result["halt"]["reason"] == "ambiguous target"
        assert result["steps"][0]["state"] == "halted"
        assert db.list_runs(limit=10)[0]["bundle_id"] == "bnd1"

    def test_teach_uses_only_the_latest_halted_run(self, deps, tmp_path: Path) -> None:
        from engine.flow_bridge import FlowResult

        disp, db, _events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        old_halt = tmp_path / "run-old-halt"
        latest_halt = tmp_path / "run-latest-halt"
        db.insert_run("run-old", str(old_halt), bundle_id="bnd1", status="HALTED")
        db.insert_run("run-latest", str(latest_halt), bundle_id="bnd1", status="halt")

        class Bridge:
            calls: list[Path] = []

            def teach(self, run_path, _bundle, out_dir, *, fix):
                self.calls.append(run_path)
                assert fix.name.endswith(".json")
                return FlowResult(ok=True, returncode=0, out_dir=out_dir)

        bridge = Bridge()
        disp.services._flow_bridge = bridge

        result = disp.dispatch(
            "teach_fix",
            {
                "workflow_id": "bnd1",
                "run_id": "run-latest",
                "mode": "describe",
                "note": "dismiss the blocking dialog",
            },
        )

        assert result == {"promoted": True, "message": "Fix promoted."}
        assert bridge.calls == [latest_halt]

    def test_teach_never_reuses_an_old_halt_after_a_newer_terminal_run(
        self, deps, tmp_path: Path
    ) -> None:
        disp, db, _events = deps
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        db.insert_run(
            "run-old-halt",
            str(tmp_path / "run-old-halt"),
            bundle_id="bnd1",
            status="HALTED",
        )
        db.insert_run(
            "run-new-verified",
            str(tmp_path / "run-new-verified"),
            bundle_id="bnd1",
            status="VERIFIED",
        )

        class Bridge:
            def teach(self, *_args, **_kwargs):
                raise AssertionError("a stale halt must not be taught")

        disp.services._flow_bridge = Bridge()
        result = disp.dispatch(
            "teach_fix",
            {
                "workflow_id": "bnd1",
                "run_id": "run-new-verified",
                "mode": "describe",
                "note": "dismiss the blocking dialog",
            },
        )

        assert result["promoted"] is False
        assert "latest run ended as VERIFIED" in result["message"]

    def test_recorded_teach_correction_reaches_the_real_flow_cli_bridge(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Protect the Desktop recording -> Flow ``teach --fix`` contract."""

        from engine.controller import RecordingController
        from engine.storage_manager import StorageManager

        config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
        storage = StorageManager(config)
        storage.initialize()
        db = storage.db
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="base-capture")
        run_dir = tmp_path / "run-halted"
        run_dir.mkdir()
        db.insert_run("run-halted", str(run_dir), bundle_id="bnd1", status="HALTED")
        calls: list[list[str]] = []

        def runner(command, **_kwargs):
            calls.append(command)
            return type(
                "Process",
                (),
                {"returncode": 0, "stdout": "promoted", "stderr": ""},
            )()

        monkeypatch.setattr(
            "engine.flow_bridge.shutil.which",
            lambda _name: "/usr/bin/openadapt-flow",
        )
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        bridge = FlowBridge(runner=runner)
        controller = RecordingController(
            captures_dir=config.data_dir / "captures",
            storage_manager=storage,
            flow_bridge=bridge,
            db=db,
            bundles_dir=config.data_dir / "bundles",
        )
        services = EngineServices(
            config,
            db=db,
            storage=storage,
            audit=FakeAudit(),
            controller=controller,
            flow_bridge=bridge,
        )
        disp = EngineDispatcher(config, services=services)

        started = disp.dispatch(
            "start_recording",
            {
                "purpose": "teach_fix",
                "workflow_id": "bnd1",
                "run_id": "run-halted",
            },
        )
        assert started["capture_id"]
        stopped = disp.dispatch("stop_recording", {})
        correction = stopped["correction"]
        assert "compile" not in stopped

        result = disp.dispatch(
            "teach_fix",
            {
                "workflow_id": "bnd1",
                "run_id": "run-halted",
                "mode": "record",
                "fix_capture_id": correction["capture_id"],
                "fix_path": correction["fix_path"],
            },
        )

        assert result == {"promoted": True, "message": "Fix promoted."}
        assert len(calls) == 1
        command = calls[0]
        assert command[1] == "teach"
        assert command[2] == str(run_dir)
        assert command[command.index("--fix") + 1] == correction["fix_path"]
        assert command[command.index("--bundle") + 1] == str(bundle)

        newer_run = tmp_path / "newer-halt"
        newer_run.mkdir()
        db.insert_run("newer-halt", str(newer_run), bundle_id="bnd1", status="HALTED")
        stale = disp.dispatch(
            "teach_fix",
            {
                "workflow_id": "bnd1",
                "run_id": "run-halted",
                "mode": "record",
                "fix_capture_id": correction["capture_id"],
                "fix_path": correction["fix_path"],
            },
        )
        assert stale["promoted"] is False
        assert "stale" in stale["message"]
        assert len(calls) == 1


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
            lambda *a, **k: {
                "success": True,
                "workflow_id": "wf_1",
                "dashboard_url": "u",
                "error": "",
            },
        )
        monkeypatch.setattr(
            "engine.qualification_lifecycle.persist_deployment_handoff",
            lambda *a, **k: disp.config.data_dir / "handoff.json",
        )
        r = disp.dispatch("push_workflow", {"workflow_id": "bnd1"})
        assert r["ok"] is True
        assert r["workflow_id"] == "wf_1"
        assert any(e == "sync_state" for e, _ in events)

    def test_push_workflow_preserves_local_review_handoff(self, deps, monkeypatch) -> None:
        disp, db, events = deps
        db.insert_bundle("bnd1", str(disp.config.data_dir), capture_id="cap1")
        monkeypatch.setattr(
            "engine.hosted.push",
            lambda *a, **k: {
                "success": False,
                "pending_review": True,
                "sanitized_path": "/private/sanitized/artifact",
                "review_command": "openadapt-flow review-sanitized ...",
                "workflow_id": "",
                "error": "",
            },
        )
        monkeypatch.setattr(
            "engine.qualification_lifecycle.persist_deployment_handoff",
            lambda *a, **k: disp.config.data_dir / "handoff.json",
        )

        result = disp.dispatch("push_workflow", {"workflow_id": "bnd1"})

        assert result["ok"] is False
        assert result["pending_review"] is True
        assert result["sanitized_path"] == "/private/sanitized/artifact"
        assert result["review_command"].startswith("openadapt-flow review-sanitized")
        assert ("sync_state", {"state": "paused", "queued": 0}) in events


class TestAuthCommands:
    def test_login_paste(self, deps, monkeypatch, fake_keyring) -> None:
        disp, _db, _e = deps
        cred = {
            "kind": "ingest_token",
            "token": "t",
            "refresh_token": None,
            "org_id": "org_1",
            "host": disp.config.hosted_host,
            "expires_at": None,
        }
        monkeypatch.setattr(
            "engine.auth.paste.PasteTokenProvider.login", lambda self, token=None: cred
        )
        r = disp.dispatch("login_paste", {"token": "t"})
        assert r["authenticated"] is True
        assert r["org_id"] == "org_1"

    def test_custom_origin_login_updates_live_and_persisted_host(
        self, deps, monkeypatch, tmp_path
    ) -> None:
        disp, _db, _e = deps
        host = "https://customer.example"
        toml_path = tmp_path / "config.toml"
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(toml_path))
        cred = {
            "kind": "ingest_token",
            "token": "oai_ingest_customer",
            "refresh_token": None,
            "org_id": "org_customer",
            "host": host,
            "expires_at": None,
        }
        monkeypatch.setattr(
            "engine.auth.paste.PasteTokenProvider.login", lambda self, token=None: cred
        )

        result = disp.dispatch("login_paste", {"host": f"{host}/path", "token": "t"})

        assert result["authenticated"] is True
        assert disp.config.hosted_host == host
        assert f'host = "{host}"' in toml_path.read_text()

    def test_login_refuses_remote_http_before_provider(self, deps, monkeypatch) -> None:
        disp, _db, _e = deps
        called = False

        def _login(self, token=None):
            nonlocal called
            called = True
            raise AssertionError("provider must not receive credentials")

        monkeypatch.setattr("engine.auth.paste.PasteTokenProvider.login", _login)
        result = disp.dispatch(
            "login_paste", {"host": "http://customer.example", "token": "secret"}
        )
        assert result["authenticated"] is False
        assert "HTTPS" in result["error"]
        assert called is False

    def test_get_auth_status_unauthed(self, deps, fake_keyring) -> None:
        disp, _db, _e = deps
        assert disp.dispatch("get_auth_status", {})["authenticated"] is False

    def test_get_auth_status_refuses_credential_for_other_configured_host(
        self, deps, fake_keyring
    ) -> None:
        from engine.auth.provider import Credential
        from engine.auth.store import store_credential

        disp, _db, _e = deps
        credential: Credential = {
            "kind": "ingest_token",
            "token": "oai_ingest_host_bound",
            "refresh_token": None,
            "org_id": "org_42",
            "host": "https://app.openadapt.ai",
            "expires_at": None,
        }
        store_credential(credential)
        disp.config.hosted_host = "https://customer.example"

        result = disp.dispatch("get_auth_status", {})

        assert result == {
            "authenticated": False,
            "host": "https://customer.example",
        }

    def test_connect_uri_forwards_one_exact_string_and_emits_safe_state(
        self, deps, monkeypatch, tmp_path
    ) -> None:
        disp, _db, events = deps
        uri = "openadapt://connect?pairing=oap_" + "A" * 43 + "&host=https%3A%2F%2Fapp.openadapt.ai"
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
        assert (
            "pairing_state",
            {
                "status": "connected",
                "host": "https://app.openadapt.ai",
            },
        ) in events

    def test_connect_uri_returns_the_canonical_authenticated_host(
        self, deps, monkeypatch, tmp_path
    ) -> None:
        disp, _db, _events = deps
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(tmp_path / "config.toml"))
        monkeypatch.setattr(
            "engine.auth.pairing.connect_uri",
            lambda _uri: {
                "authenticated": True,
                "host": "HTTPS://Customer.Example:443/pairing",
                "paired": True,
            },
        )

        result = disp.dispatch("connect_uri", {"uri": "openadapt://connect"})

        assert result["host"] == "https://customer.example"
        assert disp.config.hosted_host == "https://customer.example"

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

    def test_set_config_canonicalizes_host_alias(self, deps, monkeypatch, tmp_path) -> None:
        disp, _db, _e = deps
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(tmp_path / "config.toml"))
        result = disp.dispatch(
            "set_config",
            {"key": "host", "value": "HTTPS://Customer.Example:443/dashboard"},
        )
        assert result["ok"] is True
        assert result["host"] == "https://customer.example"

    def test_set_config_refuses_remote_http(self, deps) -> None:
        disp, _db, _e = deps
        original = disp.config.hosted_host
        result = disp.dispatch(
            "set_config", {"key": "host", "value": "http://customer.example"}
        )
        assert result["ok"] is False
        assert disp.config.hosted_host == original


class TestRunReportMapping:
    """_run_report must map openadapt-flow's real report.json onto RunReport."""

    def _write_report(self, run_dir: Path, report: dict) -> None:
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
        monkeypatch.setattr("engine.dispatch._mac_preflight_accessibility", lambda: False)
        monkeypatch.setattr("engine.dispatch._mac_preflight_input_monitoring", lambda: False)
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
        monkeypatch.setattr("engine.dispatch._mac_preflight_accessibility", lambda: True)
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

    def test_mac_input_monitoring_request_does_not_assume_success(self, deps, monkeypatch) -> None:
        disp, _db, _e = deps
        monkeypatch.setattr("engine.dispatch.sys.platform", "darwin")
        monkeypatch.setattr("engine.dispatch._mac_preflight_screen", lambda: True)
        monkeypatch.setattr("engine.dispatch._mac_preflight_accessibility", lambda: True)
        monkeypatch.setattr("engine.dispatch._mac_preflight_input_monitoring", lambda: False)
        monkeypatch.setattr("engine.dispatch._mac_request_input_monitoring", lambda: True)

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
        monkeypatch.setattr("engine.dispatch._mac_preflight_accessibility", lambda: True)
        monkeypatch.setattr("engine.dispatch._mac_preflight_input_monitoring", lambda: True)
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
            "start_recording",
            "stop_recording",
            "pause_recording",
            "resume_recording",
            "get_status",
            "get_workflows",
            "get_first_workflow_state",
            "set_first_workflow_stage",
            "get_captures",
            "get_storage_usage",
            "compile_recording",
            "replay_workflow",
            "replay_first_workflow",
            "run_workflow",
            "get_run_report",
            "retry_run_persistence",
            "teach_fix",
            "get_qualification",
            "initialize_qualification",
            "set_qualification_risk",
            "arm_qualification_identity",
            "set_qualification_identity",
            "bind_qualification_effect",
            "set_qualification_effect_verification",
            "set_qualification_minimum_effect_tier",
            "add_qualification_case",
            "run_qualification_case",
            "import_qualification_results",
            "certify_qualification",
            "version_qualification_workflow",
            "seal_qualification_workflow",
            "export_qualification_workflow",
            "deploy_qualification_workflow",
            "push_workflow",
            "get_sync_state",
            "pause_sync",
            "resume_sync",
            "get_needs_attention",
            "login_browser",
            "login_paste",
            "logout",
            "get_auth_status",
            "get_config",
            "set_config",
            "check_permissions",
            "request_input_monitoring",
            "scrub_capture",
            "approve_review",
            "dismiss_review",
            "get_pending_reviews",
        }
        assert expected.issubset(set(disp.commands))


class TestScrubCaptureRefusal:
    """A scrub that could not run must not advance the egress gate."""

    def test_unavailable_scrubber_reports_failure_and_holds_state(
        self, deps, tmp_path: Path, monkeypatch,
    ) -> None:
        """Missing Presidio -> ok False, capture stays CAPTURED, no scrubbed copy.

        Before this was fixed the scrubber copied every screenshot through
        unredacted, reported zero redactions, and the capture advanced to
        SCRUBBED -- one operator approval away from leaving the machine.
        """
        import sys

        disp, db, _events = deps
        capture_dir = tmp_path / "cap-enhanced"
        (capture_dir / "screenshots").mkdir(parents=True)
        from PIL import Image

        Image.new("RGB", (8, 8), "white").save(capture_dir / "screenshots" / "0001.png")
        db.insert_capture("capE", str(capture_dir), "2026-07-27T09:00:00Z")

        # The shipped default: openadapt-privacy is installed, Presidio is not.
        monkeypatch.setitem(sys.modules, "openadapt_privacy.providers.presidio", None)

        result = disp.dispatch("scrub_capture", {"capture_id": "capE", "level": "enhanced"})

        assert result["ok"] is False
        assert "unavailable" in result["error"]
        assert "scrubbed_path" not in result
        row = db.get_capture("capE")
        assert row["review_status"] == "captured"
        assert not row["scrubbed_path"]
