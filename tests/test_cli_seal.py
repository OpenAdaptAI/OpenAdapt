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
    monkeypatch.setitem(sys.modules, "openadapt_agent.seal_cli", types.ModuleType("openadapt_agent.seal_cli"))
    result = CliRunner().invoke(main, ["seal", "verify", "report.json", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == [["seal", "verify", "report.json", "--json"]]


def _old_agent(monkeypatch):
    # openadapt-agent 2.2+ is an alias of openadapt_flow.agent. Stand in for an
    # agent without Seal checks under both names, and drop the submodule
    # attribute an earlier import left on the (shared) package.
    for name in ("openadapt_agent.seal_cli", "openadapt_flow.agent.seal_cli"):
        monkeypatch.setitem(sys.modules, name, None)
    for package in ("openadapt_agent", "openadapt_flow.agent"):
        module = sys.modules.get(package)
        if module is not None:
            monkeypatch.delattr(module, "seal_cli", raising=False)


def test_seal_explains_an_old_agent(monkeypatch):
    _old_agent(monkeypatch)
    result = CliRunner().invoke(main, ["seal", "verify", "x.json"])
    assert result.exit_code == 1
    assert "pip install --upgrade openadapt-agent" in result.output


def test_seal_help_works_with_an_old_agent(monkeypatch):
    _old_agent(monkeypatch)
    result = CliRunner().invoke(main, ["seal", "--help"])
    assert result.exit_code == 0
    assert "openadapt seal verify" in result.output
