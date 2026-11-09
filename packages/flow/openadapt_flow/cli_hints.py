"""Spell next-step commands the way the person invoked the engine.

The OpenAdapt launcher runs this engine in-process (``openadapt flow <verb>``,
``openadapt quickstart``). A hint that then says
``openadapt-flow <verb>`` names a program the person never typed. Both
spellings work when the launcher is installed, so this only picks the one
that matches what the person used.

Under ``uvx`` the engine runs from a throwaway environment in uv's cache, so
the person's shell may have no ``openadapt`` at all, or an older install that
lacks the verb. Hints then repeat the ``uvx`` form, which reaches this same
version.
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


def uvx_ephemeral(prefix: Optional[str] = None) -> bool:
    """True when this interpreter is a ``uvx`` throwaway environment.

    ``uvx`` (``uv tool run``) builds the environment under uv's cache, in an
    ``archive-v<N>`` directory. ``uv tool install`` and ordinary virtualenvs
    live elsewhere and put the command on PATH, so they keep the plain form.
    """

    path = (sys.prefix if prefix is None else prefix).replace("\\", "/")
    return "/uv/archive-v" in path or "/uv/cache/archive-v" in path


def _uvx(prefix: Optional[str]) -> str:
    major, minor = sys.version_info[:2]
    return f"uvx --python {major}.{minor} " if uvx_ephemeral(prefix) else ""


def command(
    verb_and_args: str,
    argv: Optional[Sequence[str]] = None,
    prefix: Optional[str] = None,
) -> str:
    """Return ``verb_and_args`` prefixed with :func:`command_prefix`.

    Under ``uvx`` the launcher form becomes ``uvx --python X.Y openadapt flow``
    and the engine form ``uvx --python X.Y openadapt-flow``.
    """

    return f"{_uvx(prefix)}{command_prefix(argv)} {verb_and_args}"


def launcher_command(rest: str, prefix: Optional[str] = None) -> str:
    """Return a launcher command (``openadapt <rest>``), in ``uvx`` form if needed."""

    return f"{_uvx(prefix)}{LAUNCHER_PREFIX.split()[0]} {rest}"


__all__ = [
    "ENGINE_PREFIX",
    "LAUNCHER_PREFIX",
    "command",
    "command_prefix",
    "launcher_command",
    "uvx_ephemeral",
]
