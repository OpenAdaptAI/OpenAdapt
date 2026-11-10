"""Root routing is one command-prefix decision; no backend handler runs here."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from openadapt import cli, cli_tasks

# Exercise every public shared route unless its explicit native wrapper wins.
FLOW_CASES = [
    (old, new.split())
    for old, new in cli_tasks.FLOW_NAMES.items()
    if not any(
        tuple(new.split()[: len(prefix)]) == prefix
        for prefix in cli_tasks.NATIVE_ROUTES
    )
]


@pytest.mark.parametrize("old,prefix", FLOW_CASES)
@pytest.mark.parametrize("root_delimiter", [False, True])
@pytest.mark.parametrize("normalized", [False, True])
def test_whole_flow_route_family_preserves_literals_and_streams(
    monkeypatch, old, prefix, root_delimiter, normalized
):
    seen = []
    suffix = ["--", "--LiteralOperand", "path with spaces", "--json"]

    def engine(argv):
        seen.append(list(argv))
        cli.click.echo('{"status":"synthetic"}')
        cli.click.echo("diagnostic", err=True)
        return 7

    monkeypatch.setattr(cli, "_invoke_flow", engine)
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    tokens = [token.upper() for token in prefix] if normalized else prefix
    kwargs = {"token_normalize_func": str.lower} if normalized else {}
    result = CliRunner().invoke(
        cli.main, [*(["--"] if root_delimiter else []), *tokens, *suffix], **kwargs
    )
    assert seen == [[old, *suffix]]
    assert result.exit_code == 7
    assert result.stdout == '{"status":"synthetic"}\n'
    assert result.stderr == "diagnostic\n"


@pytest.mark.parametrize("verb", ["record", "compile", "run"])
@pytest.mark.parametrize("root_delimiter", [False, True])
def test_main_native_alias_and_explicit_flow_preserve_the_same_leaf_delimiter(
    monkeypatch, verb, root_delimiter
):
    calls = []
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    assert isinstance(cli.main.commands[verb], cli._FlowAliasCommand)
    arguments = ["--", "--replay", "literal record", "--param", "NAME=VaLuE"]
    root = ["--"] if root_delimiter else []
    explicit = CliRunner().invoke(cli.main, [*root, "flow", verb, *arguments])
    alias = CliRunner().invoke(cli.main, [*root, verb, *arguments])
    assert explicit.exit_code == alias.exit_code == 0
    assert calls == [[verb, *arguments], [verb, *arguments]]
    assert explicit.stdout == alias.stdout == ""
    assert explicit.stderr == alias.stderr == ""


@pytest.mark.parametrize("canonical,legacy", cli_tasks.NATIVE_ROUTES.items())
def test_whole_native_route_family_keeps_its_original_help_and_wrapper(
    canonical, legacy
):
    old = CliRunner().invoke(cli.main, [*legacy, "--help"])
    new = CliRunner().invoke(
        cli.main,
        ["--", *[token.upper() for token in canonical], "--help"],
        token_normalize_func=str.lower,
    )
    assert (new.exit_code, new.stderr) == (old.exit_code, old.stderr)
    assert old.stdout.startswith(new.stdout)
    assert new.stdout


@pytest.mark.parametrize("root_delimiter", [False, True])
def test_wrap_binds_the_real_native_entrypoint_without_flow_dispatch(
    monkeypatch, root_delimiter
):
    calls = []
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)

    def entry(argv):
        calls.append(("argv", argv))
        return 7

    def load(_ctx, modules):
        calls.append(("modules", modules))
        return entry

    monkeypatch.setattr(cli, "_optional_engine_entrypoint", load)

    def no_flow(_argv):
        raise AssertionError("wrap must retain its native optional-entrypoint guard")

    monkeypatch.setattr(cli, "_invoke_flow", no_flow)
    result = CliRunner().invoke(
        cli.main, [*(["--"] if root_delimiter else []), "wrap", "--task", "Task.json"]
    )
    assert result.exit_code == 7
    assert calls == [
        ("modules", ("openadapt_flow.wrap.cli",)),
        ("argv", ["--task", "Task.json"]),
    ]
    assert cli_tasks.LAUNCHER_TASKS.keys() <= cli.main.commands.keys()
    assert "wrap" not in cli_tasks.FLOW_NAMES


@pytest.mark.parametrize(
    "tokens", [["not-a-task"], ["NOT-A-TASK", "record"], ["ru", "record"]]
)
@pytest.mark.parametrize("root_delimiter", [False, True])
def test_unknown_root_commands_refuse_without_a_delegate(
    monkeypatch, tokens, root_delimiter
):
    calls = []
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(cli, "_flow_verbs", lambda: frozenset())
    monkeypatch.setattr(cli, "_warn_about_launcher_conflicts", lambda: None)
    result = CliRunner().invoke(
        cli.main,
        [*(["--"] if root_delimiter else []), *tokens],
        token_normalize_func=str.lower,
    )
    assert result.exit_code == 2
    assert calls == []


@pytest.mark.parametrize("task", ["services", "workflow", "research", "inspect"])
def test_unknown_task_operation_reaches_only_normal_parser_refusal(monkeypatch, task):
    from openadapt_flow.__main__ import build_parser

    parsed = []
    handler_calls = []
    parser = build_parser()

    def engine(argv):
        parsed.append(list(argv))
        args = parser.parse_args(argv)
        handler_calls.append(args.func)
        raise AssertionError("unknown operations must not select an executable handler")

    monkeypatch.setattr(cli, "_invoke_flow", engine)
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    result = CliRunner().invoke(cli.main, ["--", task, "not-an-operation", "record"])
    assert result.exit_code == 2
    assert parsed == [[task, "not-an-operation", "record"]]
    assert handler_calls == []


def test_prefix_matching_is_longest_first_and_never_normalizes_values(monkeypatch):
    monkeypatch.setattr(
        cli_tasks,
        "FLOW_NAMES",
        {"short": "inspect state", "long": "inspect state detail"},
    )
    monkeypatch.setattr(cli_tasks, "NATIVE_ROUTES", {})
    assert cli_tasks.translate(
        ("INSPECT", "STATE", "DETAIL", "MiXeD", "--", "--Literal"), set(), str.lower
    ) == ["flow", "long", "MiXeD", "--", "--Literal"]
    assert cli_tasks.translate(("RUN", "--", "--REPLAY"), {"run"}, str.lower) == [
        "RUN",
        "--",
        "--REPLAY",
    ]
    monkeypatch.setattr(
        cli_tasks,
        "NATIVE_ROUTES",
        {("services", "entry"): ("short",), ("services", "entry", "detail"): ("long",)},
    )
    assert cli_tasks.translate(
        ("SERVICES", "ENTRY", "DETAIL", "MiXeD"), set(), str.lower
    ) == ["long", "MiXeD"]
