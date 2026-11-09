"""The task view must retain the old parser namespace and dispatch boundary."""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest
from hypothesis import given, settings, strategies

from openadapt_flow import __main__ as cli
from openadapt_flow import cli_tasks


@pytest.fixture(scope="module")
def parsers():
    parser = cli.build_parser()

    # This is the original parsing implementation over the exact same tree.
    # Bypassing just the root route lets the test compare every registered leaf.
    def legacy(argv):
        return argparse.ArgumentParser.parse_known_args(parser, argv)

    return parser, legacy


def _children(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _required(parser):
    tokens = []
    selected = {
        group._group_actions[0]
        for group in parser._mutually_exclusive_groups
        if group.required
    }
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) or action.dest == "help":
            continue
        if action.option_strings:
            if not action.required and action not in selected:
                continue
            tokens.append(action.option_strings[0])
        elif action.nargs in ("?", "*"):
            continue
        if action.nargs == 0:
            continue
        choice = next(iter(action.choices)) if action.choices is not None else None
        value = str(
            choice
            if choice is not None
            else (1 if action.type in (int, float) else "example")
        )
        count = action.nargs if isinstance(action.nargs, int) else 1
        tokens.extend([value] * count)
    return tokens


def _cases(parser, prefix=()):
    for name, child in _children(parser).items():
        path = (*prefix, name)
        if _children(child):
            for subpath, argv in _cases(child, path):
                yield subpath, [name, *_required(child), *argv]
        else:
            yield path, [name, *_required(child)]


def _capture(parse, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            result = parse(argv)
        except SystemExit as exc:
            result = exc.code
    return result, out.getvalue(), err.getvalue()


def test_every_legacy_leaf_retains_namespace_errors_and_handler(parsers):
    parser, legacy = parsers
    cases = list(_cases(parser))
    assert set(_children(parser)) == set(cli_tasks.FLOW_NAMES) | (
        set(_children(parser)) & set(cli_tasks.INTEGRATION_NAMES)
    )
    assert len(cases) >= 170
    successes = 0
    for path, argv in cases:
        expected = _capture(legacy, argv)
        actual = _capture(parser.parse_known_args, argv)
        assert actual == expected, path
        if isinstance(actual[0], tuple):
            successes += 1
            assert actual[0][0].func is expected[0][0].func
    assert successes >= 160


def test_every_task_route_retains_namespace_errors_and_handler(parsers):
    parser, legacy = parsers
    for path, argv in _cases(parser):
        canonical = parser.task_names[path[0]].split()
        assert _capture(parser.parse_known_args, [*canonical, *argv[1:]]) == _capture(
            legacy, argv
        ), path
    assert set(cli_tasks.FLOW_NAMES) <= set(_children(parser))


@pytest.mark.parametrize(
    "task,path,extra",
    [
        ("run", "replay", []),
        ("run", "resume", []),
        ("run", "--replay", ["--"]),
        ("compile", "compose", ["--out", "out", "--name", "name"]),
        ("compile", "--induce", ["--out", "out", "--name", "name", "--"]),
        ("teach", "branch", ["--fix", "fix", "--bundle", "bundle", "--out", "out"]),
        ("approve", "review", []),
    ],
)
def test_literal_paths_keep_the_leaf_grammar(parsers, task, path, extra):
    parser, legacy = parsers
    argv = [task, *extra, path]
    assert _capture(parser.parse_known_args, argv) == _capture(legacy, argv)
    assert cli_tasks.translate(argv) == argv


def test_routes_do_not_scan_option_values_or_later_tokens():
    for argv in (
        ["record", "--out", "narration"],
        ["demo", "--out", "tutorial"],
        ["run", "bundle", "--replay"],
        ["run", "--", "--resume"],
    ):
        assert cli_tasks.translate(argv) == argv


def test_operation_selectors_were_not_valid_legacy_options(parsers):
    parser, _ = parsers
    for route in parser.task_routes:
        if len(route) == 2 and route[1].startswith("--"):
            leaf = _children(parser)[route[0]]
            matches = [
                option
                for option in leaf._option_string_actions
                if option.startswith(route[1])
            ]
            # argparse permits an unambiguous abbreviated option; do not steal it.
            assert len(matches) != 1, (route, matches)


def test_help_has_fifteen_tasks_verify_second_and_no_unmerged_features(parsers):
    parser, _ = parsers
    help_text = parser.format_help()
    assert list(cli_tasks.TASKS)[:2] == ["demo", "verify"]
    assert len(cli_tasks.TASKS) == 15
    for name in cli_tasks.TASKS:
        assert name in help_text
    for name in ("pilot", "audit", "replay", "serve-execute", "demo-record"):
        assert name not in help_text
    result = _capture(parser.parse_args, ["verify", "--help"])
    assert result[0] == 0
    assert "receipt" in result[1]
    assert ("pilot" in result[1]) == ("pilot" in _children(parser))
    assert not result[2]


def test_later_pilot_registration_is_grouped_without_copying_its_implementation():
    parser = cli_tasks.TaskParser(prog="test")
    sub = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)
    pilot = sub.add_parser("pilot")
    pilot.add_argument("workspace")
    handler = object()
    pilot.set_defaults(func=handler)
    parser.add_task_help()
    assert parser.parse_args(["verify", "pilot", "example"]) == parser.parse_args(
        ["pilot", "example"]
    )
    assert parser.parse_args(["verify", "pilot", "example"]).func is handler


