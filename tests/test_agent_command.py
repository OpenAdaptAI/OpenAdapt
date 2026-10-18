"""Every agent command the launcher prints works for every install method.

`uv tool install openadapt` (what https://openadapt.ai/install.sh runs) and
pipx put only the launcher's own `openadapt` executable on PATH. The
`openadapt-agent` executable stays inside the tool environment, so a printed
`openadapt-agent serve ...` fails there with "command not found" (exit 127).
`openadapt agent ...` forwards to the same entry point and works everywhere.
"""

from __future__ import annotations

import re
import sys
import types

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.cli import main as cli_main

# A printed `openadapt-agent <verb>` command. Package names such as
# `openadapt-agent>=2.0.1` or `pip install openadapt-agent` don't match.
_ENGINE_AGENT_COMMAND = re.compile(r"(?<![\w./\[-])openadapt-agent (?=[a-z])")


@pytest.fixture()
def supported_python(monkeypatch):
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: cli._SUPPORTED_PYTHON[1])


@pytest.fixture()
def fake_agent(monkeypatch):
    """Install a stand-in `openadapt_agent.cli` that records its argv."""
    calls: list[list[str]] = []
    package = types.ModuleType("openadapt_agent")
    module = types.ModuleType("openadapt_agent.cli")

    def main(argv=None):
        calls.append(list(argv))
        return 3

    module.main = main
    package.cli = module
    monkeypatch.setitem(sys.modules, "openadapt_agent", package)
    monkeypatch.setitem(sys.modules, "openadapt_agent.cli", module)
    return calls


def test_top_level_help_prints_the_launcher_agent_command():
    result = CliRunner().invoke(cli_main, ["--help"])

    assert result.exit_code == 0, result.output
    assert "openadapt agent serve --bundles DIR --mode production" in result.output
    assert not _ENGINE_AGENT_COMMAND.search(result.output), result.output


def test_quickstart_prints_the_launcher_agent_command(monkeypatch, supported_python):
    monkeypatch.setattr("openadapt.cli._invoke_flow", lambda _argv: 0)
    monkeypatch.setattr("openadapt.cli._agent_serves_tutorial", lambda: True)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "qs"])

    assert result.exit_code == 0, result.output
    assert "openadapt agent serve --allow-run" in result.output
    assert not _ENGINE_AGENT_COMMAND.search(result.output), result.output


def test_agent_forwards_every_argument_and_the_exit_code(fake_agent, supported_python):
    argv = ["serve", "--bundles", "bundles", "--allow-run", "--help"]

    result = CliRunner().invoke(cli_main, ["agent", *argv])

    assert fake_agent == [argv]
    assert result.exit_code == 3
    # An MCP client reads JSON-RPC from this process's stdout, so the launcher
    # must not write anything there before the bridge does.
    assert result.stdout == ""


def test_agent_without_the_bridge_says_how_to_install_it(monkeypatch, supported_python):
    monkeypatch.setitem(sys.modules, "openadapt_agent", None)
    monkeypatch.setitem(sys.modules, "openadapt_agent.cli", None)

    result = CliRunner().invoke(cli_main, ["agent", "serve"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "openadapt-agent is not installed" in result.stderr
    assert "pip install --upgrade openadapt" in result.stderr


def test_agent_on_unsupported_python_prints_the_remedy(monkeypatch, fake_agent):
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: (3, 14))

    result = CliRunner().invoke(cli_main, ["agent", "serve"])

    assert result.exit_code == 1
    assert fake_agent == []
    assert result.stdout == ""
    assert "Python 3.12" in result.stderr
