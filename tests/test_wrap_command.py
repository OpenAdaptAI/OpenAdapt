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


def test_wrap_example_reaches_the_engine_unchanged(monkeypatch):
    calls = []
    module = types.ModuleType("openadapt_flow.wrap.cli")
    module.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "openadapt_flow.wrap.cli", module)
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    result = CliRunner().invoke(cli.main, ["wrap", "--example", "--json"])
    assert result.exit_code == 0
    assert calls == [["--example", "--json"]]


def test_the_wrap_extra_brings_the_built_in_agent_sdk_and_the_base_stays_lean():
    # ``uvx 'openadapt[wrap]' wrap --example`` runs the built-in Claude agent;
    # the base launcher install must not pull its SDK.
    from pathlib import Path

    import tomllib

    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["project"]
    assert not any(d.startswith("anthropic") for d in project["dependencies"])
    assert any(
        d.startswith("anthropic>=") for d in project["optional-dependencies"]["wrap"]
    )


def test_wrap_keeps_the_separator_before_a_wrapped_command(monkeypatch):
    calls = []
    module = types.ModuleType("openadapt_flow.wrap.cli")
    module.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "openadapt_flow.wrap.cli", module)
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    arguments = ["--task", "t.json", "--json", "--", "python", "agent.py", "--json"]
    monkeypatch.setattr(sys, "argv", ["openadapt", "wrap", *arguments])
    result = CliRunner().invoke(cli.main, ["wrap", *arguments])
    assert result.exit_code == 0
    assert calls == [arguments]


def test_wrap_ignores_an_unrelated_process_argv(monkeypatch):
    calls = []
    module = types.ModuleType("openadapt_flow.wrap.cli")
    module.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "openadapt_flow.wrap.cli", module)
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])
    monkeypatch.setattr(sys, "argv", ["openadapt", "wrap", "--", "other"])
    result = CliRunner().invoke(cli.main, ["wrap", "--example"])
    assert result.exit_code == 0
    assert calls == [["--example"]]
