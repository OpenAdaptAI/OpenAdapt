"""Shared test fixtures for OpenAdapt Desktop engine tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import EngineConfig


class FakeRecorder:
    """Inert recorder used by unit tests; live capture is contract-tested separately."""

    def __init__(self, capture_dir: str, task_description: str = "") -> None:
        self.capture_dir = capture_dir
        self.task_description = task_description
        self.event_count = 0
        self.is_recording = False

    def __enter__(self):
        self.is_recording = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.is_recording = False

    def wait_for_ready(self, timeout: float = 60) -> bool:
        return self.is_recording

    def stop(self) -> None:
        self.is_recording = False


@pytest.fixture(autouse=True)
def fake_native_recorder(monkeypatch):
    """Prevent unit tests from injecting real mouse/keyboard/screen capture."""

    monkeypatch.setattr("engine.controller._load_capture_recorder", lambda: FakeRecorder)


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory for tests."""
    data_dir = tmp_path / ".openadapt"
    data_dir.mkdir()
    (data_dir / "captures").mkdir()
    (data_dir / "archive").mkdir()
    (data_dir / "tombstones").mkdir()
    return data_dir


@pytest.fixture
def config(tmp_data_dir: Path) -> EngineConfig:
    """Create a test configuration pointing to the temporary data directory."""
    return EngineConfig(
        data_dir=tmp_data_dir,
        storage_mode="air-gapped",
        max_storage_gb=1.0,
        log_level="DEBUG",
    )


@pytest.fixture
def sample_capture_dir(tmp_data_dir: Path) -> Path:
    """Create a sample capture directory with minimal structure."""
    capture_dir = tmp_data_dir / "captures" / "2026-03-02_14-30-00_test123"
    capture_dir.mkdir(parents=True)
    (capture_dir / "video").mkdir()
    (capture_dir / "screenshots").mkdir()

    # Write minimal meta.json
    import json

    meta = {
        "capture_id": "test123",
        "started_at": "2026-03-02T14:30:00Z",
        "platform": "test",
        "screen_size": [1920, 1080],
    }
    (capture_dir / "meta.json").write_text(json.dumps(meta))

    return capture_dir
