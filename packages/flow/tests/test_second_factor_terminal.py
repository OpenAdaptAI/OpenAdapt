"""Asking for a second factor at the run's own terminal, on a real pty.

Each test opens a pseudo-terminal: the source reads the follower side, and
the test plays the person by writing to the leader side once the prompt has
been written. Nothing here needs a real terminal or a person.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from typing import Any, Iterator, Optional

import pytest

from openadapt_flow import second_factor as sf

termios = pytest.importorskip("termios")
pty = pytest.importorskip("pty")

CODE = "482913"
#: Generous: the person answers as soon as the prompt shows, so a slow
#: machine only makes a test slower, never wrong.
WAIT_S = 10.0


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in sf.SOURCE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def terminal() -> Iterator[tuple[int, int]]:
    leader, follower = pty.openpty()
    yield leader, follower
    os.close(leader)
    os.close(follower)


class _Screen(io.StringIO):
    """The prompt side of the terminal; counts prompts as they are written."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts = threading.Semaphore(0)

    def write(self, text: str) -> int:
        written = super().write(text)
        if text.endswith(": "):
            self.prompts.release()
        return written


def _source(follower: int) -> tuple[Any, _Screen]:
    from openadapt_flow.second_factor_terminal import TerminalSource

    screen = _Screen()
    return TerminalSource(follower, screen), screen


def _person(
    leader: int,
    screen: _Screen,
    text: str,
    *,
    sample: Optional[list[bool]] = None,
    follower: int = -1,
) -> threading.Thread:
    """Answer the next prompt once it shows, sampling echo first if asked."""

    def answer() -> None:
        if not screen.prompts.acquire(timeout=WAIT_S):
            return
        if sample is not None:
            sample.append(_echo_on(follower))
        os.write(leader, text.encode())

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


def _echo_on(follower: int) -> bool:
    return bool(termios.tcgetattr(follower)[3] & termios.ECHO)


def _ask(source: Any, kind: str = "code", timeout_s: float = WAIT_S) -> Optional[str]:
    return source.obtain(sf.Request(kind, "s", timeout_s), heartbeat=lambda: None)


def test_a_code_is_read_without_echo_and_echo_comes_back(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    assert _echo_on(follower)
    source, screen = _source(follower)
    seen: list[bool] = []
    _person(leader, screen, "482 913\n", sample=seen, follower=follower)
    code = sf.obtain("code", "step_004", source=source, heartbeat=lambda: None)
    assert code is not None and code.consume() == CODE
    assert seen == [False]  # echo was off while the person typed
    assert _echo_on(follower)  # and on again afterwards
    assert "not shown" in screen.getvalue() and CODE not in screen.getvalue()


def test_a_timeout_restores_echo_and_the_next_prompt_gets_its_answer(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    source, screen = _source(follower)
    threads = threading.active_count()
    started = time.monotonic()
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _ask(source, timeout_s=0.3)
    assert refused.value.code == "second_factor_timeout"
    assert time.monotonic() - started < WAIT_S
    # No reader is left behind holding the terminal with echo off.
    assert _echo_on(follower)
    assert threading.active_count() == threads
    screen.prompts.acquire(timeout=0)  # the prompt that timed out
    _person(leader, screen, f"{CODE}\n")
    assert _ask(source) == CODE
    assert _echo_on(follower)


def test_input_typed_before_the_prompt_does_not_answer_it(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    os.write(leader, b"111111\n")  # typed earlier, at some other moment
    source, screen = _source(follower)
    _person(leader, screen, f"{CODE}\n")
    assert _ask(source) == CODE


def test_input_left_after_a_timeout_never_reaches_the_shell(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    source, screen = _source(follower)
    _person(leader, screen, "4829")  # part of a code, no Enter
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _ask(source, timeout_s=1.0)
    assert refused.value.code == "second_factor_timeout"
    import select

    # Nothing is left for the next reader (the shell, after the run exits).
    assert select.select([follower], [], [], 0.2)[0] == []


def test_an_empty_code_declines_and_an_approval_can_be_confirmed_or_declined(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    source, screen = _source(follower)
    _person(leader, screen, "\n")
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _ask(source)
    assert refused.value.code == "second_factor_declined"
    _person(leader, screen, "\n")
    assert _ask(source, "approval") is None
    _person(leader, screen, "n\n")
    with pytest.raises(sf.SecondFactorRefused) as refused:
        _ask(source, "approval")
    assert refused.value.code == "second_factor_declined"
    assert _echo_on(follower)


def test_a_run_without_a_terminal_never_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openadapt_flow.second_factor_terminal import TerminalSource

    class NotTty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NotTty())
    assert TerminalSource.open() is None
    assert sf.source_from_env({}) is None
    monkeypatch.setattr(TerminalSource, "open", classmethod(lambda cls: cls(0, None)))
    assert isinstance(sf.source_from_env({}), TerminalSource)
    for off in ("off", "0", "false", "OFF"):
        assert sf.source_from_env({sf.PROMPT_ENV: off}) is None
    # A command or a control plane wins over the terminal.
    assert isinstance(sf.source_from_env({sf.COMMAND_ENV: "otp"}), sf.CommandSource)


def test_the_prompt_setting_reaches_a_cached_runs_child() -> None:
    env = sf.source_env({sf.PROMPT_ENV: "off", sf.TOKEN_ENV: "t"})
    assert env == {sf.PROMPT_ENV: "off"}
