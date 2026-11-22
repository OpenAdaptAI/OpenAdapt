"""Every ``openadapt-flow`` command, subcommand and flag stays as it is.

The monorepo refactor (openadapt-internal ``docs/design/monorepo-refactor-
2026-11/PLAN.md``) moves command registration out of ``__main__.py`` into
per-group modules. A move must not drop, rename or change a command or flag.
This test pins the parser tree that ``build_parser()`` builds:

* the structure, on every Python version: each command path, its aliases, and
  for each argument its option strings, ``dest``, action, ``nargs``,
  ``required``, choices, metavar, type, help text and a primitive default,
  and each mutually exclusive group;
* the ``--help`` text of every command, byte for byte, on the Python minor the
  snapshot was written on (argparse formats help differently across minors).

The snapshot is ``fixtures/cli_surface.json``. When a change deliberately adds
or changes a command or flag, regenerate it and say why in the pull request:

    OPENADAPT_WRITE_CLI_SURFACE=1 pytest tests/test_cli_surface.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

SNAPSHOT = Path(__file__).parent / "fixtures" / "cli_surface.json"
PROG = "openadapt-flow"
COLUMNS = "100"


def _default(value: Any) -> Any:
    # Only values that cannot depend on the machine: a path, a time or an
    # environment lookup would make the snapshot differ between hosts.
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if value is argparse.SUPPRESS:
        return "==SUPPRESS=="
    if isinstance(value, str):
        home = str(Path.home())
        return (
            "<machine-dependent>" if home in value or os.sep + "tmp" in value else value
        )
    if isinstance(value, (list, tuple)) and all(
        v is None or isinstance(v, (bool, int, float, str)) for v in value
    ):
        return [_default(v) for v in value]
    return f"<{type(value).__name__}>"


def _type_name(kind: Any) -> str | None:
    if kind is None:
        return None
    return getattr(kind, "__qualname__", None) or type(kind).__name__


def _scrub_home(text: str | None) -> str | None:
    if text is None:
        return None
    return text.replace(str(Path.home()), "<HOME>")


def _arguments(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    rows = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        choices = action.choices
        rows.append(
            {
                "options": list(action.option_strings),
                "dest": action.dest,
                "action": type(action).__name__,
                "nargs": action.nargs,
                "required": action.required,
                "choices": None if choices is None else [str(c) for c in choices],
                "metavar": action.metavar
                if action.metavar is None or isinstance(action.metavar, str)
                else list(action.metavar),
                "type": _type_name(action.type),
                "default": _default(action.default),
                "help": _scrub_home(action.help),
            }
        )
    return rows


def _walk(
    parser: argparse.ArgumentParser,
    path: str,
    out: dict[str, Any],
    helps: dict[str, str],
) -> None:
    commands: dict[str, list[str]] = {}
    children: list[tuple[str, argparse.ArgumentParser]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        by_parser: dict[int, list[str]] = {}
        parsers: dict[int, argparse.ArgumentParser] = {}
        for name, sub in action.choices.items():
            by_parser.setdefault(id(sub), []).append(name)
            parsers[id(sub)] = sub
        for key, names in by_parser.items():
            commands[names[0]] = names[1:]
            children.append((names[0], parsers[key]))
    exclusive = [
        {"required": group.required, "dests": [a.dest for a in group._group_actions]}
        for group in parser._mutually_exclusive_groups
    ]
    out[path] = {
        "arguments": _arguments(parser),
        "commands": commands,
        "exclusive": exclusive,
    }
    helps[path] = _scrub_home(parser.format_help()) or ""
    for name, sub in children:
        _walk(sub, f"{path} {name}", out, helps)


def _current(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("COLUMNS", COLUMNS)
    monkeypatch.setattr(sys, "argv", [PROG])
    from openadapt_flow.__main__ import build_parser

    parser = build_parser()
    parser.prog = PROG
    tree: dict[str, Any] = {}
    helps: dict[str, str] = {}
    _walk(parser, PROG, tree, helps)
    return {
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "columns": int(COLUMNS),
        "tree": tree,
        "help": helps,
    }


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_snapshot_is_current_when_regenerating(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("OPENADAPT_WRITE_CLI_SURFACE") != "1":
        pytest.skip("set OPENADAPT_WRITE_CLI_SURFACE=1 to regenerate")
    SNAPSHOT.write_text(
        json.dumps(_current(monkeypatch), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_every_command_and_flag_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    pinned = _snapshot()["tree"]
    current = _current(monkeypatch)["tree"]
    missing = sorted(set(pinned) - set(current))
    assert missing == [], f"commands no longer registered: {missing}"
    added = sorted(set(current) - set(pinned))
    assert added == [], (
        f"new commands {added}: regenerate the snapshot (see this test's docstring)"
    )
    changed = [path for path in sorted(pinned) if pinned[path] != current[path]]
    assert changed == [], (
        f"arguments or subcommands changed under {changed}. A move must keep "
        "every flag; regenerate the snapshot only for a deliberate change."
    )


def test_help_text_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    here = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if snapshot["python"] != here:
        pytest.skip(f"help text pinned on Python {snapshot['python']}, not {here}")
    current = _current(monkeypatch)["help"]
    changed = [
        p for p in sorted(snapshot["help"]) if snapshot["help"][p] != current.get(p)
    ]
    assert changed == [], f"--help text changed for {changed}"


def test_walker_sees_nested_commands_and_aliases() -> None:
    parser = argparse.ArgumentParser(prog="x")
    parser.add_argument("--flag", default=3, help="a flag")
    sub = parser.add_subparsers(dest="cmd")
    one = sub.add_parser("one", aliases=["uno"])
    one.add_argument("path")
    inner = one.add_subparsers(dest="inner")
    deep_parser = inner.add_parser("deep")
    deep_parser.add_argument("--n", type=int, choices=[1, 2])
    either = deep_parser.add_mutually_exclusive_group(required=True)
    either.add_argument("--a", action="store_true")
    either.add_argument("--b", action="store_true")
    tree: dict[str, Any] = {}
    helps: dict[str, str] = {}
    _walk(parser, "x", tree, helps)
    assert sorted(tree) == ["x", "x one", "x one deep"]
    assert tree["x"]["commands"] == {"one": ["uno"]}
    assert tree["x one deep"]["exclusive"] == [{"required": True, "dests": ["a", "b"]}]
    deep = tree["x one deep"]["arguments"][-3]
    assert deep["options"] == ["--n"] and deep["choices"] == ["1", "2"]
    assert deep["type"] == "int"
    assert tree["x"]["arguments"][-1]["default"] == 3
