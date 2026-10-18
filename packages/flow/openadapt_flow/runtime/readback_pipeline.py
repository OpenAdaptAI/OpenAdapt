"""Pipelined read-back: verify item n's save while item n+1 runs.

A worklist worker with a read-back window above 0 gives its Replayer a
:class:`ReadbackPipeline`. When the LAST action of an item has a
system-of-record effect, the Replayer submits that effect's read-back to the
pipeline's side thread instead of waiting for it, finishes the item, and
returns. The worker then starts the next item on the same browser while the
read-back runs.

What still holds:

- The read-back is the same check with the same verifier; only its timing
  moves. The item's ``report.json`` is rewritten with the verdict (and its
  outcome re-stamped) when it finishes.
- Only the final action of an item is deferred. If the item runs any further
  step, the Replayer first waits for the read-back and halts on a failure, as
  it would inline.
- Only read-backs that don't use the screen are deferred (an on-screen
  read-back needs the browser, which the next item is using). Durable and
  idempotency-ledger runs always verify inline, because their terminal state
  is written once.
- The worker bounds how many read-backs may wait (the window), and the pool
  stops every worker on the first failed one.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class PipelinedReadback:
    """One deferred read-back: its verdict future and how to settle it."""

    step_id: str
    #: Resolves to the read-back's error text (None = confirmed).
    verdict: "Future[Optional[str]]"
    #: Applies the verdict to the item's report and saves it. Set by the
    #: Replayer when the item finishes.
    settle: Optional[Callable[[Optional[str]], Any]] = None
    #: Resolves to whatever ``settle`` returned (the settled report).
    settled: "Future[Any]" = field(default_factory=Future)


class ReadbackPipeline:
    """One side thread per worker for deferred read-backs."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readback")
        self._lock = threading.Lock()
        self._handoff: Optional[PipelinedReadback] = None
        #: Why the last item verified inline instead (for the run report).
        self.inline_reason: Optional[str] = None

    def submit(self, step_id: str, verify: Callable[[], Optional[str]]) -> PipelinedReadback:
        entry = PipelinedReadback(step_id=step_id, verdict=self._executor.submit(verify))
        return entry

    def hand_off(self, entry: PipelinedReadback, settle: Callable[[Optional[str]], Any]) -> None:
        """Called by the Replayer as the item finishes: settle on completion."""

        entry.settle = settle

        def _done(fut: "Future[Optional[str]]") -> None:
            try:
                error = fut.result()
            except Exception as exc:  # noqa: BLE001 - a raising verifier is a failure
                error = f"read-back raised {type(exc).__name__}; reconciliation is required"
            try:
                entry.settled.set_result(settle(error))
            except Exception as exc:  # noqa: BLE001
                entry.settled.set_exception(exc)

        with self._lock:
            self._handoff = entry
        entry.verdict.add_done_callback(_done)

    def take(self) -> Optional[PipelinedReadback]:
        """The read-back the last item handed off, if any (clears it)."""

        with self._lock:
            entry, self._handoff = self._handoff, None
        return entry

    def close(self) -> None:
        self._executor.shutdown(wait=True)
