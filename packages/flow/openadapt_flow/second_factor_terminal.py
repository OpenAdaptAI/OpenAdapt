"""Ask for a sign-in's second factor at the run's own terminal.

:class:`TerminalSource` is the last source :func:`openadapt_flow.second_factor.source_from_env`
tries: only when no control plane and no command is set, stdin and stderr are
both a terminal, and ``OPENADAPT_SECOND_FACTOR_PROMPT`` is not ``off``. A
service, a hosted sandbox, an MCP server or CI has no terminal and never
prompts.

One line reader (:func:`read_line`) serves every platform, over a small
:class:`Console` interface:

- :class:`PosixConsole` (``termios``): echo is turned off for a code, and
  ``select`` waits on the terminal's descriptor.
- :class:`WindowsConsole` (``msvcrt``): ``getwch`` reads one key at a time
  without echo, and ``kbhit`` is polled in short slices.

Either way the read runs in the calling thread under the step's deadline, so
nothing keeps reading after the step gives up. Anything typed before the
prompt is discarded, so an earlier keystroke can't answer it. When the read
ends, whatever ends it (an answer, the deadline, an over-long answer, Ctrl-C
or a failed read), input it did not take is discarded and the terminal's
normal input mode is restored in ``finally``, so the shell never reads part
of a code.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import Any, Optional, Protocol

from openadapt_flow.second_factor import Request, SecondFactorRefused

__all__ = [
    "PROMPT_OFF",
    "Console",
    "PosixConsole",
    "TerminalSource",
    "WindowsConsole",
    "read_line",
]

#: Values of ``OPENADAPT_SECOND_FACTOR_PROMPT`` that turn the prompt off.
PROMPT_OFF = ("off", "0", "false")
#: The longest answer read from the terminal; a code is far shorter.
_MAX_LINE = 256
#: How often the Windows console is polled for a key.
_POLL_S = 0.05


class Console(Protocol):
    """The terminal primitives :func:`read_line` needs."""

    #: Whether typed characters are shown by the terminal itself.
    echoes: bool
    #: Whether ``\x00`` or ``\xe0`` starts a two-character function or
    #: arrow key (the Windows console); elsewhere they are ordinary input.
    prefixed_keys: bool

    def begin(self, *, hidden: bool) -> None:
        """Enter reading mode (no echo when ``hidden``); drop pending input."""

    def wait(self, timeout_s: float) -> bool:
        """Whether a character is ready within ``timeout_s``."""

    def read(self) -> str:
        """Characters that are ready; ``""`` at end of input."""

    def end(self) -> None:
        """Drop input not taken and restore normal input. Never raises."""


def read_line(
    console: Console,
    out: Any,
    prompt: str,
    *,
    hidden: bool,
    deadline: float,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """One line from ``console`` before ``deadline``, or a refusal."""

    try:
        try:
            console.begin(hidden=hidden)
        except Exception:  # noqa: BLE001 - an unusable terminal is unavailable
            raise SecondFactorRefused(
                "second_factor_unavailable", "the terminal can't be read"
            ) from None
        _write(out, prompt)
        line: list[str] = []
        skip = False
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise SecondFactorRefused(
                    "second_factor_timeout",
                    f"no second factor within {timeout_s:g} s; nothing was typed",
                )
            if not console.wait(remaining):
                continue
            chunk = console.read()
            if not chunk:
                raise SecondFactorRefused(
                    "second_factor_unavailable",
                    "the terminal closed; nothing was typed",
                )
            for char in chunk:
                if skip:  # the second half of a Windows function or arrow key
                    skip = False
                    continue
                if console.prefixed_keys and char in ("\x00", "\xe0"):
                    skip = True
                    continue
                if char == "\x03":
                    raise KeyboardInterrupt
                if char in ("\r", "\n"):
                    return "".join(line)
                if char in ("\b", "\x7f"):
                    if line:
                        line.pop()
                        if not hidden and not console.echoes:
                            _write(out, "\b \b")
                    continue
                line.append(char)
                if not hidden and not console.echoes:
                    _write(out, char)  # an approval reply is shown, a code never
                if len(line) > _MAX_LINE:
                    raise SecondFactorRefused(
                        "second_factor_invalid", "the answer is too long"
                    )
    except OSError:
        raise SecondFactorRefused(
            "second_factor_unavailable", "the terminal can't be read"
        ) from None
    finally:
        console.end()
        _write(out, "\n")


def _write(out: Any, text: str) -> None:
    try:
        out.write(text)
        out.flush()
    except (OSError, ValueError, AttributeError):
        pass


class PosixConsole:
    """A POSIX terminal on file descriptor ``fd`` (``termios``)."""

    echoes = True
    prefixed_keys = False

    def __init__(self, fd: int) -> None:
        import codecs

        self._fd = fd
        self._saved: Optional[list[Any]] = None
        # A multi-byte character split across two reads still decodes.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def begin(self, *, hidden: bool) -> None:
        import termios

        self._saved = termios.tcgetattr(self._fd)
        quiet = list(self._saved)
        if hidden:
            quiet[3] = quiet[3] & ~termios.ECHO
        termios.tcsetattr(self._fd, termios.TCSANOW, quiet)
        # Input only: waiting for output to drain could block on a terminal
        # nobody reads.
        termios.tcflush(self._fd, termios.TCIFLUSH)

    def wait(self, timeout_s: float) -> bool:
        import select

        ready, _, _ = select.select([self._fd], [], [], timeout_s)
        return bool(ready)

    def read(self) -> str:
        while True:
            raw = os.read(self._fd, _MAX_LINE)
            if not raw:
                return ""
            text = self._decoder.decode(raw)
            if text:  # empty: half a character; its rest is in the same line
                return text

    def end(self) -> None:
        try:
            import termios
        except ImportError:
            return
        try:
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except (termios.error, OSError):
            pass
        if self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved)
            except (termios.error, OSError):
                pass
            self._saved = None


class WindowsConsole:
    """The Windows console (``msvcrt``). ``getwch`` reads without echo.

    ``msvcrt`` changes no console mode, so there is no mode to restore;
    ending a read drops the keys it did not take. ``kbhit``, ``getwch`` and
    ``sleep`` are injectable, so the logic is tested on every platform.
    """

    echoes = False
    prefixed_keys = True
    #: How long ending a read keeps dropping keys (a long paste included).
    drain_s = 0.5

    def __init__(
        self,
        *,
        kbhit: Optional[Callable[[], bool]] = None,
        getwch: Optional[Callable[[], str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if kbhit is None or getwch is None:
            import msvcrt  # type: ignore[import-not-found,unused-ignore]

            kbhit = kbhit or msvcrt.kbhit  # type: ignore[attr-defined,unused-ignore]
            getwch = getwch or msvcrt.getwch  # type: ignore[attr-defined,unused-ignore]
        self._kbhit = kbhit
        self._getwch = getwch
        self._sleep = sleep
        self._clock = clock

    def _drain(self) -> None:
        # Bounded by time, not by a count, so a long paste is dropped whole
        # and a key held down can't hang the run.
        until = self._clock() + self.drain_s
        while self._kbhit() and self._clock() < until:
            self._getwch()

    def begin(self, *, hidden: bool) -> None:
        self._drain()

    def wait(self, timeout_s: float) -> bool:
        # Measured on the clock: a sleep that oversleeps can't stretch the
        # deadline.
        until = self._clock() + timeout_s
        while True:
            if self._kbhit():
                return True
            left = until - self._clock()
            if left <= 0:
                return False
            self._sleep(min(_POLL_S, left))

    def read(self) -> str:
        return self._getwch()

    def end(self) -> None:
        try:
            self._drain()
        except Exception:  # noqa: BLE001 - ending never raises
            pass


class TerminalSource:
    """Ask the person at the run's own terminal (no echo for a code).

    ``fd_in`` is a POSIX terminal descriptor; pass ``console`` instead for
    another :class:`Console` (the Windows console, or a test double).
    """

    def __init__(
        self,
        fd_in: Optional[int],
        out: Any,
        *,
        console: Optional[Console] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if console is None:
            if fd_in is None:
                raise ValueError("a terminal source needs a descriptor or a console")
            console = PosixConsole(fd_in)
        self._console = console
        self._out = out
        self._clock = clock

    @classmethod
    def open(cls) -> Optional["TerminalSource"]:
        """The run's terminal, or None when it has none."""

        try:
            if not (sys.stdin.isatty() and sys.stderr.isatty()):
                return None
            if sys.platform == "win32":
                return cls(None, sys.stderr, console=WindowsConsole())
            import termios  # noqa: F401 - POSIX terminals

            return cls(sys.stdin.fileno(), sys.stderr)
        except (AttributeError, ValueError, OSError, ImportError):
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
        return read_line(
            self._console,
            self._out,
            prompt,
            hidden=hidden,
            deadline=deadline,
            timeout_s=timeout_s,
            clock=self._clock,
        )