@pytest.mark.parametrize(
    "argv",
    [
        ["replay", "example"],
        ["receipt", "verify", "example", "--json"],
        ["service", "stdio"],
    ],
)
def test_redirected_or_protocol_streams_have_no_alias_notice(monkeypatch, argv):
    streams = [io.StringIO() for _ in range(3)]
    for name, stream in zip(("stdin", "stdout", "stderr"), streams):
        monkeypatch.setattr(sys, name, stream)
    cli_tasks.alias_notice(argv, cli_tasks.FLOW_NAMES, "openadapt-flow")
    assert not streams[1].getvalue() and not streams[2].getvalue()
    for stream in streams:
        stream.isatty = lambda: True
    cli_tasks.alias_notice([*argv, "--json"], cli_tasks.FLOW_NAMES, "openadapt-flow")
    assert not streams[1].getvalue() and not streams[2].getvalue()
    cli_tasks.alias_notice(
        ["replay", "example"], cli_tasks.FLOW_NAMES, "openadapt-flow"
    )
    assert not streams[1].getvalue()
    assert "run --replay" in streams[2].getvalue()


def test_receipt_json_failure_is_identical_through_both_names(tmp_path, capsys):
    argv = [
        "verify",
        str(tmp_path / "missing.receipt"),
        "--issuer-key",
        str(tmp_path / "missing.pub"),
        "--no-log",
        "--json",
    ]
    legacy_code = cli.main(["receipt", *argv])
    legacy = capsys.readouterr()
    canonical_code = cli.main(["verify", "receipt", *argv])
    canonical = capsys.readouterr()
    assert canonical_code == legacy_code == 1
    assert canonical == legacy
    assert '"valid": false' in canonical.out
    assert not canonical.err


def test_launcher_notice_scope_restores_after_failure(monkeypatch):
    streams = [io.StringIO() for _ in range(3)]
    for name, stream in zip(("stdin", "stdout", "stderr"), streams):
        stream.isatty = lambda: True
        monkeypatch.setattr(sys, name, stream)
    with pytest.raises(RuntimeError):
        with cli_tasks.suppress_alias_notices():
            cli_tasks.alias_notice(
                ["replay", "example"], cli_tasks.FLOW_NAMES, "openadapt-flow"
            )
            raise RuntimeError("synthetic failure")
    assert not streams[1].getvalue() and not streams[2].getvalue()
    cli_tasks.alias_notice(
        ["replay", "example"], cli_tasks.FLOW_NAMES, "openadapt-flow"
    )
    assert "run --replay" in streams[2].getvalue()


def test_nested_legacy_help_shows_its_task_replacement(parsers):
    parser, _ = parsers
    code, out, err = _capture(parser.parse_args, ["receipt", "verify", "--help"])
    assert code == 0 and not err
    assert "Also available as: openadapt-flow verify receipt verify" in out


