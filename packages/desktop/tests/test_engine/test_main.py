"""Tests for the engine entry point."""

from __future__ import annotations

from engine import __version__
from engine import main as engine_main


def test_startup_log_uses_canonical_engine_version() -> None:
    """The startup log must not drift from the package version."""
    assert engine_main.ENGINE_VERSION == __version__
