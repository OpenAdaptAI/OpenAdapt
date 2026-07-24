"""Tests for the engine entry point."""

from __future__ import annotations

import sys
from types import ModuleType

from engine import __version__
from engine import main as engine_main


def test_startup_log_uses_canonical_engine_version() -> None:
    """The startup log must not drift from the package version."""
    assert engine_main.ENGINE_VERSION == __version__


def test_frozen_browser_cache_is_stable_and_overrideable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(engine_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(engine_main.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    engine_main._configure_frozen_browser_cache()

    assert engine_main.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(
        tmp_path / ".openadapt" / "browser-runtime"
    )
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/approved/offline")
    engine_main._configure_frozen_browser_cache()
    assert engine_main.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/approved/offline"


def test_normal_frozen_startup_configures_browser_cache_first(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        engine_main,
        "_configure_frozen_browser_cache",
        lambda: calls.append("cache"),
    )
    monkeypatch.setattr(engine_main.sys, "argv", ["engine", "--help"])

    class FakeCli:
        @staticmethod
        def main() -> None:
            calls.append("cli")

    monkeypatch.setitem(sys.modules, "engine.cli", FakeCli)

    engine_main.main()

    assert calls == ["cache", "cli"]


def test_embedded_playwright_strips_python_module_argv(monkeypatch) -> None:
    observed: list[list[str]] = []
    monkeypatch.setattr(engine_main, "_configure_frozen_browser_cache", lambda: None)

    class FakeModule:
        @staticmethod
        def main() -> None:
            observed.append(list(sys.argv))

    monkeypatch.setitem(sys.modules, "playwright.__main__", FakeModule)
    monkeypatch.setattr(sys, "argv", ["engine", "-m", "playwright", "install", "chromium"])

    engine_main._run_embedded_playwright()

    assert observed == [["engine", "install", "chromium"]]


def test_embedded_flow_version_stays_offline(monkeypatch, capsys) -> None:
    managed = ModuleType("engine.managed_vision")

    def unexpected_provision() -> None:
        raise AssertionError("version discovery must not provision optional vision")

    managed.ensure_managed_vision_runtime = unexpected_provision  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.managed_vision", managed)
    monkeypatch.setattr(engine_main, "_configure_frozen_browser_cache", lambda: None)
    monkeypatch.setattr(engine_main, "_normalize_flow_auto_scrub_capability", lambda: None)
    monkeypatch.setattr(engine_main, "_embedded_flow_version", lambda: "1.20.1")
    monkeypatch.setattr(sys, "argv", ["engine", "__openadapt_flow__", "--version"])

    engine_main._run_embedded_flow()

    assert capsys.readouterr().out == "openadapt-flow 1.20.1\n"


def test_embedded_flow_command_provisions_vision_before_import(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(engine_main, "_configure_frozen_browser_cache", lambda: None)
    monkeypatch.setattr(engine_main, "_normalize_flow_auto_scrub_capability", lambda: None)
    monkeypatch.setattr(engine_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["engine", "__openadapt_flow__", "lint", "bundle"],
    )

    managed = ModuleType("engine.managed_vision")

    def provision() -> None:
        calls.append("provision")

    managed.ensure_managed_vision_runtime = provision  # type: ignore[attr-defined]
    flow = ModuleType("openadapt_flow.__main__")

    def flow_main() -> None:
        calls.append("flow")

    flow.main = flow_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.managed_vision", managed)
    monkeypatch.setitem(sys.modules, "openadapt_flow.__main__", flow)

    engine_main._run_embedded_flow()

    assert calls == ["provision", "flow"]
    assert sys.argv == ["engine", "lint", "bundle"]


def test_incomplete_auto_scrubber_matches_flow_auto_fallback(monkeypatch) -> None:
    monkeypatch.delenv("OPENADAPT_FLOW_SCRUB", raising=False)
    monkeypatch.setattr(
        engine_main,
        "find_spec",
        lambda module: None if module == "presidio_analyzer" else object(),
    )

    engine_main._normalize_flow_auto_scrub_capability()

    assert engine_main.os.environ["OPENADAPT_FLOW_SCRUB"] == "off"


def test_explicit_regulated_scrub_is_never_weakened(monkeypatch) -> None:
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    monkeypatch.setattr(engine_main, "find_spec", lambda _module: None)

    engine_main._normalize_flow_auto_scrub_capability()

    assert engine_main.os.environ["OPENADAPT_FLOW_SCRUB"] == "on"
