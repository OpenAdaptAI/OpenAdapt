"""Where each step's time goes: per-step spans for ``report.json``.

Every step result carries ``timings``: milliseconds per span, measured as
exclusive time, so the spans of a step add up to its ``elapsed_ms`` (the rest
is ``other``). The spans are:

- ``settle_before`` / ``settle_after``: waiting for the screen or page to stop
  changing, before and after the action
- ``screenshot``: capturing frames
- ``resolve``: finding the target
- ``identity``: the right-record check
- ``ocr``: text recognition, whichever check asked for it
- ``actuate``: delivering the click, key or text
- ``postcondition``: screen postconditions, input read-back, dialogs
- ``readback``: the system-of-record read-back after a save
- ``artifact_write``: writing evidence files

Functions opt in with :func:`timed_span`. Timing is per thread, so parallel
worklist workers don't mix their steps, and costs two ``perf_counter`` calls
per span when no step is being timed.
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable, Optional, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

SPANS = (
    "settle_before",
    "settle_after",
    "screenshot",
    "resolve",
    "identity",
    "ocr",
    "actuate",
    "postcondition",
    "readback",
    "artifact_write",
)

_local = threading.local()


class StepTimer:
    """Exclusive time per span for one step on one thread."""

    def __init__(self) -> None:
        self.totals: dict[str, float] = {}
        self._stack: list[list[Any]] = []  # [span, start, child_s]
        self.acted = False
        self._t0 = time.perf_counter()

    def enter(self, span: str) -> None:
        if span == "settle":
            span = "settle_after" if self.acted else "settle_before"
        elif span == "actuate":
            self.acted = True
        self._stack.append([span, time.perf_counter(), 0.0])

    def exit(self) -> None:
        span, start, child = self._stack.pop()
        incl = time.perf_counter() - start
        self.totals[span] = self.totals.get(span, 0.0) + (incl - child)
        if self._stack:
            self._stack[-1][2] += incl

    def result_ms(self, elapsed_ms: Optional[float] = None) -> dict[str, float]:
        total_s = (
            elapsed_ms / 1000.0
            if elapsed_ms is not None
            else time.perf_counter() - self._t0
        )
        out = {k: round(v * 1000.0, 1) for k, v in self.totals.items() if v > 0}
        other = total_s - sum(self.totals.values())
        if other > 0:
            out["other"] = round(other * 1000.0, 1)
        return out


def current() -> Optional[StepTimer]:
    return getattr(_local, "timer", None)


def start_step() -> tuple[StepTimer, Optional[StepTimer]]:
    """Start timing a step; returns the new timer and the one it replaced."""
    previous = getattr(_local, "timer", None)
    timer = StepTimer()
    _local.timer = timer
    return timer, previous


def end_step(previous: Optional[StepTimer]) -> None:
    _local.timer = previous


def timed_span(span: str) -> Callable[[F], F]:
    """Count the wrapped call's own time toward ``span`` of the current step."""

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            timer = getattr(_local, "timer", None)
            if timer is None:
                return fn(*args, **kwargs)
            timer.enter(span)
            try:
                return fn(*args, **kwargs)
            finally:
                timer.exit()

        return wrapper  # type: ignore[return-value]

    return deco
