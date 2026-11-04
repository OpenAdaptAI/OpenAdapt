"""Clear an asyncio loop that an interrupted Playwright test left running.

Playwright's sync API runs its event loop in a dispatcher greenlet and marks
that loop as the thread's running loop. A test that pytest-timeout interrupts
inside a Playwright call never stops it, and the traceback pytest keeps holds
the dispatcher alive, so the loop stays marked running on the worker's main
thread. Every later ``sync_playwright()`` on that worker then fails with "It
looks like you are using Playwright Sync API inside the asyncio loop". On CI,
one timed-out browser test failed about fifty later tests this way.

Between two synchronous tests no loop should be running, so clearing one
there cannot affect a healthy test. The interrupted test still fails on its
own timeout; this only keeps that failure from spreading.
"""

from __future__ import annotations

import asyncio


def clear_leaked_running_loop() -> bool:
    """Unset a running loop left on this thread. True when one was cleared."""

    if asyncio._get_running_loop() is None:
        return False
    asyncio._set_running_loop(None)
    return True
