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


@pytest.mark.parametrize("verb", ["replay", "certify"])
def test_flow_verb_at_top_level_points_at_openadapt_flow(verb):
    result = CliRunner().invoke(cli_main, [verb, "bundle"])

    assert result.exit_code == 2
    assert f"openadapt flow {verb}" in result.output
    assert "deploy" not in result.output


@pytest.mark.parametrize("verb", ["replay", "certify"])
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


@pytest.mark.parametrize("verb", ["record", "compile", "run"])
@pytest.mark.parametrize("exit_code", [0, 2, 7])
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--help"],
        ["--future-option=value", "a recording", "--param", "x=1", "--param", "x=2"],
        ["--", "--literal-path", "value with spaces"],
    ],
)
def test_root_alias_preserves_existing_flow_arguments_output_and_exit(
    monkeypatch, supported_python, verb, exit_code, arguments
):
    calls = []

    def engine(argv):
        calls.append(list(argv))
        cli.click.echo('{"outcome":"UNCERTAIN","technical":"RECONCILIATION_REQUIRED"}')
        cli.click.echo("engine diagnostic", err=True)
        return exit_code

    monkeypatch.setattr(cli, "_invoke_flow", engine)
    runner = CliRunner()
    old = runner.invoke(cli_main, ["flow", verb, *arguments])
    alias = runner.invoke(cli_main, [verb, *arguments])

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0] == verb
    assert old.exit_code == alias.exit_code == exit_code
    assert (
        old.stdout
        == alias.stdout
        == ('{"outcome":"UNCERTAIN","technical":"RECONCILIATION_REQUIRED"}\n')
    )
    assert old.stderr == alias.stderr == "engine diagnostic\n"


def test_root_aliases_and_help_do_not_need_the_engine_parser(
    monkeypatch, supported_python
):
    def unavailable():
        raise AssertionError("root aliases must not load the engine parser")

    calls = []
    monkeypatch.setattr(cli, "_flow_subparsers", unavailable)
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: calls.append(argv) or 0)
    runner = CliRunner()
    help_result = runner.invoke(cli_main, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    for verb in ("record", "compile", "run"):
        assert verb in help_result.output
        result = runner.invoke(cli_main, [verb, "--future-option"])
        assert result.exit_code == 0, result.output
    assert calls == [[verb, "--future-option"] for verb in ("record", "compile", "run")]


@pytest.mark.parametrize("verb", ["record", "compile", "run"])
def test_root_alias_keeps_the_supported_python_guard(monkeypatch, verb):
    monkeypatch.setattr(cli, "_python_minor", lambda: (3, 13))
    result = CliRunner().invoke(cli_main, [verb, "--help"])
    assert result.exit_code == 1
    assert "uv tool install --python 3.12 openadapt" in result.output


@pytest.mark.parametrize("verb", ["run", "record", "compile"])
def test_visible_tasks_resolve_directly_even_without_parser_discovery(
    monkeypatch, flow_calls, verb
):
    monkeypatch.setattr("openadapt.cli._flow_subparsers", lambda: None)
    result = CliRunner().invoke(cli_main, [verb, "literal path"])
    assert result.exit_code == 0, result.output
    assert flow_calls == [[verb, "literal path"]]
