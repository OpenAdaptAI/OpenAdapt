"""Current task examples reach help; generated text follows its real generator."""

from __future__ import annotations

import argparse
import re
import runpy
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from openadapt import cli

ROOT = Path(__file__).resolve().parents[1]


def _task_examples() -> list[list[str]]:
    text = (ROOT / "docs/cli.md").read_text(encoding="utf-8")
    section = text.split("## Command tasks\n", 1)[1].split("## First run\n", 1)[0]
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", section, flags=re.DOTALL):
        for line in block.splitlines():
            tokens = shlex.split(line)
            if tokens:
                assert tokens[0] == "openadapt"
                commands.append(tokens[1:])
    assert commands
    return commands


@pytest.mark.parametrize("arguments", _task_examples())
def test_documented_task_examples_reach_their_installed_help(arguments):
    # Invoke only help. No demonstration, service, login or task is executed.
    help_arguments = [*arguments]
    if "--help" not in help_arguments:
        help_arguments.append("--help")
    result = CliRunner().invoke(cli.main, help_arguments)
    assert result.exit_code == 0, result.output
    assert result.stdout


def test_wrap_help_is_discovered_as_an_actual_launcher_command():
    from openadapt_flow.__main__ import build_parser

    launcher = CliRunner().invoke(cli.main, ["--help"])
    assert launcher.exit_code == 0, launcher.output
    names = [
        line.split()[0]
        for line in launcher.stdout.split("Commands:\n", 1)[1].splitlines()
        if line.startswith("  ")
    ]
    assert "wrap" in names
    assert cli.main.commands["wrap"] is cli.wrap
    # Wrap retains its existing native entrypoint; discovery must not invent
    # an executable `openadapt flow wrap` command in the engine parser.
    parser = build_parser()
    choices = next(
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "wrap" not in choices
    assert len(names) == 16


def test_review_brief_matches_the_unmodified_canonical_generator():
    # Use the actual generator's pure build function. Never splice or approve
    # a manually copied README section in the generated review brief.
    generator = runpy.run_path(
        str(ROOT / "scripts/build_llms_full.py"), run_name="llms_source_contract"
    )
    assert (ROOT / "llms-full.txt").read_text(encoding="utf-8") == generator[
        "build"
    ](), (
        "Run python scripts/build_llms_full.py, then --check in the qualified environment"
    )
