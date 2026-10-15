"""`openadapt seal ...` forwards to `openadapt-agent seal ...`."""

from __future__ import annotations

import sys
import types

from click.testing import CliRunner

from openadapt.cli import main


def test_seal_forwards_to_agent(monkeypatch):
    calls = []
    cli_mod = types.ModuleType("openadapt_agent.cli")
    cli_mod.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "openadapt_agent.cli", cli_mod)
    monkeypatch.setitem(
        sys.modules,
        "openadapt_agent.seal_cli",
        types.ModuleType("openadapt_agent.seal_cli"),
    )
    result = CliRunner().invoke(main, ["seal", "verify", "report.json", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == [["seal", "verify", "report.json", "--json"]]


def test_seal_explains_an_old_agent(monkeypatch):
    monkeypatch.setitem(sys.modules, "openadapt_agent.seal_cli", None)
    result = CliRunner().invoke(main, ["seal", "verify", "x.json"])
    assert result.exit_code == 1
    assert "pip install --upgrade openadapt-agent" in result.output


def test_seal_help_works_with_an_old_agent(monkeypatch):
    monkeypatch.setitem(sys.modules, "openadapt_agent.seal_cli", None)
    result = CliRunner().invoke(main, ["seal", "--help"])
    assert result.exit_code == 0
    assert "openadapt seal verify" in result.output
