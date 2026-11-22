"""The launcher's `openadapt plan` and `openadapt proxy` pass their arguments
through, and `proxy` keeps the agent's command after `--`."""

from __future__ import annotations

import sys

from click.testing import CliRunner

from openadapt import cli


def _capture(monkeypatch, calls):
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)

    def load(_ctx, modules):
        calls.append(("modules", modules))
        return lambda argv: calls.append(("argv", argv)) or 0

    monkeypatch.setattr(cli, "_optional_engine_entrypoint", load)


def test_plan_passes_its_arguments_through(monkeypatch):
    calls: list = []
    _capture(monkeypatch, calls)
    result = CliRunner().invoke(cli.main, ["plan", "apply", "--tx", "t.yaml", "abc123"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ("modules", ("openadapt_flow.txplan.cli",)),
        ("argv", ["apply", "--tx", "t.yaml", "abc123"]),
    ]


def test_proxy_keeps_the_agent_command_after_the_separator(monkeypatch):
    calls: list = []
    _capture(monkeypatch, calls)
    argv = ["proxy", "--sqlite", "app.db", "--", "claude", "--model", "x"]
    monkeypatch.setattr(sys, "argv", ["openadapt", *argv])
    result = CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("argv", ["--sqlite", "app.db", "--", "claude", "--model", "x"])
