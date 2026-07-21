"""Tests for the openadapt-flow CLI wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.flow_bridge import (
    EMBEDDED_FLOW_MODE,
    BrowserRuntimeError,
    FlowBridge,
    flow_available,
)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(recorder, returncode=0, stdout="", stderr=""):
    def run(cmd, capture_output=True, text=True, timeout=None, env=None):
        recorder.append((cmd, env))
        return FakeProc(returncode, stdout, stderr)

    return run


class TestFlowBridgeInvocation:
    def test_compile_builds_args(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="ok"))
        result = bridge.compile(tmp_path / "rec", tmp_path / "bundle")
        assert result.ok
        command, env = calls[0]
        assert command[1] == "compile"
        assert "--out" in command
        assert env is not None
        # openadapt-flow compile requires --name; default it to the bundle name.
        assert "--name" in command
        assert command[command.index("--name") + 1] == "bundle"

    def test_run_builds_args(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        bridge.run(tmp_path / "bundle", tmp_path / "cfg.yaml", out_dir=tmp_path / "run")
        command, _ = calls[0]
        assert command[1] == "run"
        assert "--config" in command
        # The run directory is passed via --run-dir (not --out).
        assert "--run-dir" in command

    def test_replay_uses_run_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        bridge.replay(tmp_path / "bundle", out_dir=tmp_path / "run")
        command, _ = calls[0]
        assert command[1] == "replay"
        assert "--run-dir" in command
        assert "--out" not in command

    def test_nonzero_returncode(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        bridge = FlowBridge(runner=_runner([], returncode=1, stderr="boom"))
        result = bridge.replay(tmp_path / "bundle")
        assert not result.ok
        assert result.stderr == "boom"

    def test_frozen_runtime_uses_own_executable_not_path(self, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge._is_frozen", lambda: True)
        monkeypatch.setattr("engine.flow_bridge.sys.executable", "/signed/openadapt-engine")
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/tmp/shadowed")
        calls: list = []

        FlowBridge(runner=_runner(calls)).replay(Path("bundle"))

        command, env = calls[0]
        assert command[:3] == [
            "/signed/openadapt-engine",
            EMBEDDED_FLOW_MODE,
            "replay",
        ]
        assert env is not None
        assert flow_available()

    def test_optional_commands_use_same_bundled_runtime(self, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge._is_frozen", lambda: True)
        monkeypatch.setattr("engine.flow_bridge.sys.executable", "/signed/openadapt-engine")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="wf_123"))

        assert bridge.supports_command("push")
        result = bridge.push(
            Path("bundle"),
            kind="bundle",
            host="https://app.openadapt.ai",
        )

        assert result.ok
        commands = [command for command, _env in calls]
        assert commands[0] == [
            "/signed/openadapt-engine",
            EMBEDDED_FLOW_MODE,
            "push",
            "--help",
        ]
        assert commands[1][:3] == [
            "/signed/openadapt-engine",
            EMBEDDED_FLOW_MODE,
            "push",
        ]


class TestReportParsing:
    def test_read_report_missing(self, tmp_path: Path) -> None:
        assert FlowBridge.read_report(tmp_path) == {}

    def test_read_halt_nested(self, tmp_path: Path) -> None:
        (tmp_path / "report.json").write_text(
            json.dumps({"halt": {"reason": "ambiguous", "step_intent": "click X"}})
        )
        halt = FlowBridge.read_halt(tmp_path)
        assert halt is not None
        assert halt["reason"] == "ambiguous"

    def test_read_halt_top_level_status(self, tmp_path: Path) -> None:
        (tmp_path / "report.json").write_text(json.dumps({"status": "halt", "reason": "drift"}))
        halt = FlowBridge.read_halt(tmp_path)
        assert halt is not None
        assert halt["reason"] == "drift"

    def test_read_halt_none_when_ok(self, tmp_path: Path) -> None:
        (tmp_path / "report.json").write_text(json.dumps({"status": "ok"}))
        assert FlowBridge.read_halt(tmp_path) is None


class TestBrowserRuntime:
    def test_provisions_once_and_reports_progress(self, monkeypatch) -> None:
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        presence = iter((False, True))
        monkeypatch.setattr(bridge, "browser_runtime_present", lambda: next(presence))
        states: list[tuple[str, str]] = []

        bridge.ensure_browser_runtime(lambda state, detail: states.append((state, detail)))

        command, _ = calls[0]
        assert command[1:] == ["-m", "playwright", "install", "chromium"]
        assert [state for state, _ in states] == ["checking", "installing", "ready"]

    def test_existing_offline_prebundle_skips_install(self, monkeypatch) -> None:
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        monkeypatch.setattr(bridge, "browser_runtime_present", lambda: True)
        states: list[str] = []

        bridge.ensure_browser_runtime(lambda state, _detail: states.append(state))

        assert calls == []
        assert states == ["checking", "ready"]

    def test_install_failure_is_explicit_and_retryable(self, monkeypatch) -> None:
        bridge = FlowBridge(runner=_runner([], returncode=1, stderr="network refused"))
        monkeypatch.setattr(bridge, "browser_runtime_present", lambda: False)
        states: list[str] = []

        with pytest.raises(BrowserRuntimeError, match="select Replay to retry"):
            bridge.ensure_browser_runtime(lambda state, _detail: states.append(state))

        assert states == ["checking", "installing", "error"]
