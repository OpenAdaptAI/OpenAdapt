"""Growth-polish contracts for `openadapt quickstart`.

Covers the launcher-lane changes: default-directory auto-suffix, verbatim
explicit --out, engine flag passthrough, and the plain-language Python
version / PEP 668 remedy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt.cli import main as cli_main


@pytest.fixture()
def engine_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda argv: calls.append(list(argv)) or 0,
    )
    return calls


def test_default_output_auto_suffixes_the_first_taken_name(engine_calls):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("openadapt-quickstart").mkdir()
        result = runner.invoke(cli_main, ["quickstart"])
        expected = Path("openadapt-quickstart-2").resolve()

    assert result.exit_code == 0, result.output
    assert len(engine_calls) == 1
    assert engine_calls[0][2] == str(expected)
    assert "openadapt-quickstart-2" in result.output


def test_default_output_keeps_counting_past_repeated_runs(engine_calls):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("openadapt-quickstart").mkdir()
        Path("openadapt-quickstart-2").mkdir()
        Path("openadapt-quickstart-3").mkdir()
        result = runner.invoke(cli_main, ["quickstart"])
        expected = Path("openadapt-quickstart-4").resolve()

    assert result.exit_code == 0, result.output
    assert engine_calls[0][2] == str(expected)


def test_explicit_out_is_honored_verbatim(engine_calls):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("openadapt-quickstart").mkdir()
        result = runner.invoke(cli_main, ["quickstart", "--out", "my-dir"])
        expected = Path("my-dir").resolve()

    assert result.exit_code == 0, result.output
    assert engine_calls[0][2] == str(expected)


def test_python_313_preflight_prints_remedy_before_delegation(
    monkeypatch, engine_calls
):
    monkeypatch.setattr(sys, "version_info", (3, 13, 1))

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart"])

    assert result.exit_code != 0
    assert "OpenAdapt needs Python 3.10\u20133.12" in result.output
    assert "uv venv --python 3.12" in result.output
    assert (
        "https://raw.githubusercontent.com/OpenAdaptAI/openadapt-flow/"
        "main/scripts/install.sh" in result.output
    )
    assert engine_calls == []


def test_pep668_error_prints_the_same_remedy(monkeypatch):
    def raise_externally_managed(_argv):
        raise RuntimeError(
            "error: externally-managed-environment\n\n"
            "This environment is externally managed"
        )

    monkeypatch.setattr("openadapt.cli._invoke_flow", raise_externally_managed)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "pep-run"])

    assert result.exit_code != 0
    assert "OpenAdapt needs Python 3.10\u20133.12" in result.output
    assert "uv pip install openadapt" in result.output


@pytest.mark.parametrize(
    "extra",
    [
        ["--guided"],
        ["--interactive-record"],
        ["--profile", "strict"],
    ],
)
def test_unknown_flags_pass_through_to_engine_verbatim(engine_calls, extra):
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", *extra])

    assert result.exit_code == 0, result.output
    argv = engine_calls[0]
    assert argv[0] == "tutorial"
    assert argv[-len(extra) :] == extra
