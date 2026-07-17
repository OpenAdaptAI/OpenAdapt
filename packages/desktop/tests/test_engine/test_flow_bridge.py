"""Tests for the openadapt-flow CLI wrapper."""

from __future__ import annotations

import json
from pathlib import Path

from engine.flow_bridge import FlowBridge


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(recorder, returncode=0, stdout="", stderr=""):
    def run(cmd, capture_output=True, text=True, timeout=None):
        recorder.append(cmd)
        return FakeProc(returncode, stdout, stderr)
    return run


class TestFlowBridgeInvocation:
    def test_compile_builds_args(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls, stdout="ok"))
        result = bridge.compile(tmp_path / "rec", tmp_path / "bundle")
        assert result.ok
        assert calls[0][1] == "compile"
        assert "--out" in calls[0]

    def test_run_builds_args(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        calls: list = []
        bridge = FlowBridge(runner=_runner(calls))
        bridge.run(tmp_path / "bundle", tmp_path / "cfg.yaml", out_dir=tmp_path / "run")
        assert calls[0][1] == "run"
        assert "--config" in calls[0]

    def test_nonzero_returncode(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("engine.flow_bridge.shutil.which", lambda _: "/usr/bin/openadapt-flow")
        bridge = FlowBridge(runner=_runner([], returncode=1, stderr="boom"))
        result = bridge.replay(tmp_path / "bundle")
        assert not result.ok
        assert result.stderr == "boom"


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
