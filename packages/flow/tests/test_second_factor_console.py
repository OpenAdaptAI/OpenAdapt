"""The terminal prompt's line reader, on every platform.

:class:`WindowsConsole` takes injected ``kbhit``, ``getwch`` and ``sleep``,
so its deadline, discard and key handling run here on a fake keyboard with a
fake clock: no console, no person and no real waiting. The last test runs
only on Windows, against the real ``msvcrt``.
"""

from __future__ import annotations

import io
import sys
from typing import Optional

import pytest

from openadapt_flow import second_factor as sf
from openadapt_flow.second_factor_terminal import (
    TerminalSource,
    WindowsConsole,
    read_line,
)

CODE = "482913"


class Keyboard:
    """Keys that arrive at given times on a fake clock."""

    def __init__(self, keys: list[tuple[float, str]] = ()) -> None:  # type: ignore[assignment]
        self.now = 0.0
        self.keys = sorted(keys, key=lambda k: k[0])
        self.taken: list[str] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def kbhit(self) -> bool:
        return bool(self.keys) and self.keys[0][0] <= self.now

    def getwch(self) -> str:
        assert self.kbhit(), "getwch would block"
        key = self.keys.pop(0)[1]
        self.taken.append(key)
        return key

    def type(self, at: float, text: str) -> None:
        self.keys = sorted([*self.keys, *((at, c) for c in text)], key=lambda k: k[0])

    def console(self) -> WindowsConsole:
        return WindowsConsole(kbhit=self.kbhit, getwch=self.getwch, sleep=self.sleep)


def _ask(kb: Keyboard, kind: str = "code", timeout_s: float = 30.0) -> Optional[str]:
    source = TerminalSource(None, io.StringIO(), console=kb.console(), clock=kb.clock)
    return source.obtain(sf.Request(kind, "s", timeout_s), heartbeat=lambda: None)


def test_a_code_is_read_key_by_key_and_never_shown() -> None:
    kb = Keyboard()
    kb.type(1.0, "482 913\r")
    out = io.StringIO()
    source = TerminalSource(None, out, console=kb.console(), clock=kb.clock)
    reply = source.obtain(sf.Request("code", "s", 30.0), heartbeat=lambda: None)
    assert reply == "482 913" and sf.normalize_code(reply) == CODE
    assert CODE[:3] not in out.getvalue() and "not shown" in out.getvalue()


def test_keys_typed_before_the_prompt_are_discarded() -> None:
    kb = Keyboard([(0.0, c) for c in "111111\r"])
    kb.type(2.0, f"{CODE}\r")
    assert _ask(kb) == CODE


def test_the_deadline_holds_and_left_over_keys_are_discarded() -> None:
    kb = Keyboard()
    kb.type(1.0, "4829")  # part of a code, then nothing
    kb.type(40.0, "13\r")  # after the deadline
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _ask(kb, timeout_s=30.0)
    assert refused.value.code == "second_factor_timeout"
    assert 30.0 <= kb.now < 31.0  # gave up at the deadline, not later
    kb.now = 41.0
    # What arrived late is still queued for nobody: the next read drops it
    # before its prompt, so it can't answer or reach the shell.
    kb.type(42.0, f"{CODE}\r")
    assert _ask(kb) == CODE


def test_ctrl_c_interrupts_and_still_discards_what_was_typed() -> None:
    kb = Keyboard()
    kb.type(1.0, "48\x03")
    kb.type(1.0, "2913\r")
    with pytest.raises(KeyboardInterrupt):
        _ask(kb)
    assert not kb.kbhit()  # the rest was dropped, not left for the shell


def test_function_keys_backspace_and_an_over_long_answer() -> None:
    kb = Keyboard()
    kb.type(1.0, "\xe0H48\x00;27\b913\r")  # arrow, F1, a corrected digit
    assert _ask(kb) == CODE
    kb = Keyboard()
    kb.type(1.0, "9" * 300 + "\r")
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _ask(kb)
    assert refused.value.code == "second_factor_invalid"
    assert not kb.kbhit()


def test_an_approval_reply_is_shown_and_can_decline() -> None:
    kb = Keyboard()
    kb.type(1.0, "n\r")
    out = io.StringIO()
    source = TerminalSource(None, out, console=kb.console(), clock=kb.clock)
    with pytest.raises(sf.SecondFactorRefused) as refused:
        source.obtain(sf.Request("approval", "s", 30.0), heartbeat=lambda: None)
    assert refused.value.code == "second_factor_declined"
    assert out.getvalue().rstrip("\n").endswith("n")


def test_a_failing_console_is_unavailable() -> None:
    class Broken(WindowsConsole):
        def begin(self, *, hidden: bool) -> None:
            raise OSError("no console")

    kb = Keyboard()
    console = Broken(kbhit=kb.kbhit, getwch=kb.getwch, sleep=kb.sleep)
    with pytest.raises(sf.SecondFactorRefused) as refused:
        read_line(
            console,
            io.StringIO(),
            "? ",
            hidden=True,
            deadline=10.0,
            timeout_s=10.0,
            clock=kb.clock,
        )
    assert refused.value.code == "second_factor_unavailable"


@pytest.mark.skipif(sys.platform != "win32", reason="needs the Windows console")
def test_the_real_windows_console_times_out_without_a_key() -> None:
    import time

    console = WindowsConsole()  # the real msvcrt.kbhit and getwch
    started = time.monotonic()
    with pytest.raises(sf.SecondFactorRefused) as refused:
        read_line(
            console,
            io.StringIO(),
            "? ",
            hidden=True,
            deadline=time.monotonic() + 0.5,
            timeout_s=0.5,
        )
    assert refused.value.code == "second_factor_timeout"
    assert time.monotonic() - started < 5.0
