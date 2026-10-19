"""Fixtures for e2e tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def src_dir() -> Path:
    """Path to the HTML source directory."""
    return Path(__file__).parent.parent.parent / "src"


@pytest.fixture
def project_root() -> Path:
    """Path to the project root directory."""
    return Path(__file__).parent.parent.parent
