"""Asking for a second factor at the run's own terminal, on a real pty.

Each test opens a pseudo-terminal: the source reads the follower side, and
the test plays the person by writing to the leader side. Nothing here needs
a real terminal or a person.
"""

from __future__ import annotations

import io
import os
import sys
import threading
from typing import Iterator

import pytest

from openadapt_flow import second_factor as sf

termios = pytest.importorskip("termios")
pty = pytest.importorskip("pty")

CODE = "482913"


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


def _source(follower: int) -> tuple["TerminalSource", io.StringIO]:  # noqa: F821
    from openadapt_flow.second_factor_terminal import TerminalSource

    out = io.StringIO()
    return TerminalSource(follower, out), out


def _answer_later(leader: int, text: str, delay: float = 0.2) -> threading.Timer:
    timer = threading.Timer(delay, lambda: os.write(leader, text.encode()))
    timer.start()
    return timer


def _echo_on(follower: int) -> bool:
    return bool(termios.tcgetattr(follower)[3] & termios.ECHO)


def test_a_code_is_read_without_echo_and_echo_comes_back(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    assert _echo_on(follower)
    source, out = _source(follower)
    seen: list[bool] = []
    threading.Timer(0.1, lambda: seen.append(_echo_on(follower))).start()
    _answer_later(leader, "482 913\n")
    code = sf.obtain("code", "step_004", source=source, heartbeat=lambda: None)
    assert code is not None and code.consume() == CODE
    assert seen == [False]  # echo was off while the person typed
    assert _echo_on(follower)  # and on again afterwards
    assert "not shown" in out.getvalue() and CODE not in out.getvalue()


def test_a_timeout_restores_echo_and_the_next_prompt_gets_its_answer(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    source, _out = _source(follower)
    threads = threading.active_count()
    with pytest.raises(sf.SecondFactorRefused) as refused:
        source.obtain(sf.Request("code", "s", 0.3), heartbeat=lambda: None)
    assert refused.value.code == "second_factor_timeout"
    # No reader is left behind holding the terminal with echo off.
    assert _echo_on(follower)
    assert threading.active_count() == threads
    _answer_later(leader, f"{CODE}\n")
    reply = source.obtain(sf.Request("code", "s", 5.0), heartbeat=lambda: None)
    assert reply == CODE
    assert _echo_on(follower)


def test_input_typed_before_the_prompt_does_not_answer_it(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    os.write(leader, b"111111\n")  # typed earlier, at some other moment
    source, _out = _source(follower)
    _answer_later(leader, f"{CODE}\n", delay=0.3)
    assert source.obtain(sf.Request("code", "s", 5.0), heartbeat=lambda: None) == CODE


def test_an_empty_code_declines_and_an_approval_can_be_confirmed_or_declined(
    terminal: tuple[int, int],
) -> None:
    leader, follower = terminal
    source, _out = _source(follower)
    _answer_later(leader, "\n")
    with pytest.raises(sf.SecondFactorRefused) as refused:
        source.obtain(sf.Request("code", "s", 5.0), heartbeat=lambda: None)
    assert refused.value.code == "second_factor_declined"
    _answer_later(leader, "\n")
    assert (
        source.obtain(sf.Request("approval", "s", 5.0), heartbeat=lambda: None) is None
    )
    _answer_later(leader, "n\n")
    with pytest.raises(sf.SecondFactorRefused) as refused:
        source.obtain(sf.Request("approval", "s", 5.0), heartbeat=lambda: None)
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
