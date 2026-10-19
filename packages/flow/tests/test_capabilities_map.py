"""CAPABILITIES.md at the repository root lists every openadapt-flow command.

Reviews of OpenAdapt keep missing capabilities that ship because nothing tied
the capability map to the code. A new command fails this test until it has a
file under packages/flow/docs/commands/<group>/ and the list in CAPABILITIES.md
is regenerated with scripts/build_command_map.py. One file per command keeps
two pull requests that each add a command from editing the same line.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from openadapt_flow.__main__ import build_parser

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "build_command_map.py"
pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(), reason="runs from the monorepo checkout"
)


def _verbs() -> list[str]:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("openadapt-flow has no subcommands")


def _command_map():
    spec = importlib.util.spec_from_file_location("build_command_map", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_command_has_a_capability_file() -> None:
    documented = _command_map().documented_verbs()
    missing = sorted(set(_verbs()) - set(documented))
    assert not missing, (
        f"add packages/flow/docs/commands/<group>/<verb>.md for {missing}, "
        "then run: python scripts/build_command_map.py"
    )
    stale = sorted(set(documented) - set(_verbs()))
    assert not stale, f"no such openadapt-flow command; remove the files for {stale}"
    duplicated = sorted({v for v in documented if documented.count(v) > 1})
    assert not duplicated, f"each command belongs to one capability: {duplicated}"


def test_capability_map_command_list_is_current() -> None:
    command_map = _command_map()
    text = command_map.MAP.read_text(encoding="utf-8")
    assert command_map.splice(text, command_map.render()) == text, (
        "run: python scripts/build_command_map.py"
    )
