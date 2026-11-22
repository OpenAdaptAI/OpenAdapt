"""Every ``openadapt`` launcher command, subcommand and option stays as it is.

The monorepo refactor (openadapt-internal ``docs/design/monorepo-refactor-
2026-11/PLAN.md``) moves code without removing functionality. This test pins
the Click tree under ``openadapt.cli.main``:

* the structure, on every Python version: each command path, whether it is a
  group, its subcommands and hidden flag, and for each parameter its kind,
  names, ``nargs``, ``required``, flag and multiple settings, type, choices,
  a primitive default and help text;
* the ``--help`` text of every command, byte for byte, at a fixed width, on
  the Python minor and Click version the snapshot was written on (the
  launcher's ``uv.lock`` pins Click).

Flow's own commands, which the launcher forwards, are pinned by
``packages/flow/tests/test_cli_surface.py``.

The snapshot is ``fixtures/cli_surface.json``. When a change deliberately adds
or changes a command or option, regenerate it and say why in the pull request:

    OPENADAPT_WRITE_CLI_SURFACE=1 pytest tests/test_cli_surface.py
"""

from __future__ import annotations

import difflib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

import click
import pytest

SNAPSHOT = Path(__file__).parent / "fixtures" / "cli_surface.json"
PROG = "openadapt"
WIDTH = 100


def _primitive(value: Any) -> Any:
    if callable(value):
        return "<callable>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        home = str(Path.home())
        return (
            "<machine-dependent>" if home in value or os.sep + "tmp" in value else value
        )
    if isinstance(value, (list, tuple)):
        return [_primitive(v) for v in value]
    return f"<{type(value).__name__}>"


def _param(param: click.Parameter) -> dict[str, Any]:
    kind = param.type
    row: dict[str, Any] = {
        "kind": type(param).__name__,
        "name": param.name,
        "opts": list(param.opts),
        "secondary_opts": list(param.secondary_opts),
        "nargs": param.nargs,
        "required": param.required,
        "multiple": param.multiple,
        "type": type(kind).__name__,
        "choices": list(kind.choices) if isinstance(kind, click.Choice) else None,
        "default": _primitive(param.default),
    }
    if isinstance(param, click.Option):
        row.update(
            is_flag=param.is_flag,
            hidden=param.hidden,
            help=param.help,
        )
    return row


def _walk(
    command: click.Command,
    path: str,
    parent: click.Context | None,
    tree: dict[str, Any],
    helps: dict[str, str],
) -> None:
    ctx = click.Context(
        command,
        info_name=path.split(" ")[-1],
        parent=parent,
        terminal_width=WIDTH,
        max_content_width=WIDTH,
        resilient_parsing=True,
    )
    subcommands: dict[str, bool] = {}
    if isinstance(command, click.Group):
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            if sub is not None:
                subcommands[name] = bool(sub.hidden)
    tree[path] = {
        "group": isinstance(command, click.Group),
        "hidden": bool(command.hidden),
        "params": [_param(p) for p in command.params],
        "commands": subcommands,
    }
    helps[path] = command.get_help(ctx)
    if isinstance(command, click.Group):
        for name in subcommands:
            sub = command.get_command(ctx, name)
            assert sub is not None
            _walk(sub, f"{path} {name}", ctx, tree, helps)


def _current() -> dict[str, Any]:
    from openadapt.cli import main

    tree: dict[str, Any] = {}
    helps: dict[str, str] = {}
    _walk(main, PROG, None, tree, helps)
    return {
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "click": importlib.metadata.version("click"),
        "width": WIDTH,
        "tree": tree,
        "help": helps,
    }


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_snapshot_is_current_when_regenerating() -> None:
    if os.environ.get("OPENADAPT_WRITE_CLI_SURFACE") != "1":
        pytest.skip("set OPENADAPT_WRITE_CLI_SURFACE=1 to regenerate")
    SNAPSHOT.write_text(
        json.dumps(_current(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_every_command_and_option_is_unchanged() -> None:
    pinned = _snapshot()["tree"]
    current = _current()["tree"]
    missing = sorted(set(pinned) - set(current))
    assert missing == [], f"commands no longer registered: {missing}"
    added = sorted(set(current) - set(pinned))
    assert added == [], (
        f"new commands {added}: regenerate the snapshot (see this test's docstring)"
    )
    changed = [path for path in sorted(pinned) if pinned[path] != current[path]]
    assert changed == [], (
        f"options or subcommands changed under {changed}. A move must keep every "
        "option; regenerate the snapshot only for a deliberate change."
    )


def test_help_text_is_byte_identical() -> None:
    snapshot = _snapshot()
    here = f"{sys.version_info[0]}.{sys.version_info[1]}"
    click_here = importlib.metadata.version("click")
    if (snapshot["python"], snapshot["click"]) != (here, click_here):
        pytest.skip(
            f"help text pinned on Python {snapshot['python']} with Click "
            f"{snapshot['click']}, not {here} with {click_here}"
        )
    current = _current()["help"]
    changed = [
        p for p in sorted(snapshot["help"]) if snapshot["help"][p] != current.get(p)
    ]
    if not changed:
        return
    diffs = []
    for path in changed:
        diffs.append(
            "\n".join(
                difflib.unified_diff(
                    snapshot["help"][path].splitlines(),
                    (current.get(path) or "").splitlines(),
                    fromfile=f"snapshot {path}",
                    tofile=f"current {path}",
                    lineterm="",
                )
            )
        )
    raise AssertionError(
        f"--help text changed for {changed}. Regenerate with "
        "OPENADAPT_WRITE_CLI_SURFACE=1 pytest tests/test_cli_surface.py\n"
        + "\n".join(diffs)
    )
