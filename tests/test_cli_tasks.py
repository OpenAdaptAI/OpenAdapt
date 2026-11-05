"""Canonical launcher names retain native wrappers and Flow passthrough."""

from __future__ import annotations

import sys
import types

import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.cli_tasks import FLOW_NAMES, NATIVE_ROUTES, TASKS, translate


@pytest.mark.parametrize("canonical,legacy", NATIVE_ROUTES.items())
def test_native_alias_help_uses_original_command(canonical, legacy):
    runner = CliRunner()
    old = runner.invoke(cli.main, [*legacy, "--help"])
    new = runner.invoke(cli.main, [*canonical, "--help"])
    assert (new.exit_code, new.stderr) == (old.exit_code, old.stderr)
    # Requested legacy help may append the replacement name. Its original
    # option grammar is identical through the canonical route.
    assert old.stdout.startswith(new.stdout)
    assert new.stdout


@pytest.mark.parametrize(
    "task", [name for name in TASKS if name not in {"verify", "setup"}]
)
def test_task_forwards_exact_argv_to_flow(monkeypatch, task):
    seen = []
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: seen.append(argv) or 7)
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    argv = [task, "--", "replay", "dir with space", "--json"]
    result = CliRunner().invoke(cli.main, argv)
    assert seen == [argv]
    assert result.exit_code == 7
    assert not result.stdout and not result.stderr


def test_agent_stdio_and_exit_status_are_byte_identical(monkeypatch):
    def bridge(argv):
        assert argv == ["serve", "--mode", "sandbox"]
        sys.stdout.write(sys.stdin.read())
        sys.stderr.write("bridge diagnostic\n")
        return 3

    monkeypatch.setitem(
        sys.modules, "openadapt_agent.cli", types.SimpleNamespace(main=bridge)
    )
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    runner = CliRunner()
    body = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    old = runner.invoke(cli.main, ["agent", "serve", "--mode", "sandbox"], input=body)
    new = runner.invoke(
        cli.main, ["services", "agent", "serve", "--mode", "sandbox"], input=body
    )
    assert (old.exit_code, old.stdout, old.stderr) == (3, body, "bridge diagnostic\n")
    assert (new.exit_code, new.stdout, new.stderr) == (
        old.exit_code,
        old.stdout,
        old.stderr,
    )


def test_pull_keeps_old_engine_version_guard(monkeypatch):
    monkeypatch.setattr(cli, "_flow_verbs", lambda: frozenset({"run"}))
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    old = CliRunner().invoke(cli.main, ["pull", "example/workflow"])
    new = CliRunner().invoke(cli.main, ["workflow", "pull", "example/workflow"])
    assert (new.exit_code, new.stdout, new.stderr) == (
        old.exit_code,
        old.stdout,
        old.stderr,
    )
    assert new.exit_code == 1
    assert "upgrade" in new.output.lower()


def test_setup_leaf_and_literal_option_values_remain_unchanged():
    old_names = set(cli.main.commands)
    for argv in (
        ["setup"],
        ["setup", "--client", "clients"],
        ["capture", "start", "--name", "capture"],
        ["flow", "run", "replay"],
        ["verify", "serve", "--port", "8000"],
    ):
        assert translate(argv, old_names) == argv
    assert translate(["setup", "clients", "--client", "x"], old_names) == [
        "setup",
        "--client",
        "x",
    ]


@pytest.mark.parametrize(
    "task", ["verify", "services", "workflow", "research", "inspect"]
)
def test_group_help_is_available_without_running_a_service(task):
    result = CliRunner().invoke(cli.main, [task, "--help"])
    assert result.exit_code == 0, result.output
    assert "operations:" in result.stdout
    assert not result.stderr


def test_launcher_route_table_matches_the_engine_source():
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "packages/flow/openadapt_flow/cli_tasks.py"
    )
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"FLOW_NAMES", "TASKS"}
    }
    assert FLOW_NAMES == assignments["FLOW_NAMES"]
    assert TASKS == assignments["TASKS"]


@pytest.mark.parametrize("old,new", FLOW_NAMES.items())
def test_canonical_flow_names_route_to_released_engine_verbs(old, new):
    canonical = [*new.split(), "literal path", "--json"]
    native = set(cli.main.commands)
    # Native wrappers deliberately take precedence (notably pull and shadow).
    if any(tuple(canonical[: len(prefix)]) == prefix for prefix in NATIVE_ROUTES):
        return
    if (
        old
        in {
            "approve",
            "compile",
            "demo",
            "qualify",
            "record",
            "repair",
            "run",
            "teach",
            "undo",
        }
        or new != old
    ):
        assert translate(canonical, native) == ["flow", old, "literal path", "--json"]


def test_legacy_flow_notice_uses_the_launcher_task_name(monkeypatch):
    import io

    from openadapt.cli_tasks import alias_notice

    streams = [io.StringIO() for _ in range(3)]
    for name, stream in zip(("stdin", "stdout", "stderr"), streams):
        stream.isatty = lambda: True
        monkeypatch.setattr(sys, name, stream)
    alias_notice(["flow", "replay", "literal path"], "openadapt")
    assert (
        streams[2].getvalue()
        == "openadapt flow replay is also available as openadapt run --replay\n"
    )
    assert not streams[1].getvalue()
    streams[2].seek(0)
    streams[2].truncate()
    alias_notice(["flow", "receipt", "verify", "example", "--json"], "openadapt")
    assert not streams[2].getvalue()


def test_passthrough_alias_help_shows_its_task_replacement():
    result = CliRunner().invoke(
        cli.main, ["agent", "serve", "--help"], prog_name="openadapt"
    )
    assert result.exit_code == 0
    assert "Also available as: openadapt services agent" in result.stdout
    assert not result.stderr


def test_literal_help_filename_does_not_request_an_alias_footer():
    from openadapt.cli_tasks import alias_help

    assert alias_help(["flow", "replay", "--", "--help"], "openadapt") is None