class _UntrustedNoticeSpelling(str):
    """A valid command token must not control how the notice renders it."""

    def __str__(self):
        raise AssertionError("Untrusted argv rendering reached the notice")

    def __format__(self, _format_spec):
        raise AssertionError("Untrusted argv formatting reached the notice")


def _capture_alias_notice(surface, argv, *, tty=True):
    from openadapt import cli_tasks as launcher_tasks

    streams = [io.StringIO() for _ in range(3)]
    with pytest.MonkeyPatch.context() as patch:
        for name, stream in zip(("stdin", "stdout", "stderr"), streams):
            stream.isatty = lambda: tty
            patch.setattr(sys, name, stream)
        if surface == "flow":
            cli_tasks.alias_notice(argv, cli_tasks.FLOW_NAMES, "openadapt-flow")
        else:
            launcher_tasks.alias_notice(["flow", *argv], "openadapt")
    return streams[1].getvalue(), streams[2].getvalue()


@pytest.mark.parametrize("surface", ["flow", "launcher"])
@pytest.mark.parametrize("wrapped", [False, True])
@settings(max_examples=32, deadline=None)
@given(
    alias=strategies.sampled_from(
        sorted(
            name
            for name, canonical in cli_tasks.FLOW_NAMES.items()
            if name != canonical
        )
    ),
    tail=strategies.text(max_size=80),
)
def test_alias_notice_uses_registered_labels_and_never_argument_rendering(
    surface, wrapped, alias, tail
):
    canonical = cli_tasks.FLOW_NAMES[alias]
    token = _UntrustedNoticeSpelling(alias) if wrapped else alias
    synthetic_secret = "SYNTHETIC-TAIL-VALUE=" + tail
    stdout, stderr = _capture_alias_notice(surface, [token, synthetic_secret])
    program = "openadapt-flow" if surface == "flow" else "openadapt"
    legacy_prefix = alias if surface == "flow" else "flow " + alias
    assert stdout == ""
    assert (
        stderr
        == f"{program} {legacy_prefix} is also available as {program} {canonical}\n"
    )
    assert synthetic_secret not in stderr


@pytest.mark.parametrize("surface", ["flow", "launcher"])
@pytest.mark.parametrize(
    "protocol",
    [
        "--json",
        "--jsonl",
        "--stdio",
        "stdio",
        "json",
        "jsonl",
        "--format=json",
        "--output-format=json",
        "--transport=stdio",
    ],
)
def test_trusted_notice_label_selection_retains_protocol_silence(surface, protocol):
    stdout, stderr = _capture_alias_notice(
        surface,
        [_UntrustedNoticeSpelling("replay"), "SYNTHETIC-TAIL-VALUE", protocol],
    )
    assert stdout == stderr == ""


@pytest.mark.parametrize("surface", ["flow", "launcher"])
def test_trusted_notice_label_selection_retains_redirected_silence(surface):
    stdout, stderr = _capture_alias_notice(
        surface,
        [_UntrustedNoticeSpelling("replay"), "SYNTHETIC-TAIL-VALUE"],
        tty=False,
    )
    assert stdout == stderr == ""


@pytest.mark.parametrize("surface", ["flow", "launcher"])
@pytest.mark.parametrize("command", ["run", "unregistered-synthetic-command"])
def test_same_name_and_unregistered_commands_still_have_no_alias_notice(
    surface, command
):
    stdout, stderr = _capture_alias_notice(surface, [_UntrustedNoticeSpelling(command)])
    assert stdout == stderr == ""


def test_launcher_native_alias_labels_and_help_already_come_from_registered_routes():
    from openadapt import cli_tasks as launcher_tasks

    for canonical, legacy in launcher_tasks.NATIVE_ROUTES.items():
        if canonical == legacy or legacy == ("setup",):
            continue
        argv = [_UntrustedNoticeSpelling(token) for token in legacy]
        assert launcher_tasks.legacy_names([*argv, "SYNTHETIC-TAIL-VALUE"]) == (
            " ".join(legacy),
            " ".join(canonical),
        )
    assert (
        launcher_tasks.alias_help(
            ["flow", _UntrustedNoticeSpelling("replay"), "--help"],
            "openadapt",
        )
        == "Also available as: openadapt run --replay"
    )
