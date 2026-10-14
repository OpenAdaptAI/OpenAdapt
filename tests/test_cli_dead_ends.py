"""Mistyped launcher commands end in a launcher hint, not a dead end.

`openadapt run`, `openadapt record` and `openadapt replay` printed only
"No such command", and `replay` suggested `deploy`. They are Flow commands,
spelled `openadapt flow <verb>` through the launcher. `openadapt quickstart
--brek-it` forwarded the typo to the engine, which printed its own 43-verb
`openadapt-flow` usage instead of pointing at `--break-it`.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.cli import main as cli_main

TUTORIAL_OPTIONS = frozenset(
    {
        "--break-it",
        "--guided",
        "--headed",
        "--help",
        "--interactive-record",
        "--name",
        "--no-receipt",
        "--out",
        "--presentation-delay",
    }
)


@pytest.fixture()
def supported_python(monkeypatch):
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: cli._SUPPORTED_PYTHON[1])


@pytest.fixture()
def tutorial_options(monkeypatch):
    monkeypatch.setattr(
        "openadapt.cli._flow_tutorial_options",
        lambda: TUTORIAL_OPTIONS,
        raising=False,
    )


@pytest.fixture()
def flow_calls(monkeypatch):
    calls: list[list[str]] = []

    def record(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr("openadapt.cli._invoke_flow", record)
    return calls


@pytest.mark.parametrize("verb", ["run", "record", "replay", "compile", "certify"])
def test_flow_verb_at_top_level_points_at_openadapt_flow(verb):
    result = CliRunner().invoke(cli_main, [verb, "bundle"])

    assert result.exit_code == 2
    assert f"openadapt flow {verb}" in result.output
    assert "deploy" not in result.output


@pytest.mark.parametrize("verb", ["run", "record", "replay"])
def test_flow_verb_hint_works_without_a_readable_flow_parser(monkeypatch, verb):
    monkeypatch.setattr("openadapt.cli._flow_subparsers", lambda: None, raising=False)

    result = CliRunner().invoke(cli_main, [verb])

    assert result.exit_code == 2
    assert f"openadapt flow {verb}" in result.output


def test_other_unknown_commands_keep_clicks_suggestion():
    result = CliRunner().invoke(cli_main, ["flwo"])

    assert result.exit_code == 2
    assert "Did you mean 'flow'?" in result.output


def test_launcher_commands_still_resolve_first():
    result = CliRunner().invoke(cli_main, ["version"])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("out", [None, "qs"])
def test_quickstart_refuses_a_mistyped_flag_before_running(
    supported_python, tutorial_options, flow_calls, out
):
    argv = ["quickstart", "--brek-it"] + (["--out", out] if out else [])
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, argv)

    assert result.exit_code == 2
    assert "No such option: --brek-it. Did you mean --break-it?" in result.output
    assert "usage: openadapt-flow" not in result.output
    assert "Using output directory" not in result.output
    assert flow_calls == []


@pytest.mark.parametrize(
    "extra",
    [["--guided"], ["--interactive-record"], ["--presentation-delay=0.5"]],
)
def test_quickstart_still_forwards_tutorial_flags(
    supported_python, tutorial_options, flow_calls, extra
):
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", *extra, "--out", "qs"])

    assert result.exit_code == 0, result.output
    assert len(flow_calls) == 1
    assert flow_calls[0][-len(extra) :] == extra


def test_quickstart_forwards_unchanged_when_flow_options_are_unreadable(
    monkeypatch, supported_python, flow_calls
):
    monkeypatch.setattr(
        "openadapt.cli._flow_tutorial_options", lambda: None, raising=False
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--future-flag", "--out", "qs"])

    assert result.exit_code == 0, result.output
    assert flow_calls[0][-1] == "--future-flag"


def test_installed_flow_tutorial_exposes_the_documented_flags():
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    options = cli._flow_tutorial_options()

    assert options is not None
    assert {"--break-it", "--guided", "--interactive-record"} <= options
