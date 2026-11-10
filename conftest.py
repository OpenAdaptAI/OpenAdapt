"""Repository-wide local browser launch guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if config.pluginmanager.hasplugin("openadapt-headless"):
        return
    path = Path(__file__).parent / "packages/flow/tests/headless_guard.py"
    spec = importlib.util.spec_from_file_location("_openadapt_headless_guard", path)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    config.pluginmanager.register(plugin, "openadapt-headless")
