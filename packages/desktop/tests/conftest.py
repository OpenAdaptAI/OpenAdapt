"""Shared test fixtures for OpenAdapt Desktop engine tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import EngineConfig


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
