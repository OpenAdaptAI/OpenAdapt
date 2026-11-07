"""Invalid serve modes need no optional transport; valid modes still delegate."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from openadapt_flow.agent.cli import main


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--teach-browser", "--url", "http://127.0.0.1:1"],
        ["serve", "--teach-browser", "--bundles", "x"],
        ["serve", "--tools-dir", "x"],
    ],
)
def test_invalid_modes_never_attempt_optional_transport_import(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, argv: list[str]
) -> None:
    original = builtins.__import__
    attempts: list[str] = []

    def without_mcp(name: str, *args: Any, **kwargs: Any) -> Any:
        if (
            name == "openadapt_flow.agent.mcp"
            or name == "mcp"
            or name.startswith("mcp.")
        ):
            attempts.append(name)
            raise ModuleNotFoundError("synthetic optional transport unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mcp)
    assert main(argv) == 2
    assert "--teach-browser" in capsys.readouterr().err
    assert attempts == []


@pytest.fixture()
def browser_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple:
    events: list[str] = []
    instances: list[Any] = []

    class BrowserBridge:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.programs = {}
            self.tools_dir = tmp_path / "tools"
            self.files_dir = tmp_path / "files"
            instances.append(self)
            events.append("bridge")

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(
        "openadapt_flow.agent.browser_teach.BrowserTeachBridge", BrowserBridge
    )
    return events, instances


def _serve_args(tmp_path: Path) -> list[str]:
    return [
        "serve",
        "--teach-browser",
        "--tools-dir",
        str(tmp_path / "tools"),
        "--files-dir",
        str(tmp_path / "files"),
    ]


def test_valid_mode_delegates_exact_bridges_to_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browser_bridge: tuple
) -> None:
    events, instances = browser_bridge
    calls: list[tuple] = []
    transport = ModuleType("openadapt_flow.agent.mcp")

    def serve(*bridges: Any, **options: Any) -> None:
        events.append("transport")
        calls.append((bridges, options))

    transport.serve = serve
    monkeypatch.setitem(sys.modules, transport.__name__, transport)
    assert main(_serve_args(tmp_path)) == 0
    assert calls == [((None, None, None, instances[0]), {})]
    assert events == ["bridge", "transport", "closed"]


def test_valid_mode_preserves_missing_transport_failure_and_closes_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browser_bridge: tuple
) -> None:
    events, _instances = browser_bridge
    original = builtins.__import__

    def without_mcp(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "openadapt_flow.agent.mcp":
            events.append("import")
            raise ModuleNotFoundError("synthetic optional transport unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mcp)
    with pytest.raises(
        ModuleNotFoundError, match="synthetic optional transport unavailable"
    ):
        main(_serve_args(tmp_path))
    assert events == ["bridge", "import", "closed"]


def test_valid_mode_preserves_transport_error_and_closes_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browser_bridge: tuple
) -> None:
    events, instances = browser_bridge
    transport = ModuleType("openadapt_flow.agent.mcp")

    def serve(*bridges: Any, **options: Any) -> None:
        assert bridges == (None, None, None, instances[0])
        assert options == {}
        events.append("transport")
        raise RuntimeError("synthetic transport failure")

    transport.serve = serve
    monkeypatch.setitem(sys.modules, transport.__name__, transport)
    with pytest.raises(RuntimeError, match="synthetic transport failure"):
        main(_serve_args(tmp_path))
    assert events == ["bridge", "transport", "closed"]
