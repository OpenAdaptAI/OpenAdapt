"""`openadapt doctor` shows model-access checks only for research installs.

Doctor always printed "PyTorch not installed", "ANTHROPIC_API_KEY not set" and
"OPENAI_API_KEY not set", even on a base install. Healthy runs of compiled
workflows make no model calls, so those lines told evaluators that an AI key
and a GPU stack were missing when neither is used. Only the research extras
(`openadapt train`, `openadapt eval`) use them.
"""

from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.cli import main as cli_main


@pytest.fixture()
def supported_python(monkeypatch):
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: cli._SUPPORTED_PYTHON[1])


def _installed_except(monkeypatch, missing: set[str]) -> None:
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name, *args: None if name in missing else object(),
    )


@pytest.fixture()
def no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Force the PyTorch probe to see no torch, whatever this host has.
    monkeypatch.setitem(sys.modules, "torch", None)


def test_base_install_says_no_model_access_is_needed(
    monkeypatch, supported_python, no_keys
):
    _installed_except(monkeypatch, {"openadapt_ml", "openadapt_evals"})

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "windows"])

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" not in result.output
    assert "OPENAI_API_KEY" not in result.output
    assert "PyTorch" not in result.output
    assert "GPU" not in result.output
    assert "no model calls" in result.output


@pytest.mark.parametrize("research", ["openadapt_ml", "openadapt_evals"])
def test_research_install_shows_the_model_checks_under_research(
    monkeypatch, supported_python, no_keys, research
):
    _installed_except(monkeypatch, {"openadapt_ml", "openadapt_evals"} - {research})

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "windows"])

    assert result.exit_code == 0, result.output
    heading = result.output.index("Research extras")
    for line in ("PyTorch not installed", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert result.output.index(line) > heading, line
    assert "openadapt eval" in result.output
