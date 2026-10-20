"""Stand-in worker processes for tests/test_quickstart_workers.py.

Spawned children import this module by name, so it lives at the top level of
tests/ (on sys.path under pytest) and imports nothing heavy.
"""

from __future__ import annotations

import time
from pathlib import Path


def _facts(index: int, note, run_dir: str) -> dict:
    return {
        "index": index,
        "note": note or "",
        "run_dir": run_dir,
        "execution_outcome": "VERIFIED",
        "transaction_outcome": "VERIFIED",
        "checked": True,
        "records": 1,
        "model_calls": 0,
        "cost_usd": 0.0,
        "effects_required": 2,
        "effects_confirmed": 2,
        "seconds": 0.1,
        "plain_label": "Done and checked",
    }


def _serve(conn, behave) -> None:
    conn.send(("ready",))
    while True:
        message = conn.recv()
        if message[0] != "run":
            return
        _, index, note, _bundle, run_dir = message
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        behave(conn, index, note, run_dir)


def good(conn, visible, browser="") -> None:
    _serve(conn, lambda c, i, n, d: c.send(("done", _facts(i, n, d))))


def stalls_on_run_2(conn, visible, browser="") -> None:
    def behave(c, i, n, d):
        if i == 2:
            time.sleep(600)
        c.send(("done", _facts(i, n, d)))

    _serve(conn, behave)


def fails_run_3(conn, visible, browser="") -> None:
    def behave(c, i, n, d):
        if i == 3:
            c.send(("failed", "TutorialError: the gate refused"))
        else:
            c.send(("done", _facts(i, n, d)))

    _serve(conn, behave)


def never_ready(conn, visible, browser="") -> None:
    conn.send(("broken", "Playwright's own Chromium isn't installed"))
