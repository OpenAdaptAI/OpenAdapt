"""Start the engine's child processes without forking the engine.

On macOS, Python normally starts a child by forking and then, in the forked
copy, closing every inherited descriptor before it runs the program. If Tcl is
loaded in the engine (tkinter, which a dependency can import), Tcl starts a
thread in each forked copy, and on macOS 26 that copy can then stop for good
inside ``close()``. The engine would wait for it inside ``Popen`` with no
timeout that applies.

Python starts the child with ``posix_spawn`` instead, with no forked copy at
all, when the program path is absolute and ``close_fds`` is false. Python opens
its own descriptors close-on-exec, so the child still inherits only its
standard streams. ``run`` and ``popen`` set that up and otherwise behave like
``subprocess.run`` and ``subprocess.Popen``. Windows has no fork and is left
alone.

Do not pass ``cwd``, ``start_new_session``, ``preexec_fn`` or ``pass_fds``
through these functions: each one makes Python fork again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

_FORKING_OPTIONS = ("cwd", "start_new_session", "preexec_fn", "pass_fds")


def _prepare(command: Sequence[Any], options: dict[str, Any]) -> list[Any]:
    arguments = list(command)
    if os.name != "posix":
        return arguments
    for name in _FORKING_OPTIONS:
        if options.get(name):
            raise ValueError(f"{name} would make Python fork to start {arguments[0]!r}")
    options.setdefault("close_fds", False)
    program = os.fspath(arguments[0])
    if not os.path.dirname(program):
        environment: Mapping[str, str] | None = options.get("env")
        search = environment.get("PATH") if environment is not None else None
        found = shutil.which(program, path=search)
        if found is None:
            # Python would fork to find out the same thing.
            raise FileNotFoundError(2, "No such file or directory", program)
        arguments[0] = os.path.abspath(found)
    return arguments


def run(command: Sequence[Any], **options: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a command list, started without a fork on POSIX."""

    return subprocess.run(_prepare(command, options), **options)


def popen(command: Sequence[Any], **options: Any) -> subprocess.Popen:
    """``subprocess.Popen`` for a command list, started without a fork on POSIX."""

    return subprocess.Popen(_prepare(command, options), **options)
