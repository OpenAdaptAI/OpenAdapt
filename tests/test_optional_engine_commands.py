"""Optional engine entrypoints retain help and argument ownership."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openadapt import cli

_COMMANDS = (
    ("wrap", ("openadapt_flow.wrap.cli",), []),
    ("agent", ("openadapt_agent.cli",), []),
    ("seal", ("openadapt_agent.seal_cli", "openadapt_agent.cli"), ["seal"]),
)


@pytest.fixture(autouse=True)
def supported_python(monkeypatch):
    monkeypatch.setattr(cli, "_python_minor", lambda: cli._SUPPORTED_PYTHON[1])


@pytest.mark.parametrize("command,modules,prefix", _COMMANDS)
@pytest.mark.parametrize("help_flag", ["--help", "-h"])
@pytest.mark.parametrize("missing_ancestor", [False, True])
def test_pure_help_for_exact_missing_command(
    monkeypatch, command, modules, prefix, help_flag, missing_ancestor
):
    missing = modules[0].split(".")[0] if missing_ancestor else modules[0]

    def load(name):
        raise ModuleNotFoundError(name=missing)

    monkeypatch.setattr(cli, "import_module", load)
    result = CliRunner().invoke(cli.main, [command, help_flag])
    assert result.exit_code == 0, result.output
    assert f"Usage: main {command}" in result.output


@pytest.mark.parametrize("command,modules,prefix", _COMMANDS)
@pytest.mark.parametrize("arguments", [["--help", "--unknown"], ["missing.json"]])
def test_absent_command_does_not_accept_mixed_arguments(
    monkeypatch, command, modules, prefix, arguments
):
    def load(name):
        raise ModuleNotFoundError(name=modules[0])

    monkeypatch.setattr(cli, "import_module", load)
    result = CliRunner().invoke(cli.main, [command, *arguments])
    assert result.exit_code != 0
    assert result.stdout == ""


@pytest.mark.parametrize("command,modules,prefix", _COMMANDS)
@pytest.mark.parametrize("arguments", [["--help"], ["--task", "task.json", "--help"]])
def test_installed_command_owns_original_arguments(
    monkeypatch, command, modules, prefix, arguments
):
    imports, calls = [], []

    def main(argv):
        calls.append(argv)
        return 3

    def load(name):
        imports.append(name)
        return SimpleNamespace(main=main)

    monkeypatch.setattr(cli, "import_module", load)
    result = CliRunner().invoke(cli.main, [command, *arguments])
    assert result.exit_code == 3
    assert imports == list(modules)
    assert calls == [[*prefix, *arguments]]
    assert result.stdout == ""


@pytest.mark.parametrize("command,modules,prefix", _COMMANDS)
@pytest.mark.parametrize("error_kind", [ImportError, ModuleNotFoundError])
def test_installed_command_import_failure_is_not_missing_command_help(
    monkeypatch, command, modules, prefix, error_kind
):
    error = error_kind("broken installed dependency", name="engine_dependency")

    def load(name):
        raise error

    monkeypatch.setattr(cli, "import_module", load)
    result = CliRunner().invoke(cli.main, [command, "--help"])
    assert result.exit_code != 0
    assert result.exception is error
    assert result.stdout == ""
