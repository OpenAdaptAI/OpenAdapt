# vendored from 2b9a14cc4014:packages/flow/openadapt_flow/txplan/audit.py, lines 473-503, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S2 pre-fix: an old-stamped event ends the audit read before the cursor (#2437).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AuditError(RuntimeError):
    pass


@dataclass
class Window:
    events: list[Any] = field(default_factory=list)
    pages: int = 0
    other_actors: int = 0


Cursor = Any


class Reader:
    """Stub of AuditReader: pages of events, newest first, then an empty page."""

    def __init__(self, events: list[Any], page_size: int, skew_s: float = 0.0) -> None:
        self._events = events
        self._size = page_size
        self.source = type("Source", (), {"skew_s": skew_s})()

    def pages(self) -> Any:
        for i in range(0, len(self._events), self._size):
            yield self._events[i : i + self._size]
        yield []

    def _mine(self, e: Any) -> bool:
        return bool(e.actor == "me")


class PreReader(Reader):
    # vendored: 2b9a14cc4014:packages/flow/openadapt_flow/txplan/audit.py 473-503
    # fmt: off
    def after(self, cursor: Cursor) -> Window:
        """Every event after ``cursor`` by this actor, read to the cursor."""
        win = Window()
        seen: set[str] = set()
        reached = False
        for items in self.pages():
            win.pages += 1
            older = False
            for e in items:
                if cursor.at is not None and e.at < cursor.at - self.source.skew_s:
                    older = True
                    continue
                if e.id in cursor.ids:
                    reached = True
                    continue
                if e.id in seen:
                    continue
                seen.add(e.id)
                if self._mine(e):
                    win.events.append(e)
                else:
                    win.other_actors += 1
            if older:
                return win
            if not items:
                break
        if cursor.at is not None and not reached:
            # The cursor's own events are in the log, so a read that ends
            # without reaching any of them is incomplete.
            raise AuditError("the log ended before the cursor")
        return win
    # fmt: on
