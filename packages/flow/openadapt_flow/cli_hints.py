"""Spell next-step commands the way the person invoked the engine.

The OpenAdapt launcher runs this engine in-process (``openadapt flow <verb>``,
``openadapt quickstart``). A hint that then says
``openadapt-flow <verb>`` names a program the person never typed. Both
spellings work when the launcher is installed, so this only picks the one
that matches what the person used.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

#: The engine's own executable.
ENGINE_PREFIX = "openadapt-flow"
#: The launcher's forwarding form.
LAUNCHER_PREFIX = "openadapt flow"


def command_prefix(argv: Optional[Sequence[str]] = None) -> str:
    """Return ``"openadapt flow"`` under the launcher, else ``"openadapt-flow"``.

    The launcher's console script is named ``openadapt``, so its process
    ``argv[0]`` ends in ``openadapt`` (``openadapt.exe`` on Windows). Anything
    else, including ``python -m openadapt_flow`` and test runners, keeps the
    engine spelling.
    """

    args = sys.argv if argv is None else argv
    if not args:
        return ENGINE_PREFIX
    # Split on both separators so a Windows path reads the same on any host.
    name = str(args[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return LAUNCHER_PREFIX if name == "openadapt" else ENGINE_PREFIX


def command(verb_and_args: str, argv: Optional[Sequence[str]] = None) -> str:
    """Return ``verb_and_args`` prefixed with :func:`command_prefix`."""

    return f"{command_prefix(argv)} {verb_and_args}"


__all__ = ["ENGINE_PREFIX", "LAUNCHER_PREFIX", "command", "command_prefix"]
