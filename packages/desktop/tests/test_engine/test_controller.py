"""Tests for the recording controller."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from engine import controller as controller_module
from engine.controller import RecordingController, RecordingState

REAL_LOAD_CAPTURE_RECORDER = controller_module._load_capture_recorder  # noqa: SLF001


def test_frozen_capture_provisions_vision_before_recorder_import(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(controller_module.sys, "frozen", True, raising=False)
    managed = ModuleType("engine.managed_vision")
    capture = ModuleType("openadapt_capture")

    class Recorder:
        pass

    def provision() -> None:
        calls.append("provision")

    managed.ensure_managed_vision_runtime = provision  # type: ignore[attr-defined]
    capture.Recorder = Recorder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.managed_vision", managed)
    monkeypatch.setitem(sys.modules, "openadapt_capture", capture)

    assert REAL_LOAD_CAPTURE_RECORDER() is Recorder
    assert calls == ["provision"]


class TestRecordingController:
    """Tests for RecordingController lifecycle management."""

    def test_initial_state_is_idle(self, tmp_data_dir: Path) -> None:
        """Controller should start in IDLE state."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        assert controller.state == RecordingState.IDLE
        assert not controller.is_recording
        assert controller.current_capture_id is None

    def test_start_creates_capture_directory(self, tmp_data_dir: Path) -> None:
        """Starting a recording should create a capture directory."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        capture_id = controller.start()
        assert capture_id is not None
        assert controller.state == RecordingState.RECORDING
        assert controller.is_recording

        # Verify directory was created
        dirs = list((tmp_data_dir / "captures").iterdir())
        assert len(dirs) == 1
        assert (dirs[0] / "meta.json").exists()
        assert (dirs[0] / "state.json").exists()

    def test_start_uses_capture_runtime_contract(self, tmp_data_dir: Path, monkeypatch) -> None:
        """Desktop must use Capture's real context-manager constructor contract."""

        calls: dict[str, object] = {}

        class ContractRecorder:
            event_count = 0
            is_recording = False

            def __init__(self, capture_dir: str, task_description: str = "") -> None:
                calls["capture_dir"] = capture_dir
                calls["task_description"] = task_description

            def __enter__(self):
                self.is_recording = True
                calls["entered"] = True
                return self

            def __exit__(self, exc_type, exc_val, exc_tb) -> None:
                self.is_recording = False
                calls["exited"] = True

            def wait_for_ready(self, timeout: float = 60) -> bool:
                calls["timeout"] = timeout
                return True

            def stop(self) -> None:
                calls["stopped"] = True

        monkeypatch.setattr("engine.controller._load_capture_recorder", lambda: ContractRecorder)
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")

        controller.start(task_description="Claims intake")
        controller.stop()

        assert Path(str(calls["capture_dir"])).parent == tmp_data_dir / "captures"
        assert calls["task_description"] == "Claims intake"
        assert calls["entered"] is True
        assert calls["timeout"] == 60
        assert calls["stopped"] is True
        assert calls["exited"] is True

    def test_start_failure_is_explicit_and_not_recording(
        self, tmp_data_dir: Path, monkeypatch
    ) -> None:
        """A missing native recorder must never become a metadata-only success."""

        def unavailable():
            raise RuntimeError("native recorder unavailable")

        monkeypatch.setattr("engine.controller._load_capture_recorder", unavailable)
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")

        with pytest.raises(RuntimeError, match="Could not start native recording"):
            controller.start()

        assert controller.state == RecordingState.IDLE
        assert not controller.is_recording
        assert controller.current_capture_id is None
        capture_dir = next((tmp_data_dir / "captures").iterdir())
        state = json.loads((capture_dir / "state.json").read_text())
        assert state["status"] == "failed"
        assert "native recorder unavailable" in state["error"]

    def test_stop_failure_is_explicit_and_not_completed(
        self, tmp_data_dir: Path, monkeypatch
    ) -> None:
        """A recorder shutdown failure must not produce completed metadata."""

        class BrokenStopRecorder:
            event_count = 0
            is_recording = False

            def __init__(self, capture_dir: str, task_description: str = "") -> None:
                pass

            def __enter__(self):
                self.is_recording = True
                return self

            def __exit__(self, exc_type, exc_val, exc_tb) -> None:
                self.is_recording = False

            def wait_for_ready(self, timeout: float = 60) -> bool:
                return True

            def stop(self) -> None:
                raise RuntimeError("writer did not stop")

        monkeypatch.setattr("engine.controller._load_capture_recorder", lambda: BrokenStopRecorder)
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        controller.start()

        with pytest.raises(RuntimeError, match="Could not stop native recording cleanly"):
            controller.stop()

        assert controller.state == RecordingState.IDLE
        capture_dir = next((tmp_data_dir / "captures").iterdir())
        state = json.loads((capture_dir / "state.json").read_text())
        assert state["status"] == "failed"
        assert "writer did not stop" in state["error"]

    def test_registration_failure_stops_recorder_and_rolls_back_state(
        self, tmp_data_dir: Path, monkeypatch
    ) -> None:
        """Startup remains transactional through local index registration."""

        calls: dict[str, bool] = {}

        class TrackedRecorder:
            event_count = 0
            is_recording = False

            def __init__(self, capture_dir: str, task_description: str = "") -> None:
                pass

            def __enter__(self):
                self.is_recording = True
                return self

            def __exit__(self, exc_type, exc_val, exc_tb) -> None:
                calls["exited"] = True
                self.is_recording = False

            def wait_for_ready(self, timeout: float = 60) -> bool:
                return True

            def stop(self) -> None:
                calls["stopped"] = True
                self.is_recording = False

        class FailingStorageManager:
            def register_capture(self, capture_id: str, capture_dir: Path) -> None:
                raise RuntimeError("index unavailable")

        monkeypatch.setattr("engine.controller._load_capture_recorder", lambda: TrackedRecorder)
        controller = RecordingController(
            captures_dir=tmp_data_dir / "captures",
            storage_manager=FailingStorageManager(),
        )

        with pytest.raises(RuntimeError, match="Could not start native recording"):
            controller.start()

        assert calls == {"stopped": True, "exited": True}
        assert controller.state == RecordingState.IDLE
        assert controller.current_capture_id is None
        capture_dir = next((tmp_data_dir / "captures").iterdir())
        state = json.loads((capture_dir / "state.json").read_text())
        assert state["status"] == "failed"
        assert "index unavailable" in state["error"]

    def test_finalization_failure_resets_controller_and_is_not_completed(
        self, tmp_data_dir: Path
    ) -> None:
        """A local-index failure cannot wedge or falsely complete a stopped run."""

        class FailingDB:
            def update_capture(self, capture_id: str, **fields: object) -> None:
                raise RuntimeError("index update failed")

        class StorageManager:
            db = FailingDB()

            def register_capture(self, capture_id: str, capture_dir: Path) -> None:
                pass

        controller = RecordingController(
            captures_dir=tmp_data_dir / "captures",
            storage_manager=StorageManager(),
        )
        controller.start()

        with pytest.raises(RuntimeError, match="Could not finalize native recording"):
            controller.stop()

        assert controller.state == RecordingState.IDLE
        assert controller.current_capture_id is None
        capture_dir = next((tmp_data_dir / "captures").iterdir())
        state = json.loads((capture_dir / "state.json").read_text())
        assert state["status"] == "failed"
        assert "index update failed" in state["error"]

    def test_stop_returns_metadata(self, tmp_data_dir: Path) -> None:
        """Stopping a recording should return capture metadata."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        controller.start()
        metadata = controller.stop()
        assert "id" in metadata
        assert "duration" in metadata
        assert "event_count" in metadata
        assert "size_bytes" in metadata
        assert "path" in metadata
        assert controller.state == RecordingState.IDLE

    def test_cannot_start_while_recording(self, tmp_data_dir: Path) -> None:
        """Starting a second recording should raise an error."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        controller.start()
        with pytest.raises(RuntimeError, match="Cannot start"):
            controller.start()

    def test_cannot_stop_when_idle(self, tmp_data_dir: Path) -> None:
        """Stopping when idle should raise."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        with pytest.raises(RuntimeError, match="No recording"):
            controller.stop()

    def test_state_json_written_on_start(self, tmp_data_dir: Path) -> None:
        """state.json should be written with 'recording' status."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        capture_id = controller.start()

        dirs = list((tmp_data_dir / "captures").iterdir())
        state = json.loads((dirs[0] / "state.json").read_text())
        assert state["status"] == "recording"
        assert state["capture_id"] == capture_id

    def test_state_json_updated_on_stop(self, tmp_data_dir: Path) -> None:
        """state.json should be updated to 'completed' on stop."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        controller.start()
        metadata = controller.stop()

        state = json.loads((Path(metadata["path"]) / "state.json").read_text())
        assert state["status"] == "completed"

    def test_recover_finds_incomplete_sessions(self, tmp_data_dir: Path) -> None:
        """Recover should find sessions with state 'recording'."""
        cap_dir = tmp_data_dir / "captures" / "2026-03-02_10-00-00_abc12345"
        cap_dir.mkdir(parents=True)
        (cap_dir / "meta.json").write_text(
            json.dumps({"capture_id": "abc12345", "started_at": "2026-03-02T10:00:00Z"})
        )
        (cap_dir / "state.json").write_text(
            json.dumps({"status": "recording", "capture_id": "abc12345"})
        )

        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        recovered = controller.recover()
        assert "abc12345" in recovered

        state = json.loads((cap_dir / "state.json").read_text())
        assert state["status"] == "recovered"

    def test_pause_raises_not_implemented(self, tmp_data_dir: Path) -> None:
        """Pause should explain the supported lifecycle alternative."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        with pytest.raises(
            NotImplementedError,
            match="^Pause is not supported; use stop/start instead$",
        ):
            controller.pause()

    def test_resume_raises_not_implemented(self, tmp_data_dir: Path) -> None:
        """Resume should explain the supported lifecycle alternative."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        with pytest.raises(
            NotImplementedError,
            match="^Resume is not supported; use stop/start instead$",
        ):
            controller.resume()


class TestPostStopCompile:
    """The post-stop loop step: compile the recording into a flow bundle."""

    def test_auto_compile_records_bundle(self, tmp_data_dir: Path) -> None:
        from unittest.mock import MagicMock

        from engine.db import IndexDB
        from engine.flow_bridge import FlowResult

        db = IndexDB(tmp_data_dir / "index.db")
        db.initialize()

        bridge = MagicMock()
        bridge.compile.return_value = FlowResult(ok=True, returncode=0)

        controller = RecordingController(
            captures_dir=tmp_data_dir / "captures",
            flow_bridge=bridge,
            db=db,
            bundles_dir=tmp_data_dir / "bundles",
            auto_compile=True,
        )
        controller.start()
        metadata = controller.stop()

        assert "bundle_id" in metadata
        bridge.compile.assert_called_once()
        assert db.get_bundle(metadata["bundle_id"]) is not None
        db.close()

    def test_no_compile_without_flag(self, tmp_data_dir: Path) -> None:
        from unittest.mock import MagicMock

        bridge = MagicMock()
        controller = RecordingController(
            captures_dir=tmp_data_dir / "captures",
            flow_bridge=bridge,
            auto_compile=False,
        )
        controller.start()
        metadata = controller.stop()
        assert "bundle_id" not in metadata
        bridge.compile.assert_not_called()
