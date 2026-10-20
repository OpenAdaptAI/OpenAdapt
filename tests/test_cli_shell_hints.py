"""Printed commands paste into the shell a person on this host uses.

Windows is a supported host (the `windows` extra), and its profile paths often
contain a space (C:\\Users\\Jane Doe). The launcher printed:

- the Python remedy as `curl ... | sh` plus a link to a POSIX install.sh,
  neither of which runs in cmd or PowerShell;
- package specs in single quotes (`'openadapt[rdp]'`), which cmd passes to
  pip with the quotes, and unquoted (`openadapt[ml]`), which zsh rejects as a
  glob;
- quickstart follow-ups with the output path unquoted, so a path with a space
  split into several arguments.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.cli import main as cli_main

# A bracketed package spec that isn't inside double quotes.
_UNQUOTED_SPEC = re.compile(r"(?<![\"\w-])openadapt(?:-flow)?\[")


@pytest.fixture()
def supported_python(monkeypatch):
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: cli._SUPPORTED_PYTHON[1])


def _commands(root: Path) -> list[str]:
    """The quickstart's printed commands that carry its output path."""
    from openadapt.quickstart import next_steps
    from openadapt.quickstart.engine import SealFacts
    from openadapt.quickstart.story import Outcome

    outcome = Outcome(root=root, seal=SealFacts("seal-ed25519-0", 5))
    steps = next_steps(cli._hint, cli._shell_quote, False)(outcome)
    return [steps.verify, steps.agent]


def test_posix_quickstart_hints_keep_a_spaced_path_in_one_argument(
    monkeypatch, tmp_path, supported_python
):
    monkeypatch.setattr("sys.platform", "darwin")
    root = tmp_path / "dir with space" / "qs"

    verify, agent = _commands(root)

    assert shlex.split(verify)[-1] == str(root)
    assert shlex.split(agent)[-1] == str(root / "bundle")


def test_windows_quickstart_hints_double_quote_a_spaced_path(
    monkeypatch, tmp_path, supported_python
):
    monkeypatch.setattr("sys.platform", "win32")
    root = tmp_path / "dir with space" / "qs"

    for command in _commands(root):
        assert "'" not in command, command
        tokens = shlex.split(command, posix=False)
        paths = [t for t in tokens if str(tmp_path) in t]
        assert paths, command
        for token in paths:
            assert token.startswith('"') and token.endswith('"'), command


def test_package_specs_are_double_quoted(monkeypatch, supported_python):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name, *args: None)

    doctor = CliRunner().invoke(cli_main, ["doctor", "--backend", "rdp"])
    native = CliRunner().invoke(cli_main, ["doctor", "--backend", "windows"])
    deploy = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    output = doctor.output + native.output + deploy.output + cli._RDP_INSTALL_COMMAND
    assert '"openadapt[rdp]"' in output
    assert '"openadapt[windows]"' in output
    assert '"openadapt[ml]"' in output
    assert "'openadapt" not in output
    assert not _UNQUOTED_SPEC.search(output), output
    assert shlex.split(cli._RDP_INSTALL_COMMAND, posix=False)[-1] == (
        '"openadapt[rdp]"'
    )


def test_deploy_console_hint_is_double_quoted(monkeypatch, supported_python):
    present = {"openadapt_flow", "playwright"}
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name, *args: object() if name in present else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.1")

    result = CliRunner().invoke(cli_main, ["deploy"])

    assert result.exit_code == 0, result.output
    assert '"openadapt-flow[console]==2.0.1"' in result.output
    assert not _UNQUOTED_SPEC.search(result.output), result.output


def test_windows_python_remedy_uses_the_powershell_installer(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")

    remedy = cli._python_remedy()

    assert "| sh" not in remedy
    assert "install.sh" not in remedy
    assert "install.ps1" in remedy
    assert "uv tool install --python 3.12 openadapt" in remedy


def test_posix_python_remedy_keeps_the_shell_installer(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")

    remedy = cli._python_remedy()

    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in remedy
    assert "install.ps1" not in remedy
