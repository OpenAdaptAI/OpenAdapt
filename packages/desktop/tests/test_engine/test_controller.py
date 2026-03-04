"""Tests for the recording controller."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.controller import RecordingController, RecordingState


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
        """Pause should raise NotImplementedError for v0.1.0."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        with pytest.raises(NotImplementedError, match="Pause not supported"):
            controller.pause()
