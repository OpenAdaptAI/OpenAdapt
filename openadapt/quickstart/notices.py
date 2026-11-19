"""The engine's plaintext-write notices, collapsed to one line.

Flow names each folder it writes unencrypted on stderr, once per folder, and
records it in its plaintext ledger. The quickstart writes seven such folders
of synthetic data; seven warnings through the live display tear it and bury
the story. While the story runs, these notices are collected (in the main
process) or dropped (in Act 2's worker processes, whose folders the ledger
still records), and the finale says it once. Every other stderr line passes
through unchanged.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import threading
from typing import IO, Iterator, Optional, Sequence

#: Set while notices are collected, so worker processes drop theirs.
QUIET_ENV = "OPENADAPT_QUICKSTART_PLAINTEXT_NOTICES"

#: "... in plaintext at PATH: ..." per folder, or "... in plaintext at rest
#: ('reason')" once under Flow's recorded opt-out.
_PLAINTEXT = re.compile(r"^WARNING: writing (.+?) in plaintext at (rest \(|.+?: )")


class Notices(io.TextIOBase):
    """Stands in for stderr: keeps the plaintext notices, forwards the rest."""

    def __init__(self, target: IO[str]) -> None:
        self.target = target
        self.paths: list[str] = []
        self._partial = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                match = _PLAINTEXT.match(line)
                if match:
                    self.paths.append(match.group(2))
                else:
                    self.target.write(line + "\n")
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._partial:
                self.target.write(self._partial)
                self._partial = ""
        self.target.flush()

    def isatty(self) -> bool:
        isatty = getattr(self.target, "isatty", None)
        return bool(callable(isatty) and isatty())


@contextlib.contextmanager
def collect_notices(target: Optional[IO[str]] = None) -> Iterator[Notices]:
    """Collect plaintext notices from this process; workers started inside drop theirs."""

    notices = Notices(target or sys.stderr)
    previous = os.environ.get(QUIET_ENV)
    os.environ[QUIET_ENV] = "1"
    try:
        with contextlib.redirect_stderr(notices):
            yield notices
    finally:
        notices.flush()
        if previous is None:
            os.environ.pop(QUIET_ENV, None)
        else:
            os.environ[QUIET_ENV] = previous


def quiet_in_worker() -> None:
    """In a worker process: drop plaintext notices when the parent collects them."""

    if os.environ.get(QUIET_ENV) == "1":
        sys.stderr = Notices(sys.stderr)


def plaintext_line(paths: Sequence[str], where: object) -> Optional[str]:
    """The finale's one line, when any folder was written unencrypted."""

    if not paths:
        return None
    return (
        f"{where} is written unencrypted (synthetic data only); "
        "OPENADAPT_FLOW_AT_REST=strict refuses that"
    )


__all__ = [
    "QUIET_ENV",
    "Notices",
    "collect_notices",
    "plaintext_line",
    "quiet_in_worker",
]
