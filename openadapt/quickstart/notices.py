"""The engine's plaintext-write notices, collapsed to one line.

Flow names each folder it writes unencrypted on stderr, once per folder, and
records it in its plaintext ledger. The quickstart writes seven such folders
of synthetic data; seven warnings through the live display tear it and bury
the story. While the story runs, these notices are collected (in the main
process) or dropped (in Act 2's worker processes, whose folders the ledger
still records), and the finale says it once. Flow's one-time "Created a local
encryption key" line is held for the finale too: the key is created here, in
the main process, before the story and its workers start. Every other stderr
line passes through unchanged.
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

#: Flow's line when it creates this machine's at-rest key on first use.
_KEY_CREATED = re.compile(
    r"^Created a local encryption key .* at (.+?)\. Back it up"
    r"|^WARNING: no OS keychain is available .*device key in (.+?), readable"
)


class Notices(io.TextIOBase):
    """Stands in for stderr: keeps the plaintext notices, forwards the rest."""

    def __init__(self, target: IO[str]) -> None:
        self.target = target
        self.paths: list[str] = []
        self.key_created: Optional[str] = None
        self.keys_created: list[str] = []
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
                created = _KEY_CREATED.match(line)
                if match:
                    self.paths.append(match.group(1) + "\0" + match.group(2))
                elif created:
                    where = created.group(1) or created.group(2)
                    self.keys_created.append(where)
                    self.key_created = ", ".join(self.keys_created)
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
            _ensure_local_key()
            yield notices
    finally:
        notices.flush()
        if previous is None:
            os.environ.pop(QUIET_ENV, None)
        else:
            os.environ[QUIET_ENV] = previous


def _ensure_local_key() -> None:
    """Create Flow's local at-rest key now, so workers find it and say nothing.

    Skipped when a key is configured or encryption is opted out. Any error is
    left for Flow to raise where it needs the key, with its own message.
    """
    try:
        from openadapt_flow import at_rest

        if at_rest.explicit_key() or at_rest.opt_out_reason() is not None:
            return
        at_rest.local_key()
        from openadapt_flow import key_hierarchy

        # The device key that new runs are wrapped to, made once here.
        key_hierarchy.device_keys(create=True)
    except Exception:  # noqa: BLE001 - Flow reports it where the key is used
        return


def quiet_in_worker() -> None:
    """In a worker process: drop plaintext notices when the parent collects them."""

    if os.environ.get(QUIET_ENV) == "1":
        sys.stderr = Notices(sys.stderr)


def plaintext_line(paths: Sequence[str], where: object) -> Optional[str]:
    """The finale's one line, when any folder was written unencrypted."""

    if not paths:
        return None
    if all(item.startswith("a receipt page") for item in paths):
        # Everything else is encrypted; the receipt pages embed the report.
        return (
            f"The receipt pages in {where} are unencrypted: each embeds its "
            "run's report (synthetic data only)"
        )
    return (
        f"The recording, program and run reports in {where} are unencrypted "
        "(synthetic data only); OPENADAPT_FLOW_AT_REST=strict refuses that"
    )


def key_line(path: Optional[str]) -> Optional[str]:
    """The finale's line when this run created the local at-rest key."""

    if not path:
        return None
    return (
        f"Created a local encryption key at {path}; back it up: "
        "what it encrypts can't be read without it"
    )


__all__ = [
    "QUIET_ENV",
    "Notices",
    "collect_notices",
    "key_line",
    "plaintext_line",
    "quiet_in_worker",
]
