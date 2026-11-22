"""Run ``openadapt-flow run`` for a readiness task without putting its
arguments on a command line.

``ps`` shows every process's arguments to every local user, and the
program's arguments carry the DevTools relay URL with the run's token and
the task's inputs. :class:`~openadapt_flow.readiness.launcher.ProgramLauncher`
writes them to a 0600 JSON file in the task's run folder and starts::

    python -m openadapt_flow.readiness.program_child <file>

which reads the file, deletes it, and calls the Flow CLI in this process
with those arguments. The file holds ``{"schema", "argv"}``; ``argv`` must
start with ``run``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

SCHEMA = "openadapt.readiness-program-args/v1"


def write_args(path: Path, argv: list[str]) -> Path:
    """Write ``argv`` for the child to ``path`` (0600)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # a new file, so its mode is 0600 whatever was there
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"schema": SCHEMA, "argv": list(argv)}, fh)
    return path


def read_args(path: Path) -> list[str]:
    """The arguments, once: the file is deleted after it is read. Refuses a
    file others can read or one in another shape."""

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit("program arguments: the file is readable by others; refused")
    doc = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    argv = doc.get("argv") if isinstance(doc, dict) else None
    if doc.get("schema") != SCHEMA or not isinstance(argv, list) or not argv or argv[0] != "run":
        raise SystemExit("program arguments: not a readiness program run; refused")
    return [str(a) for a in argv]


def main(args: list[str]) -> int:
    if len(args) != 1:
        raise SystemExit("usage: python -m openadapt_flow.readiness.program_child <file>")
    argv = read_args(Path(args[0]))
    from openadapt_flow.__main__ import main as flow_main

    return int(flow_main(argv) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
