"""Clear an asyncio loop that a timed-out Playwright test left running.

Playwright's sync API runs its event loop in a dispatcher greenlet and marks
that loop as the thread's running loop for as long as a ``sync_playwright()``
session is open. A test that pytest-timeout interrupts inside a Playwright
call never stops its session, and the traceback pytest keeps holds the
dispatcher alive, so the loop stays marked running on the worker's main
thread. Every later ``sync_playwright()`` on that worker then fails with "It
looks like you are using Playwright Sync API inside the asyncio loop". On CI,
one timed-out browser test failed about fifty later tests this way.

A healthy module- or session-scoped browser fixture also keeps the loop
marked running between tests, so the loop is cleared only after a test that
pytest-timeout stopped (:func:`is_pytest_timeout`), never after every test.
The timed-out test still fails; this only keeps that failure from spreading.
"""

from __future__ import annotations

import asyncio

# pytest-timeout's failure text is "Timeout (>Ns) from pytest-timeout."
_PYTEST_TIMEOUT_MARK = "from pytest-timeout"


def is_pytest_timeout(exc: BaseException | None) -> bool:
    """True when ``exc`` is the failure pytest-timeout raises in a test."""

    return exc is not None and _PYTEST_TIMEOUT_MARK in str(exc)


def clear_leaked_running_loop() -> bool:
    """Unset a running loop left on this thread. True when one was cleared."""

    if asyncio._get_running_loop() is None:
        return False
    asyncio._set_running_loop(None)
    return True
