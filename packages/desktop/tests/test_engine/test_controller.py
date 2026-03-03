"""Tests for the recording controller."""

from __future__ import annotations

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

    @pytest.mark.skip(reason="Not yet implemented")
    def test_start_creates_capture_directory(self, tmp_data_dir: Path) -> None:
        """Starting a recording should create a capture directory."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        capture_id = controller.start()
        assert capture_id is not None
        assert controller.state == RecordingState.RECORDING
        assert controller.is_recording

    @pytest.mark.skip(reason="Not yet implemented")
    def test_stop_returns_metadata(self, tmp_data_dir: Path) -> None:
        """Stopping a recording should return capture metadata."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        controller.start()
        metadata = controller.stop()
        assert "id" in metadata
        assert "duration" in metadata
        assert controller.state == RecordingState.IDLE

    @pytest.mark.skip(reason="Not yet implemented")
    def test_cannot_start_while_recording(self, tmp_data_dir: Path) -> None:
        """Starting a second recording should raise an error."""
        controller = RecordingController(captures_dir=tmp_data_dir / "captures")
        controller.start()
        with pytest.raises(RuntimeError):
            controller.start()
