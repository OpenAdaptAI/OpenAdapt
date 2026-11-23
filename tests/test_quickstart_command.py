"""The documented first command is one spelling everywhere it is printed.

``uvx --python 3.12 openadapt@latest quickstart`` (with ``--break-it`` where
the break is shown) gets the newest launcher on a Python the engine supports,
with nothing installed first. The README (and so the PyPI description),
llms-full.txt and the launcher's own Python fix all use it.
"""

from __future__ import annotations

import re
import runpy
from pathlib import Path

from openadapt import cli

ROOT = Path(__file__).resolve().parents[1]
COMMAND = "uvx --python 3.12 openadapt@latest quickstart"


def test_the_launcher_names_the_documented_command():
    assert cli.QUICKSTART_COMMAND == COMMAND
    assert f"  {COMMAND}\n" in cli._python_fix()


def _texts() -> dict[str, str]:
    # llms-full.txt as its generator writes it: a pull request does not
    # regenerate the committed copy (the refresh workflow does).
    generator = runpy.run_path(str(ROOT / "scripts/build_llms_full.py"))
    return {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "llms-full.txt": generator["build"](),
    }


def test_readme_and_llms_full_use_the_documented_command():
    for name, text in _texts().items():
        assert f"{COMMAND} --break-it" in text, name
        # No other uvx spelling of the quickstart.
        for match in re.finditer(r"uvx [^\n`]*?quickstart", text):
            assert match.group(0) == COMMAND, (name, match.group(0))
