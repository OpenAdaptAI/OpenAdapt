"""Growth-polish contracts for `openadapt quickstart`.

Covers the launcher-lane changes: default-directory auto-suffix, verbatim
explicit --out, engine flag passthrough, and the plain-language Python
version / PEP 668 remedy.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt.cli import main as cli_main
from openadapt.quickstart.story import Outcome


@pytest.fixture()
def engine_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda argv: calls.append(list(argv)) or 0,
    )
    return calls


@pytest.fixture()
def story_calls(monkeypatch):
    """Stand in for the three-act quickstart; record what it was asked to do."""
    calls = []

    def fake(root, **kwargs):
        calls.append({"root": root, **kwargs})
        return Outcome(root=root)

    monkeypatch.setattr("openadapt.cli._flow_runs_quickstart", lambda: True)
    monkeypatch.setattr("openadapt.quickstart.run_quickstart", fake)
    return calls


def test_default_output_auto_suffixes_the_first_taken_name(story_calls):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("openadapt-quickstart").mkdir()
        result = runner.invoke(cli_main, ["quickstart"])
        expected = Path("openadapt-quickstart-2").resolve()

    assert result.exit_code == 0, result.output
    assert len(story_calls) == 1
    assert story_calls[0]["root"] == expected


def test_default_output_keeps_counting_past_repeated_runs(story_calls):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("openadapt-quickstart").mkdir()
        Path("openadapt-quickstart-2").mkdir()
        Path("openadapt-quickstart-3").mkdir()
        result = runner.invoke(cli_main, ["quickstart"])
        expected = Path("openadapt-quickstart-4").resolve()

    assert result.exit_code == 0, result.output
    assert story_calls[0]["root"] == expected


def test_explicit_out_is_honored_verbatim(story_calls):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("openadapt-quickstart").mkdir()
        result = runner.invoke(cli_main, ["quickstart", "--out", "my-dir"])
        expected = Path("my-dir").resolve()

    assert result.exit_code == 0, result.output
    assert story_calls[0]["root"] == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], {"break_it": True, "verbose": False}),
        (["--no-break"], {"break_it": False}),
        (["--verbose"], {"verbose": True}),
        (["--break-it"], {"break_it": True}),
    ],
)
def test_quickstart_options_reach_the_story(story_calls, monkeypatch, argv, expected):
    monkeypatch.setattr("openadapt.quickstart.engine.display_available", lambda: False)
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(cli_main, ["quickstart", *argv, "--out", "qs"])
    assert result.exit_code == 0, result.output
    for key, value in expected.items():
        assert story_calls[0][key] == value


@pytest.mark.parametrize(
    ("argv", "display", "visible"),
    [
        ([], True, True),
        ([], False, False),
        (["--headless"], True, False),
        (["--headed"], False, True),
    ],
)
def test_the_browser_shows_when_a_display_is_available(
    story_calls, monkeypatch, argv, display, visible
):
    monkeypatch.setattr(
        "openadapt.quickstart.engine.display_available", lambda: display
    )
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(cli_main, ["quickstart", *argv, "--out", "qs"])
    assert result.exit_code == 0, result.output
    assert story_calls[0]["visible"] is visible


def test_an_older_flow_runs_its_own_tutorial(monkeypatch, engine_calls):
    monkeypatch.setattr("openadapt.cli._flow_runs_quickstart", lambda: False)
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(cli_main, ["quickstart", "--out", "qs"])
    assert result.exit_code == 0, result.output
    assert engine_calls[0][:1] == ["tutorial"]


@pytest.mark.parametrize(
    ("installed", "runs"),
    [("2.1.0", False), ("2.2.0", True), ("2.10.1", True)],
)
def test_the_three_act_quickstart_needs_flow_2_2(monkeypatch, installed, runs):
    from openadapt import cli

    monkeypatch.setattr("importlib.metadata.version", lambda _name: installed)
    assert cli._flow_runs_quickstart() is runs


def test_quickstart_restores_the_operator_scrub_setting(monkeypatch, story_calls):
    seen = []
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    monkeypatch.setattr(
        "openadapt.quickstart.run_quickstart",
        lambda root, **kw: (
            seen.append(os.environ["OPENADAPT_FLOW_SCRUB"]) or Outcome(root=root)
        ),
    )
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(cli_main, ["quickstart", "--out", "qs"])
    assert result.exit_code == 0, result.output
    assert seen == ["off"]
    assert os.environ["OPENADAPT_FLOW_SCRUB"] == "auto"


def test_a_stopped_stage_names_where_its_files_are(monkeypatch, story_calls):
    class TutorialError(RuntimeError):
        pass

    def stop(root, **kwargs):
        root.mkdir(parents=True)
        (root / "partial.json").write_text("{}")
        raise TutorialError("the compiler emitted a placeholder")

    monkeypatch.setattr("openadapt.quickstart.run_quickstart", stop)
    with CliRunner().isolated_filesystem():
        result = CliRunner().invoke(cli_main, ["quickstart", "--out", "qs"])
        assert Path("qs/partial.json").is_file()
    assert result.exit_code == 1
    assert "The quickstart stopped: the compiler emitted a placeholder" in result.output
    assert "Anything it wrote is in" in result.output


@pytest.mark.parametrize("version_info", [(3, 13), (3, 14)])
def test_unsupported_python_preflight_prints_remedy_before_delegation(
    monkeypatch, engine_calls, version_info
):
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: version_info)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart"])

    assert result.exit_code != 0
    assert "OpenAdapt needs Python 3.10\u20133.12" in result.output
    assert f"This is Python {version_info[0]}.{version_info[1]}" in result.output
    assert "uv tool install --python 3.12 openadapt" in result.output
    assert (
        "https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/"
        "main/packages/flow/scripts/install.sh" in result.output
    )
    assert engine_calls == []


def test_pep668_error_prints_the_same_remedy(monkeypatch, story_calls):
    def raise_externally_managed(*_args, **_kwargs):
        raise RuntimeError(
            "error: externally-managed-environment\n\n"
            "This environment is externally managed"
        )

    monkeypatch.setattr("openadapt.quickstart.run_quickstart", raise_externally_managed)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "pep-run"])

    assert result.exit_code != 0
    assert "OpenAdapt needs Python 3.10\u20133.12" in result.output
    assert "managed by your operating system" in result.output
    assert "uv tool install --python 3.12 openadapt" in result.output


@pytest.mark.parametrize(
    "extra",
    [
        ["--guided"],
        ["--interactive-record"],
        ["--profile", "strict"],
    ],
)
def test_unknown_flags_pass_through_to_engine_verbatim(
    monkeypatch, engine_calls, extra
):
    # A newer Flow tutorial that adds `--profile` gets it without a launcher
    # change: quickstart checks flags against the installed engine's parser.
    monkeypatch.setattr(
        "openadapt.cli._flow_tutorial_options",
        lambda: frozenset({"--guided", "--interactive-record", "--profile"}),
        raising=False,
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", *extra])

    assert result.exit_code == 0, result.output
    argv = engine_calls[0]
    assert argv[0] == "tutorial"
    assert argv[-len(extra) :] == extra
