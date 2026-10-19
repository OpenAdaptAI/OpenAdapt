"""CAPABILITIES.md at the repository root lists every openadapt-flow command.

Reviews of OpenAdapt keep missing capabilities that ship because nothing tied
the capability map to the code. A new command fails this test until it is
listed, by capability, in CAPABILITIES.md.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from openadapt_flow.__main__ import build_parser

MAP = Path(__file__).resolve().parents[3] / "CAPABILITIES.md"


def _verbs() -> list[str]:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("openadapt-flow has no subcommands")


@pytest.mark.skipif(not MAP.exists(), reason="runs from the monorepo checkout")
def test_every_command_is_listed_in_the_capability_map() -> None:
    text = MAP.read_text(encoding="utf-8")
    section = text[text.index("## Every command, by capability") :]
    listed = set(re.findall(r"`([a-z][a-z-]*)`", section))
    missing = [verb for verb in _verbs() if verb not in listed]
    assert not missing, (
        f"add {missing} to 'Every command, by capability' in CAPABILITIES.md"
    )
