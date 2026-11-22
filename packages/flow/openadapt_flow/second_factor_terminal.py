"""Ask for a sign-in's second factor at the run's own terminal.

:class:`TerminalSource` is the last source :func:`openadapt_flow.second_factor.source_from_env`
tries: only when no control plane and no command is set, stdin and stderr are
both a TTY, and ``OPENADAPT_SECOND_FACTOR_PROMPT`` is not ``off``. A service,
a hosted sandbox, an MCP server or CI has no terminal and never prompts.

The read runs in the calling thread under the step's deadline, with
``select`` on the terminal's descriptor; nothing keeps reading after the
step gives up. For a code, echo is turned off for that read only and turned
back on in ``finally``, whether the person answered, the deadline passed or
the read failed. Input typed before the prompt is discarded, so an earlier
keystroke can't answer it, and so is input left over when the read ends, so
the shell never reads part of a code.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import Any, Optional

from openadapt_flow.second_factor import Request, SecondFactorRefused

__all__ = ["PROMPT_OFF", "TerminalSource"]

#: Values of ``OPENADAPT_SECOND_FACTOR_PROMPT`` that turn the prompt off.
PROMPT_OFF = ("off", "0", "false")
#: The longest answer read from the terminal; a code is far shorter.
_MAX_LINE = 256


class TerminalSource:
    """Ask the person at the run's own terminal (no echo for a code)."""

    def __init__(
        self,
        fd_in: int,
        out: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fd = fd_in
        self._out = out
        self._clock = clock

    @classmethod
    def open(cls) -> Optional["TerminalSource"]:
        """The run's terminal, or None when it has none (or no termios)."""

        try:
            import termios  # noqa: F401 - POSIX terminals only
        except ImportError:
            return None
        try:
            if not (sys.stdin.isatty() and sys.stderr.isatty()):
                return None
            return cls(sys.stdin.fileno(), sys.stderr)
        except (AttributeError, ValueError, OSError):
            return None

    def obtain(
        self, request: Request, *, heartbeat: Callable[[], None]
    ) -> Optional[str]:
        deadline = self._clock() + request.timeout_s
        if request.kind == "approval":
            reply = self._ask(
                "Approve the sign-in on your device, then press Enter "
                "(type n and Enter to decline): ",
                hidden=False,
                deadline=deadline,
                timeout_s=request.timeout_s,
            )
            if reply.strip().lower() in ("n", "no"):
                raise SecondFactorRefused(
                    "second_factor_declined", "the person declined; nothing was typed"
                )
            return None
        reply = self._ask(
            "Sign-in code (not shown; leave empty to decline): ",
            hidden=True,
            deadline=deadline,
            timeout_s=request.timeout_s,
        )
        if not reply.strip():
            raise SecondFactorRefused(
                "second_factor_declined", "the person declined; nothing was typed"
            )
        return reply

    def _ask(
        self, prompt: str, *, hidden: bool, deadline: float, timeout_s: float
    ) -> str:
        import select
        import termios

        try:
            saved = termios.tcgetattr(self._fd)
        except (termios.error, OSError):
            raise SecondFactorRefused(
                "second_factor_unavailable", "the terminal can't be read"
            ) from None
        try:
            quiet = list(saved)
            if hidden:
                quiet[3] = quiet[3] & ~termios.ECHO
            termios.tcsetattr(self._fd, termios.TCSANOW, quiet)
            # Drop anything typed before the prompt (input only: waiting for
            # output to drain could block on a terminal nobody reads).
            termios.tcflush(self._fd, termios.TCIFLUSH)
            self._out.write(prompt)
            self._out.flush()
            line = b""
            while b"\n" not in line:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise SecondFactorRefused(
                        "second_factor_timeout",
                        f"no second factor within {timeout_s:g} s; nothing was typed",
                    )
                ready, _, _ = select.select([self._fd], [], [], remaining)
                if not ready:
                    continue
                chunk = os.read(self._fd, _MAX_LINE)
                if not chunk:
                    raise SecondFactorRefused(
                        "second_factor_unavailable",
                        "the terminal closed; nothing was typed",
                    )
                line += chunk
                if len(line) > _MAX_LINE:
                    raise SecondFactorRefused(
                        "second_factor_invalid", "the answer is too long"
                    )
            return line.split(b"\n", 1)[0].decode("utf-8", "replace")
        except OSError:
            raise SecondFactorRefused(
                "second_factor_unavailable", "the terminal can't be read"
            ) from None
        finally:
            try:
                # Whatever the person typed and the read did not take (a
                # timeout, an over-long answer, an interrupt) is discarded
                # before echo comes back, so the shell never reads it.
                termios.tcflush(self._fd, termios.TCIFLUSH)
            except (termios.error, OSError):
                pass
            try:
                termios.tcsetattr(self._fd, termios.TCSANOW, saved)
            except (termios.error, OSError):
                pass
            try:
                self._out.write("\n")
                self._out.flush()
            except (OSError, ValueError):
                pass
