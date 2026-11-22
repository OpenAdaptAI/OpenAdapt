"""`openadapt flight` passes through to `openadapt flow flight`."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from openadapt import cli


@pytest.fixture
def captured(monkeypatch):
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(cli, "_flow_verbs", lambda: frozenset({"flight"}))
    monkeypatch.setattr(cli, "_run_flow", lambda argv: seen.update(argv=list(argv)))
    return seen


def test_flight_passes_every_argument_and_restores_the_double_dash(captured):
    result = CliRunner().invoke(
        cli.main,
        ["flight", "--task-id", "t1", "--gate=file", "--json", "--", "python", "agent.py", "--flag", "-v"],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "flight", "--task-id", "t1", "--gate=file", "--json",
        "--", "python", "agent.py", "--flag", "-v",
    ]


def test_a_command_that_starts_with_a_dash_stays_the_command(captured):
    CliRunner().invoke(cli.main, ["flight", "--", "--weird-tool", "x"])
    assert captured["argv"] == ["flight", "--", "--weird-tool", "x"]


def test_help_goes_to_the_engine(captured):
    CliRunner().invoke(cli.main, ["flight", "--help"])
    assert captured["argv"] == ["flight", "--help"]


def _released_engine(monkeypatch):
    """The released openadapt-flow 3.4.0 (what the launcher CI installs): its
    verbs, without flight, and its version."""
    from importlib import metadata

    real = cli._flow_verbs
    verbs = frozenset(real() if real() else ()) - {"flight"}
    monkeypatch.setattr(cli, "_flow_verbs", lambda: verbs)
    original = metadata.version
    monkeypatch.setattr(
        metadata,
        "version",
        lambda name: "3.4.0" if name == "openadapt-flow" else original(name),
    )


def test_an_engine_without_flight_says_which_version_it_needs(monkeypatch):
    _released_engine(monkeypatch)
    result = CliRunner().invoke(cli.main, ["flight", "--", "true"])
    assert result.exit_code == cli.FLIGHT_ENGINE_TOO_OLD_EXIT == 4
    assert "needs openadapt-flow >= 3.5.0" in result.output
    assert "installed: 3.4.0" in result.output


def test_help_works_without_the_flight_verb_and_says_what_is_missing(monkeypatch):
    _released_engine(monkeypatch)
    result = CliRunner().invoke(cli.main, ["flight", "--help"])
    assert result.exit_code == 0, result.output
    assert "Record any agent you already run" in result.output
    assert "needs openadapt-flow >= 3.5.0" in result.output


def test_the_launcher_knows_every_flight_option():
    flight_cli = pytest.importorskip("openadapt_flow.flight.cli")
    import argparse

    parser = argparse.ArgumentParser()
    flight_cli.add_parser(parser.add_subparsers())
    sub = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ).choices["flight"]
    valued, flags = set(), set()
    for action in sub._actions:
        for name in action.option_strings:
            (flags if action.nargs == 0 else valued).add(name)
    assert valued == set(cli.FLIGHT_VALUE_OPTIONS)
    assert flags == set(cli.FLIGHT_FLAGS)
