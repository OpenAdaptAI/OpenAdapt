"""The launcher delegates wrap arguments and leaves the MCP wire untouched."""

from __future__ import annotations

import sys
import types

from click.testing import CliRunner

from openadapt import cli


def test_wrap_delegates_arguments_and_exit_without_stdout(monkeypatch):
    calls = []
    module = types.ModuleType("openadapt_flow.wrap.cli")

    def main(argv):
        calls.append(argv)
        return 3

    module.main = main
    monkeypatch.setitem(sys.modules, "openadapt_flow.wrap.cli", module)
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    arguments = ["--task", "task.json", "--serve", "--help"]
    result = CliRunner().invoke(cli.main, ["wrap", *arguments])
    assert result.exit_code == 3
    assert calls == [arguments]
    assert result.stdout == ""
